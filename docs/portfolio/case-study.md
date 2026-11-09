# Distributed LLM inference with admission control and HA failover

**Python 3.12 · PyTorch · Hugging Face · AWS EC2 · Application Load Balancer**

## Summary

I built a distributed serving system for a small Hugging Face language model. A gateway routes requests to batched workers, workers cache greedy responses, admission control protects the queue, and a health-checked proxy and an internal AWS ALB provide failover. The work turned a queue-collapse failure into an explicit load-shedding contract: at 4 requests/s, accepted p95 fell from 13.1 seconds to 2.03 seconds. On AWS, a two-gateway internal ALB completed normal, failover, and recovery, but took 15.8 seconds to mark a dead target unhealthy and exposed 8 HTTP 502 and 1 HTTP 504 response. I measured that gap instead of hiding it.

**Provenance:** this is a new Python implementation of the same architecture as my C++ ONNX engine. The C++ engine is a separate project. I set the goals, chose the experiments, and reviewed the results; implementation and runs were AI-assisted.

## Problem

The model is Qwen2.5-0.5B-Instruct in float32 on CPU. Two workers with four torch threads each can serve about 1.5–2 requests/s for 8-token answers. Under 4 requests/s, the baseline kept accepting work and returned HTTP 200 for every request, but the queue grew without bound: p95 reached 13.1 seconds and a 10-second workload took 19.3 seconds to drain. That is a failure even when every response is "successful".

The second question was availability. A single gateway is a single point of failure. The system needed evidence that a second gateway, a load balancer, and a replacement process could keep serving.

## What I built

- **Gateway and workers.** An `aiohttp` gateway and worker processes. The gateway validates requests, routes by least-loaded or consistent hash, retries one alternate worker, and exposes `/stats` and `/metrics`.
- **Batching and cache.** Workers group requests by generation settings, wait a 10 ms batch window, and run one `model.generate` call per batch. An LRU caches greedy responses; a repeated prompt returns in about 2 ms.
- **Admission control.** A per-gateway in-flight limit returns HTTP 503 with `Retry-After: 1` when full. An AIMD adaptive policy can adjust the limit from a p95 window; static remains the default because it measured better.
- **Observability and retry.** Prometheus metrics, JSON stats, and a retry-aware benchmark client with exponential backoff, jitter, and `Retry-After` handling.
- **HA.** A health-checked local proxy with two gateways, plus a multi-host AWS controller that builds an internal ALB, runs normal/failover/recovery, replaces the killed gateway, and cleans up.
- **Tests and CI.** 40 Python tests, Ruff, and GitHub Actions. Tests cover the cache, routing, circuit breaker, batching, queue-full behavior, admission, adaptive policy, retry delays, gateway retry, and HA proxy.

## Results

### Local overload

| Run | Successes | 503 | p50 | p95 | Accepted req/s |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline, no admission | 40 / 40 | 0 | 5,373 ms | 13,051 ms | 2.07 |
| Admission limit 2 | 10 / 40 | 30 | 1,954 ms | 2,031 ms | 0.93 |
| Admission limit 4 | 12 / 40 | 28 | 3,756 ms | 4,060 ms | 0.99 |

### Optimization

A 14-config matrix varied threads, batch window, batch size, admission, routing, cache, model, adaptive admission, and retry.

- 4 threads per worker beat 2 and 8 threads (p95 1,072 ms vs 1,330 and 1,132).
- A 10 ms batch window was the best default; 0 and 25 ms were close but did not improve p95.
- Least-loaded routing beat hash for unique prompts (p95 1,072 ms vs 1,660 ms). Hash is for cache locality, not a general default.
- A repeated prompt hit the cache 18 of 20 times and returned in about 1.9 ms.
- SmolLM2-135M was about 3.1× faster at p95 (346 ms vs 1,072 ms). Quality was not evaluated.
- Adaptive admission converged from 6 to 1, but static 2 still had the better tail (1,242 ms vs 2,654 ms) because the early queue dominated.
- A separate 1/2/4-worker sweep on one 16-CPU host got worse with more workers (p95 3.5, 4.4, and 5.6 seconds). The host was CPU-saturated; the AWS topology gives each worker its own host.

### AWS single host

One `c6i.2xlarge` host with the full Docker Compose stack: 20/20 in normal, failover, and recovery; failover served all 20 requests from gateway 2 with no 502/503/504; p95 510, 724, and 595 ms. Cost $0.024164.

### AWS multi-host internal ALB

Two gateways, two workers, one load generator, internal ALB, 2 requests/s for 20 seconds.

| Phase | Successes | 502 | 504 | p50 | p95 | p99 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Normal | 40 / 40 | 0 | 0 | 762 ms | 1,349 ms | 1,412 ms |
| Failover | 31 / 40 | 8 | 1 | 767 ms | 1,308 ms | 1,312 ms |
| Recovery | 40 / 40 | 0 | 0 | 754 ms | 773 ms | 777 ms |

## What failed, and what I learned

The ALB did fail over, but not fast enough for clients. The target group checks every 5 seconds and requires two unhealthy checks. TargetHealth first reported the dead gateway unhealthy **15.8 seconds** after the termination request. Dead-target requests started about 5.0 seconds after the load marker; the last one started about 13.0 seconds. Clients saw 8 × 502 and 1 × 504. The 504 completed about 23.0 seconds after the load marker because it waited the 10-second client timeout.

The local proxy polls every second and retries a connection error, so it fails over without client-visible errors. The ALB does not retry a POST. The practical fixes are bounded client retry for 502/504, draining for planned shutdowns, or faster detection. A hard instance kill cannot drain.

The normal phase had a higher p95 than recovery (1,349 ms vs 773 ms) because the first requests include worker warmup and two slow requests drive p95 at this sample size. The p50 is stable at about 760 ms.

## Limits

- One region; all targets in one subnet/AZ. The ALB spans two AZs, but this is not a multi-AZ failover test.
- Qwen2.5-0.5B on CPU only; no GPU, larger model, or model-quality evaluation.
- No streaming or time-to-first-token measurement.
- Response cache only; no KV-cache reuse or continuous batching across generation settings.
- No global admission, TLS, authentication, or public endpoint.
- One ALB run; benchmark evidence, not a production capacity claim.

## Next

1. Add streaming and measure time to first token.
2. Add bounded client retry for 502/504 and rerun the ALB failover.
3. Add a KV cache and continuous batching.
4. Test multi-AZ placement and a shared admission limit.

## Evidence

- [Results](../results.md)
- [Optimization report](../optimization.md)
- [Deployment guide](../../deploy/README.md)
- [Source repository](https://github.com/althaafsn/llm-inference-engine)
