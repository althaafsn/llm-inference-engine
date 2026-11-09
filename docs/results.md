# Local results

All runs are local, CPU-only, on a 16-logical-CPU AMD Ryzen 7 5800H host. The model is `Qwen/Qwen2.5-0.5B-Instruct` in float32. Each worker uses 8 torch threads. The load generator, gateway, and workers share the host CPU.

The numbers are benchmark evidence, not production capacity claims.

## Baseline queue collapse

2 workers, one gateway, no admission limit. Offered 4 requests/s for 10 seconds, 16 output tokens, 16 connections.

| Metric | Value |
| --- | ---: |
| Requests | 40 |
| Successes | 40 |
| Failures | 0 |
| Wall time | 19.293 s |
| p50 | 5,372 ms |
| p95 | 13,051 ms |
| p99 | 13,802 ms |
| Accepted throughput | 2.07 req/s |
| Token throughput | 33.2 tok/s |
| Cache hits | 10 |

Every request returned HTTP 200, but the 10-second workload took 19.3 seconds to drain and p95 reached 13 seconds. That is queue collapse.

## Admission control

Same topology and load, with a per-gateway in-flight limit.

| Limit | Successes | 503 | Wall | p50 | p95 | p99 | Accepted req/s | Tokens/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2 | 10 / 40 | 30 | 10.724 s | 1,954 ms | 2,031 ms | 2,031 ms | 0.93 | 14.9 |
| 4 | 12 / 40 | 28 | 12.133 s | 3,756 ms | 4,060 ms | 4,060 ms | 0.99 | 15.8 |

The limit of 2 gives the better tail: p95 falls from 13.1 s to 2.03 s, and the run finishes in the workload window. It sheds 75% of requests with explicit HTTP 503 and `Retry-After`. The limit of 4 admits slightly more work but doubles p95. The limit is a latency-versus-goodput tradeoff, not a capacity increase.

## Cache locality

1 worker, hash routing, repeated prompt, 2 requests/s for 6 seconds, 8 tokens.

| Metric | Value |
| --- | ---: |
| Requests | 12 |
| Successes | 12 |
| Cache hits | 10 |
| p50 | 1.9 ms |
| p95 | 1,046 ms |
| Accepted throughput | 2.18 req/s |

The first requests miss and run inference; later requests hit the LRU cache and return in about 2 ms. Hash routing keeps the repeated prompt on the same worker, which is what makes the cache effective.

## Gateway failover

2 workers, 2 gateways, one health-checked proxy. Offered 1 request/s for 10 seconds, 8 tokens, no admission limit. Gateway 1 is terminated 2 seconds after the load starts. The proxy polls `/health` every second and retries the other gateway once on a connection error.

| Metric | Value |
| --- | ---: |
| Requests | 10 |
| Successes | 10 |
| Failures | 0 |
| p50 | 767 ms |
| p95 | 847 ms |
| Accepted throughput | 1.02 req/s |

One request was served by gateway 1 before the kill; all later requests were served by gateway 2. There were no client-visible 502, 503, or 504 responses. The proxy detects a dead gateway on the next connection error and retries immediately; the one-second health poll is the upper bound on detection, not the observed request failure window.

A second failover run at 2 requests/s with a per-gateway limit of 2 produced 10 successes and 10 admission `503`s, but no `502` or `504`. That isolates the failover behavior from the load-shedding behavior.

## Interpretation

- Admission control protects the accepted requests. It does not increase the model's throughput.
- The model is the bottleneck: 0.5B parameters on CPU, 8 threads per worker. The accepted throughput is about 1 request/s for 16-token answers, or about 2 requests/s for 8-token answers.
- Batching helps, but it cannot exceed the CPU inference rate. The baseline's HTTP 200 responses hide multi-second queue waits.
- The cache is effective only for repeated prompts and deterministic generation (`temperature=0`). It is not a KV cache and does not help different prompts.
- The local proxy fails over without client-visible errors. This is not an ALB result and not a multi-host test.

## AWS multi-host internal ALB

Topology: two `c6i.large` gateways, two `c6i.xlarge` workers, one `c6i.large` load generator, and one internal Application Load Balancer in `us-east-1`. The load generator sent 2 requests/s for 20 seconds with 8 output tokens and 32 connections. The target group used HTTP `8100`, path `/health`, 5 s interval, 3 s timeout, 2 healthy and 2 unhealthy checks.

| Phase | Successes | 502 | 504 | p50 | p95 | p99 | Accepted req/s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Normal | 40 / 40 | 0 | 0 | 762 ms | 1,349 ms | 1,412 ms | 2.05 |
| Failover (gateway 1 terminated) | 31 / 40 | 8 | 1 | 767 ms | 1,308 ms | 1,312 ms | 1.35 |
| Recovery (replacement gateway) | 40 / 40 | 0 | 0 | 754 ms | 773 ms | 777 ms | 1.97 |

- Normal and recovery split requests 20/20 between the two gateways.
- TargetHealth reported the terminated gateway unhealthy **15.8 seconds** after the termination request. The 5 s interval and two unhealthy checks set the floor.
- Dead-target requests started about 5.0 seconds after the load marker and the last one started about 13.0 seconds. Clients saw **8 × 502** and **1 × 504**. The 504 completed about 23.0 seconds after the load marker because it waited the 10 s client timeout.
- Recovery launched a replacement gateway, registered it, and split traffic 20/20 with the surviving gateway.
- Successful run cost **$0.089016**. Earlier ALB attempts cost **$0.401329**.

### Client retry

