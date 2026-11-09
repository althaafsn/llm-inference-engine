# Resume entry

## Primary entry

**Distributed LLM Inference Engine — Python, PyTorch, AWS** · 2026  
*[github.com/althaafsn/llm-inference-engine](https://github.com/althaafsn/llm-inference-engine)*

- Built a distributed LLM serving engine with an `aiohttp` gateway, batched PyTorch workers, an LRU response cache, per-gateway admission control, and Prometheus metrics; cut accepted p95 from 13.1 s to 2.03 s at 4 requests/s by shedding excess load with HTTP 503 and `Retry-After`.
- Ran a 14-config matrix and LLM-specific runtime tests; selected 4 threads, 10 ms batching, least-loaded routing, and a static limit of 2, and used dynamic int8 quantization to cut p95 from 5.0 s to 265 ms at 4 requests/s.
- Deployed two gateways and two workers behind an internal AWS ALB; measured a 10–16 s target-health failover, then added bounded retry that recovered 14 of 16 retried requests and left 2 exhausted 503s, with p95 rising to 10.8 s.
- Added 40 tests and CI; kept total AWS spend to **$0.84** and verified zero remaining resources after every run.

## Tailoring

**Backend / distributed systems:** keep bullets 1 and 3. Emphasize the 503 contract, p95, batching, cache, routing, and the failover window.

**Infrastructure / SRE:** keep bullets 3 and 4. Emphasize the ALB target group, health checks, replacement gateway, preflight, cleanup, and cost control.

**ML systems:** keep bullets 1 and 2. Emphasize batching, response cache, model comparison, tokens/s, and the CPU capacity limits.

Use three bullets if space is tight. Drop the cost bullet first; the engineering bullets are stronger.

## Evidence map

| Claim | Evidence |
| --- | --- |
| p95 13.1 s → 2.03 s at 4 req/s | `docs/results.md`, local overload table |
| 14-config optimization matrix | `docs/optimization.md`, `benchmarks/llm-optimization.json` |
| 4 threads, 10 ms, least-loaded, limit 2 | `docs/optimization.md`, decisions section |
| Int8 p95 5.0 s → 265 ms at 4 req/s | `docs/runtime-optimizations.md`, `benchmarks/llm-runtime.json` |
| SmolLM2-135M 3.1x lower p95 | `docs/optimization.md`, model row |
| 10–16 s target-health failover | `docs/results.md`, AWS multi-host section |
| 14 of 16 retried; 2 exhausted 503s | same section, client retry table |
| 40 tests | `tests/`, CI |
| $0.84 total AWS spend | `docs/results.md`, cost lines |
| Cleanup verified | `docs/results.md`; read-only AWS check |

## Do not claim

- “Production-ready”, “zero downtime”, or “multi-AZ”. The ALB run used one subnet/AZ for targets and exposed 502/504.
- “Global rate limiting”. The limit is per gateway.
- “TLS”, “authentication”, or “public endpoint”. None were built.
- “Streaming” or “time to first token”. Neither was measured.
- “KV cache” or “continuous batching”. The cache is a response cache; batches run to completion.
- “Handles 4 requests/s”. The offered rate was 4; accepted throughput was about 1–2 requests/s after shedding.
- “Wrote every line”. Implementation and runs were AI-assisted; you directed the work and reviewed the results.
- “Retry fixed the failover latency”. Retry removed 502/504 but raised p95 to 10.8 s; the health window is still 10–16 s.
- “Quantization is quality-neutral”. It was spot-checked only, not benchmarked. The PyTorch dynamic quantization API is deprecated in torch 2.15.

## Can you defend this?

1. **Why Python instead of C++?** The question was what changes when the unit of work is a token stream. Python and transformers made that cheap to test with a real model; the gateway, admission, routing, and failover design is the same. A C++ or llama.cpp backend can replace `HFBackend` without changing the gateway.
2. **Why batching, and why not continuous batching?** Batches amortize the model call and improve throughput. Continuous batching needs a token-level scheduler and a KV cache; this version runs a batch to completion and groups requests by generation settings.
3. **Why admission limit 2?** At 2 requests/s, limit 2 gave p95 1,072 ms with 75% accepted. Limit 1 protected p95 at 859 ms but shed half the work; limits 0 and 4 queued to about 2 seconds.
4. **Why static instead of adaptive?** Adaptive converged from 6 to 1, but the early queue at limit 6 dominated p95 (2,654 ms). Static 2 had p95 1,242 ms at 4 requests/s. Static remains the default; adaptive is opt-in.
5. **Why p95 and accepted throughput?** Tail latency is what users feel. Accepted throughput shows the goodput that remains after shedding; offered rate alone hides the rejection cost.
6. **Why 10–16 s for the ALB?** The target group checks every 5 s and requires two unhealthy checks. TargetHealth reported the dead gateway unhealthy at 15.8 s in the no-retry run and 10.2 s in the retry run; the timing depends on where the check lands.
7. **Why 502 and 504?** The ALB kept routing to the dead target until it was marked unhealthy. 502 means the target could not be reached; 504 means the client timed out after 10 s. Retry removed these responses but not the window.
8. **Why did the last 504 finish at 23 s?** It started at about 13 s and waited the 10 s client timeout. The health state changed at 15.8 s, but the request was already in flight.
9. **Why is normal p95 higher than recovery?** The normal phase includes worker warmup; the p50 is stable at about 760 ms. Recovery runs on warm workers.
10. **Why cache only greedy responses?** Sampled responses are not deterministic. Caching them would return a different answer for the same prompt and settings.
11. **Why least-loaded instead of hash?** Least-loaded beat hash for unique prompts (1,072 ms vs 1,660 ms). Hash is useful when repeated prompts must reach the same worker for cache locality.
12. **What did retry fix, and what did it cost?** It removed 502/504; 14 of 16 retried requests recovered. The cost is p95 10.8 s because retries waited for the 10 s timeout. A shorter timeout or a retry budget is next.
13. **What did int8 quantization actually change?** It replaced Linear weights with int8 and kept activations dynamic. p95 fell from 5.0 s to 265 ms at 4 requests/s, and accepted throughput rose from 3.11 to 4.10. The baseline was queueing; quantization removed the queue. Quality was spot-checked, not benchmarked, and the API is deprecated.
14. **What did you personally do?** You set the goals, chose the experiments, and reviewed the results. Implementation and runs were AI-assisted. Say that plainly and be ready to explain the code paths.

## Risks before publishing

- The model is 0.5B on CPU; do not imply GPU-scale or production capacity.
- The project is strongest as distributed systems and performance engineering, not as a general backend service. It has no database, authentication, or multi-tenant API.
- The 10–16 s failover is a limitation, not a hidden failure. Present it as measured evidence and a clear next step.
- The repository is MIT-licensed and original; the C++ ONNX engine is a separate project with its own provenance note.
- Quantization is an opt-in benchmark result, not a quality guarantee. The default is FP32.
