# Distributed LLM Inference Engine

A small distributed serving system for Hugging Face causal language models. It keeps the design of the C++ inference engine: a gateway routes requests to workers, workers batch and cache generations, admission control protects the queue, and a health-checked proxy provides gateway failover.

This is a local CPU system with a real model. It is not a production service.

## Architecture

```
Client -> Gateway (admission, routing, metrics)
              |
              +-> Worker 1 (batching, cache, model) -> Qwen2.5-0.5B
              +-> Worker 2 (batching, cache, model) -> Qwen2.5-0.5B

Gateway 1 --\
             +-> HA Proxy -> Client
Gateway 2 --/
```

- **Gateway**: validates requests, enforces the in-flight limit, routes to a worker, retries another worker on a connection error, and exposes `/stats` and `/metrics`.
- **Worker**: loads the model, batches concurrent requests inside a configurable window, caches deterministic responses, and exposes queue and batch metrics.
- **HA proxy**: polls gateway `/health` every second, round-robins healthy gateways, and retries once on a connection error.

## Quickstart

```sh
python3 -m venv .venv
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch
.venv/bin/pip install -r requirements.txt
```

Start two workers and a gateway:

```sh
MODEL=Qwen/Qwen2.5-0.5B-Instruct
.venv/bin/python -m llm_engine.worker --port 8101 --id worker-1 --model "$MODEL" &
.venv/bin/python -m llm_engine.worker --port 8102 --id worker-2 --model "$MODEL" &
.venv/bin/python -m llm_engine.gateway --port 8100 --workers \
  http://127.0.0.1:8101 http://127.0.0.1:8102 --admission-max 2 &
```

Wait for both workers to load the model, then send a request:

```sh
curl -s http://127.0.0.1:8100/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Explain admission control in one sentence.","max_tokens":16}'
```

## API

`POST /generate`

```json
{
  "request_id": "optional-id",
  "prompt": "Explain admission control in one sentence.",
  "max_tokens": 16,
  "temperature": 0.0,
  "top_p": 1.0
}
```

Response:

```json
{
  "request_id": "optional-id",
  "worker": "worker-1",
  "gateway": "gateway-1",
  "text": "Admission control limits...",
  "tokens": 16,
  "cached": false,
  "queue_wait_ms": 3.2,
  "generation_ms": 1540.1,
  "latency_ms": 1543.3
}
```

`GET /health`, `GET /stats`, `GET /metrics` are available on the gateway and worker.

## Admission control

Set `--admission-max N` on the gateway. `0` disables the limit.

- When `N` requests are in flight, the next request gets HTTP `503` with `Retry-After: 1`.
- `/health`, `/stats`, and `/metrics` are never rejected.
- The limit is per gateway. Two gateways with `N=2` allow up to four in-flight requests; if one gateway dies, the limit drops to two.

## Batching and cache

- The worker waits `--batch-window-ms` (default 10 ms) for more requests, then sends up to `--max-batch-size` prompts through one `model.generate` call.
- Requests are grouped by `(max_tokens, temperature, top_p)` so one request's settings cannot change another's.
- The worker keeps an LRU response cache. It stores only greedy responses (`temperature=0`), keyed by prompt and generation settings.
- This is a response cache, not a KV cache. Different prompts do not share compute.

## Routing

- `least_loaded` (default): pick the worker with the fewest in-flight requests.
- `hash`: consistent-hash the prompt to a worker, which improves cache locality for repeated prompts.
- A worker circuit opens after five request failures or two failed health checks and closes after ten seconds. The gateway retries the next worker once.
- A background watcher polls each worker `/health` every two seconds and removes an unhealthy worker from routing.

## Benchmark

```sh
.venv/bin/python -m llm_engine.bench --mode normal \
  --workers 2 --rate 4 --duration 10 --max-tokens 16 \
  --output /tmp/llm-normal
```

Modes:

- `normal`: one gateway, no admission limit.
- `admission`: one gateway with `--admission-max N`.
- `failover`: two gateways behind the HA proxy; the first gateway is terminated after `--kill-after` seconds.

The runner allocates free ports, starts the processes, waits for health, runs an open-loop load, and writes `summary.json` and `requests.jsonl`.

## Deploy with Docker Compose

The Compose stack runs two workers, two gateways, and the HA proxy. Only the proxy port is published.

```sh
docker compose -f deploy/docker-compose.yml up -d --build
curl -s http://127.0.0.1:8180/health
docker compose -f deploy/docker-compose.yml run --rm bench \
  python -m llm_engine.bench --target http://proxy:8180 \
  --rate 2 --duration 10 --max-tokens 8 --output /results/manual
docker compose -f deploy/docker-compose.yml down -v
```

## Deploy on AWS

Two controllers are included:

- `deploy/aws_single_host.py` launches one `c6i.2xlarge` host, installs Docker, builds the full Compose stack, runs normal/failover/recovery, collects the results, and terminates the host.
- `deploy/aws_multihost.py` launches two `c6i.large` gateways, two `c6i.xlarge` workers, one `c6i.large` load generator, and an internal ALB. It runs normal/failover/recovery, replaces the failed gateway, collects evidence, and removes every resource.

```sh
python3 deploy/aws_single_host.py          # preflight only
python3 deploy/aws_single_host.py --run

python3 deploy/aws_multihost.py            # preflight only
python3 deploy/aws_multihost.py --run
python3 deploy/aws_multihost.py --run --workers 8 --rates 4 8 12 \
  --duration 10 --admission-max 0
```

Both controllers use a $1.50 cap. Evidence is written under `deploy/runs/<run-id>/`, which is ignored because it contains the temporary SSH key and instance state.

## Results