A second ALB run added bounded client retry: three attempts, 100 ms initial backoff, 1 s maximum, 0.2 jitter, and `Retry-After` handling. Normal and recovery still completed 40/40. The failover phase changed to:

| Failover with retry | Successes | 502 | 504 | 503 | p50 | p95 | p99 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Gateway 1 terminated | 38 / 40 | 0 | 0 | 2 | 779 ms | 10,839 ms | 12,118 ms |

- Sixteen requests were retried. Fourteen recovered; two exhausted with 503 from the surviving gateway's admission limit.
- Retry removed the 502/504 responses, but p95 rose to 10.8 s because retries waited for the 10 s client timeout before succeeding. A production client needs a shorter timeout or a retry budget.
- TargetHealth reported the dead gateway unhealthy after **10.189 seconds** in this run. The observation varies with health-check timing; the two checks set a 10 s floor.
- The retry run cost **$0.092016**. Multi-host total, including the no-retry run, earlier attempts, and the 8-worker capacity run: **$0.930476**.

### Reading the normal p95

The normal phase has a higher p95 (1,349 ms) than recovery (773 ms). The first requests after the workers start include warmup and the tail of two requests drives p95 at this sample size. Recovery runs on warm workers. The p50 is stable at about 760 ms in all phases.

## AWS 8-worker capacity

Topology: two `c6i.large` gateways, eight `c6i.xlarge` workers, one `c6i.large` load generator, and one internal ALB. Each worker is on its own host. The normal phases used 4, 8, and 12 offered requests/s for 10 seconds each, 8 output tokens, 32 connections, and no admission limit.

| Offered req/s | Successes | 5xx | p50 | p95 | p99 | Accepted req/s | Tokens/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 40 / 40 | 0 | 1,029 ms | 1,306 ms | 1,316 ms | 3.71 | 29.6 |
| 8 | 80 / 80 | 0 | 789 ms | 1,463 ms | 1,541 ms | 7.38 | 59.1 |
| 12 | 120 / 120 | 0 | 884 ms | 1,504 ms | 1,601 ms | 11.17 | 89.4 |

At 12 offered requests/s, 120/120 requests succeeded at p95 1.5 seconds. Accepted throughput was 11.17 requests/s, 93% of the offered rate. The run did not find the ceiling; 12 requests/s is a verified point, not the maximum.

Failover at 12 requests/s: 116/120 succeeded, 4 × 502, p95 1,599 ms. The retry client retried 37 requests, recovered 33, and exhausted 4. TargetHealth reported the dead gateway unhealthy after **15.811 seconds**. Recovery at 12 requests/s completed 120/120 at p95 1,497 ms.

### Distributed versus one engine

- One local worker, cold cache, 4 offered req/s: 2.60 accepted req/s, p95 3,517 ms.
- Two local workers on the same host: 2.34 accepted req/s, p95 4,413 ms. More workers made it worse because the host was CPU-saturated.
- Two AWS workers behind the ALB: 2 offered req/s, p95 1,349 ms. This was not a capacity point.
- Eight AWS workers on separate hosts: 11.17 accepted req/s at 12 offered, p95 1,504 ms.

The useful result is the direction: adding workers on one host does not help, but adding separate hosts does. The 8-worker run served about 5.6× the 2-worker offered rate at a similar p95, with four times the workers. That is not linear, and the 2-worker run was not a ceiling measurement, but it shows real horizontal scaling.

Cost: **$0.348115**. Cleanup verified; no tagged instances, volumes, security groups, load balancers, or target groups remained.

## AWS single-host deployment

The stack was deployed to one `c6i.2xlarge` host in `us-east-1` (Amazon Linux 2023, Docker Compose). The host ran two workers, two gateways, and the HA proxy. The benchmark ran inside the Compose network against `http://proxy:8180`. The security group allowed SSH only from the owner `/32`; no application port was public.

| Phase | Successes | 503 | p50 | p95 | p99 | Accepted req/s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Normal | 20 / 20 | 0 | 473 ms | 510 ms | 513 ms | 2.00 |
| Failover (gateway 1 stopped) | 20 / 20 | 0 | 499 ms | 724 ms | 837 ms | 2.01 |
| Recovery (gateway 1 restarted) | 20 / 20 | 0 | 456 ms | 595 ms | 646 ms | 2.01 |

- Normal and recovery split requests 10/10 between gateway 1 and gateway 2.
- During failover, all 20 requests were served by gateway 2. There were no client-visible 502, 503, or 504 responses. The proxy health interval is one second, and it retries a connection error once.
- The host was terminated; the security group, key pair, and volume were removed. A read-only check found no active tagged instances, volumes, or security groups.
- Successful run cost **$0.024164**. Earlier bootstrap attempts cost **$0.232165** (Docker bootstrap failures and one run with an invalid failover phase). Single-host total: **$0.256329**.
- Multi-host ALB total: **$0.930476**. Combined LLM total: **$1.186805**. Combined with the C++ ONNX project: **$4.752693 of $10**, about **$5.25** remains.

## Limits

- One host; workers and load generator share CPU.
- One model (0.5B) and one prompt set.
- 10-second runs; small request counts.
- No TLS, authentication, public endpoint, or global admission.
- The AWS ALB run used one region and one subnet/AZ for targets; it is not a multi-AZ failover test.
- No KV-cache reuse, continuous batching across different parameter sets, or streaming.
- The cache only stores greedy responses.

## Next

1. Run the harness on separate worker and gateway hosts with an internal ALB.
2. Add streaming responses and measure time to first token.
3. Add a shared admission limit across gateways.
4. Test multi-AZ placement and a real instance kill.
