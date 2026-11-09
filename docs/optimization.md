# Optimization report

## Method

Local host: AMD Ryzen 7 5800H, 16 logical CPUs, 61 GB RAM. Two worker processes, one gateway. Model `Qwen/Qwen2.5-0.5B-Instruct` in float32 unless noted. Each matrix run offers 2 requests/s for 10 seconds (20 requests), 8 output tokens, 8 connections. The adaptive comparison uses 4 requests/s for 20 seconds (80 requests).

Each run starts fresh workers, so the response cache begins cold. The cache comparison uses a repeated prompt and hash routing.

The full data is in `benchmarks/llm-optimization.json` and `benchmarks/llm-adaptive.json`.

## Matrix

| Config | Successes | 503 | p50 | p95 | Accepted req/s | Tokens/s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Base: 4 threads, 10 ms, limit 2 | 15 / 20 | 5 | 940 ms | 1,072 ms | 1.51 | 12.1 |
| 2 threads | 14 / 20 | 6 | 1,201 ms | 1,330 ms | 1.32 | 10.5 |
| 8 threads | 14 / 20 | 6 | 988 ms | 1,132 ms | 1.36 | 10.9 |
| Batch window 0 ms | 15 / 20 | 5 | 947 ms | 1,086 ms | 1.51 | 12.1 |
| Batch window 25 ms | 15 / 20 | 5 | 984 ms | 1,117 ms | 1.51 | 12.1 |
| Max batch 4 | 15 / 20 | 5 | 971 ms | 1,076 ms | 1.45 | 11.6 |
| Admission 0 | 20 / 20 | 0 | 1,412 ms | 1,998 ms | 1.83 | 14.6 |
| Admission 1 | 10 / 20 | 10 | 830 ms | 859 ms | 1.02 | 8.1 |
| Admission 4 | 20 / 20 | 0 | 1,427 ms | 1,994 ms | 1.83 | 14.6 |
| Hash routing | 14 / 20 | 6 | 1,173 ms | 1,660 ms | 1.33 | 10.6 |
| Cache, repeated prompt | 20 / 20 | 0 | 1.9 ms | 841 ms | 2.11 | 16.8 |
| SmolLM2-135M | 20 / 20 | 0 | 325 ms | 346 ms | 2.04 | 16.3 |
| Adaptive, max 4, target 800 ms | 20 / 20 | 0 | 1,458 ms | 1,916 ms | 1.76 | 14.1 |
| Retry 3 attempts, limit 2 | 19 / 20 | 1 | 1,066 ms | 4,227 ms | 1.53 | 12.2 |

## Findings

**Threads.** Four threads per worker gives the best p95. Two threads underutilize the CPU; eight threads per worker (16 total on 16 logical CPUs) adds contention and raises p95 to 1,132 ms.

**Batch window.** The 10 ms window is the best default. Zero and 25 ms are close, but 0 ms gives up batching and 25 ms adds queue time without a measurable throughput gain at this load.

**Batch size.** A maximum of 8 and a maximum of 4 are effectively tied. Keep 8 because it gives the scheduler room at higher concurrency.

**Admission.** A limit of 2 is the best balance at 2 requests/s: p95 1,072 ms and 75% accepted. Limit 1 protects p95 at 859 ms but sheds half the work. Limits 0 and 4 both queue: p95 about 2.0 seconds. Admission is a latency-versus-goodput choice, not a capacity increase.

**Routing.** Least-loaded beats hash for unique prompts: p95 1,072 ms versus 1,660 ms. Hash is useful when repeated prompts must reach the same worker for cache locality; it is not a general-purpose default.

**Cache.** A repeated prompt hits 18 of 20 times. The two misses dominate p95; hits return in about 1.9 ms. The cache is a response cache, not a KV cache, and only stores greedy responses.

**Model.** SmolLM2-135M is about 3.1x faster at p95 (346 ms versus 1,072 ms) and reaches 2.04 accepted req/s. Model quality is not evaluated here. Use 0.5B for the default demo and 135M when latency matters more than answer quality.

**Adaptive admission.** The controller moved the limit from 6 to 1 in five steps. It still had p95 2,654 ms because the early requests queued at limit 6 and the 20-sample window reacted after the queue was already deep. Static limit 2 had p95 1,242 ms with 1.85 accepted req/s; adaptive accepted 2.14 req/s but with a worse tail. Static remains the default, matching the C++ result.

**Retry.** Three attempts raise p95 to 4,227 ms and do not create capacity. Retry helps a transient 503; it makes sustained overload worse. Use a retry budget, jitter, and `Retry-After`; do not retry forever.

## Decisions

- Default worker: 4 torch threads, 10 ms batch window, max batch 8, cache 256.
- Default gateway: least-loaded routing, static admission 2 per gateway, adaptive opt-in.
- Default model: Qwen2.5-0.5B-Instruct. SmolLM2-135M is the low-latency option.
- Cache only greedy responses; hash routing only when repeated prompts are expected.
- Retry is a client policy, not a server capacity fix.

## Limits

- One host; workers and load generator share CPU.
- 20 requests per matrix run; the adaptive comparison uses 80.
- No streaming, time-to-first-token, KV-cache reuse, or continuous batching across parameter sets.
- No AWS or multi-host result in this report.
- The numbers are benchmark evidence, not a production capacity claim.