Local, CPU-only, Qwen2.5-0.5B-Instruct, 2 workers, 16 output tokens.

| Run | Successes | 503 | p95 | Accepted req/s |
| --- | ---: | ---: | ---: | ---: |
| Baseline, no admission | 40 / 40 | 0 | 13,051 ms | 2.07 |
| Admission limit 2 | 10 / 40 | 30 | 2,031 ms | 0.93 |
| Admission limit 4 | 12 / 40 | 28 | 4,060 ms | 0.99 |

The baseline returned HTTP 200 for every request but took 19.3 seconds to drain a 10-second workload. Admission limit 2 cut p95 to 2.03 seconds and finished in the workload window.

### Runtime optimizations

Two workers, 4 requests/s, 10 seconds, 8 tokens, no admission.

| Config | p50 | p95 | Accepted req/s | Tokens/s |
| --- | ---: | ---: | ---: | ---: |
| FP32, SDPA | 3,406 ms | 5,014 ms | 3.11 | 24.9 |
| **Int8 dynamic, SDPA** | **249 ms** | **265 ms** | **4.10** | **31.8** |
| FP32, eager | 3,075 ms | 4,856 ms | 3.12 | 24.9 |

Dynamic int8 quantization cut p95 by about 19x and raised accepted throughput from 3.11 to 4.10 requests/s. The baseline was queueing at 4 requests/s; the quantized model handled the offered load. Warmup helped cold starts slightly; SDPA versus eager was within noise. Quality was spot-checked, not benchmarked. See [runtime optimizations](docs/runtime-optimizations.md).

On AWS, one `c6i.2xlarge` host ran the full Compose stack at 2 requests/s. Normal, failover, and recovery each completed 20/20 requests with no 502/503/504; the run cost $0.024164.

The multi-host internal-ALB run used two gateways, two workers, and a load generator. Normal and recovery completed 40/40; the failover phase completed 31/40 with 8×502 and 1×504. TargetHealth reported the dead gateway unhealthy after **15.8 s**, and the last 504 completed about **23.0 s** after the load marker. A follow-up with three retries recovered 14 of 16 retried requests and left 2 exhausted 503s, but p95 rose to 10.8 s because retries waited for the 10 s timeout. The two-worker ALB runs cost $0.181032.

The 8-worker capacity run used two gateways, eight workers on separate hosts, and no admission limit. At 12 offered requests/s it completed 120/120 with p95 **1,504 ms** and 11.17 accepted requests/s. Failover at 12 requests/s completed 116/120; the retry client recovered 33 of 37 retried requests. The run cost $0.348115. Total LLM AWS spend is **$1.186805**; combined with the ONNX project, **$4.752693 of $10**, about **$5.25** remains. Full results: [docs/results.md](docs/results.md).

### 8-worker capacity

Two gateways, eight workers on separate hosts, no admission limit, 10 seconds per rate, 8 tokens.

| Offered req/s | Successes | p95 | Accepted req/s |
| ---: | ---: | ---: | ---: |
| 4 | 40 / 40 | 1,306 ms | 3.71 |
| 8 | 80 / 80 | 1,463 ms | 7.38 |
| 12 | 120 / 120 | 1,504 ms | 11.17 |

Across separate hosts, 8 workers served 11.17 requests/s at p95 1.5 s. On one host, the 1/2/4-worker sweep got worse because the CPU was saturated. Distributed scaling depends on giving each worker its own CPU.

## Portfolio

See [docs/portfolio](docs/portfolio/README.md) for the case study, resume bullets, eight-session learning path, and five-minute demo script.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -t .
.venv/bin/ruff check llm_engine tests deploy
```

The suite covers the cache, routing, circuit breaker, worker batching, queue-full behavior, admission rejection, gateway retry, and HA proxy failover. It uses the deterministic fake backend, so it does not load the model.

## Configuration

| Setting | Default | Meaning |
| --- | ---: | --- |
| Gateway `--admission-max` | 0 | Per-gateway in-flight limit; 0 disables |
| Gateway `--routing` | least_loaded | `least_loaded` or `hash` |
| Gateway `--timeout` | 120 s | Upstream request timeout |
| Gateway `--watch-interval` | 2 s | Worker health poll interval; 0 disables |
| Worker `--max-batch-size` | 8 | Maximum prompts per generation batch |
| Worker `--batch-window-ms` | 10 | Time to collect a batch |
| Worker `--cache-capacity` | 256 | LRU entries; 0 disables |
| Worker `--threads` | 4 | Torch CPU threads |
| Worker `--quantize` | off | Dynamic int8 linear quantization |
| Worker `--attn` | sdpa | `sdpa` or `eager` attention |
| Worker `--warmup` | off | One-token warmup after model load |
| Worker `--interop-threads` | 1 | Torch inter-op threads |
| Worker `--backend` | hf | `hf` or `fake` |

## Limits

- One local host; workers and load generator share CPU.
- Qwen2.5-0.5B only; no larger model or GPU test.
- No streaming, KV-cache reuse, or continuous batching across different generation settings.
- No TLS, authentication, public endpoint, or global admission.
- No AWS ALB, multi-AZ, or multi-host run.
- The results are benchmark evidence, not a production capacity claim.

## Layout

```
llm_engine/
  backend.py     Hugging Face and fake backends
  cache.py       LRU response cache
  gateway.py     admission, routing, retries, metrics
  ha_proxy.py    health-checked gateway proxy
  metrics.py     counters and latency windows
  protocol.py    request validation
  router.py      least-loaded and consistent-hash routing
  worker.py      batching, cache, HTTP API
  bench.py       local benchmark runner
tests/           unit and API tests
docs/results.md  measured results and interpretation
```

## License

MIT. See [LICENSE](LICENSE).
