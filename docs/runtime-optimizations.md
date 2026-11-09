# Runtime optimizations

## Method

Local host: AMD Ryzen 7 5800H, 16 logical CPUs. Two workers, four torch threads each, `Qwen/Qwen2.5-0.5B-Instruct`, 4 offered requests/s for 10 seconds, 8 output tokens, 16 connections, no admission limit. Each run starts fresh workers with a cold response cache.

| Config | Successes | p50 | p95 | p99 | Accepted req/s | Tokens/s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| FP32, SDPA | 40 / 40 | 3,406 ms | 5,014 ms | 5,405 ms | 3.11 | 24.9 |
| FP32, SDPA, warmup | 40 / 40 | 3,081 ms | 4,916 ms | 5,415 ms | 3.19 | 25.5 |
| **Int8 dynamic, SDPA** | **40 / 40** | **249 ms** | **265 ms** | **278 ms** | **4.10** | **31.8** |
| Int8 dynamic, SDPA, warmup | 40 / 40 | 249 ms | 267 ms | 287 ms | 4.10 | 31.8 |
| FP32, eager attention | 40 / 40 | 3,075 ms | 4,856 ms | 5,290 ms | 3.12 | 24.9 |

## Findings

**Int8 dynamic quantization is the dominant win.** It cut p95 from 5,014 ms to 265 ms and raised accepted throughput from 3.11 to 4.10 requests/s. The baseline was queueing at 4 offered requests/s; the quantized model handled the offered load without a queue. The per-request p50 fell from 3,406 ms to 249 ms.

**Warmup is small and only matters without quantization.** A one-token warmup lowered p95 from 5,014 to 4,916 ms in FP32, within run-to-run noise. With quantization, warmup made no measurable difference.

**SDPA versus eager attention is not a clear win on this CPU.** Eager measured p95 4,856 ms versus 5,014 ms for SDPA. The difference is small and within noise at this sample size. SDPA stays the default because it is the supported optimized path, not because this run proved a gain.

**Inter-op thread control is available but not separately benchmarked.** The default is one inter-op thread; the run did not isolate its effect.

## Quality check

Dynamic int8 quantization can change outputs. Two spot checks produced coherent text:

- “Say hello in one short sentence.” → “Say hello to the world and welcome everyone with open arms.”
- “What is admission control?” → a generic but coherent answer.

This is not a quality benchmark. Do not claim the quantized model is quality-neutral. For a production use, evaluate task accuracy and pin the PyTorch version.

## Decision

- Keep FP32 as the default for quality-sensitive use.
- Use `--quantize` as the first CPU optimization when latency and capacity matter.
- Keep SDPA as the default attention path.
- Keep warmup optional; it helps cold starts but not steady-state throughput.
- The PyTorch dynamic quantization API emits a deprecation warning in torch 2.15. Pin the version and plan a migration.

## Limits

- One host, one model, 40 requests per run.
- No quality benchmark, GPU, larger model, or production workload.
- The 4.10 accepted requests/s equals the offered rate; this run does not measure the quantized capacity ceiling.
- No KV-cache reuse, continuous batching, or streaming.

## Evidence

- `benchmarks/llm-runtime.json`
- `docs/optimization.md`
- `llm_engine/backend.py` (`quantize`, `attn`, `warmup`, `interop_threads`)
