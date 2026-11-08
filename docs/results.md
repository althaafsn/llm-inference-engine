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

## Limits

- One host; workers and load generator share CPU.
- One model (0.5B) and one prompt set.
- 10-second runs; small request counts.
- No TLS, authentication, public endpoint, or global admission.
- No AWS ALB, multi-AZ, or multi-host run.
- No KV-cache reuse, continuous batching across different parameter sets, or streaming.
- The cache only stores greedy responses.

## Next

1. Run the same harness on separate AWS hosts with an internal ALB.
2. Add streaming responses and measure time to first token.
3. Add a shared admission limit across gateways.
4. Test multi-AZ placement and a real instance kill.
