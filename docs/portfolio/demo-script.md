# Five-minute demo script

## Before the demo

```sh
python3 -m venv .venv
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch
.venv/bin/pip install -r requirements.txt
```

Check that ports 8000, 8100, 8101, 8180 are free. The benchmark allocates its own ports, so this is mostly to avoid a local stack.

Set the model:

```sh
export MODEL=Qwen/Qwen2.5-0.5B-Instruct
```

## 0:00–0:40 — Problem and architecture

Show `README.md`, Architecture section.

Say:

> This is a distributed LLM serving system. A gateway routes requests to two PyTorch workers, which batch generation and cache greedy responses. Under load, the queue grew without bound: every request returned 200, but p95 reached 13 seconds. I added admission control, metrics, batching, and two HA deployments.

## 0:40–2:20 — Baseline collapse

```sh
.venv/bin/python -m llm_engine.bench --mode normal \
  --workers 2 --rate 4 --duration 5 --max-tokens 8 \
  --admission-max 0 --output /tmp/llm-demo-base
```

Show:

```sh
python3 - <<'PY'
import json
d = json.load(open("/tmp/llm-demo-base/summary.json"))
print({k: d[k] for k in ("requests", "successes", "failures", "latency_ms", "successful_requests_per_second")})
PY
```

Say:

> Every request succeeds, but p95 is several seconds. That is queue collapse. HTTP 200 does not mean the latency is usable.

## 2:20–3:40 — Admission control

```sh
.venv/bin/python -m llm_engine.bench --mode normal \
  --workers 2 --rate 4 --duration 5 --max-tokens 8 \
  --admission-max 2 --output /tmp/llm-demo-ad2
```

Show the same summary fields.

Say:

> The gateway now rejects excess work with 503 and Retry-After. Accepted p95 is about two seconds, and the run finishes in the workload window. The 503s are the contract, not a bug.

## 3:40–4:20 — Cache and model size

```sh
.venv/bin/python -m llm_engine.bench --mode normal \
  --workers 1 --rate 2 --duration 5 --max-tokens 8 \
  --repeat --routing hash --output /tmp/llm-demo-cache
```

Say:

> The cache hits most repeated prompts and returns in about two milliseconds. The first misses pay full generation. Hash routing keeps the repeated prompt on the same worker.

If time permits, mention the runtime comparison: p95 fell from 5.0 seconds (FP32) to 265 ms (int8) at 4 requests/s with two workers. Quality was spot-checked, not benchmarked.

## 4:20–5:00 — AWS ALB result and limits

Show the AWS multi-host table in `docs/results.md`.

Say:

> On AWS, two gateways and two workers behind an internal ALB completed normal and recovery. Failover exposed 8 502s and 1 504 because the ALB took 15.8 seconds to mark the dead target unhealthy. The last 504 finished at 23 seconds. The fix is client retry or draining. This is benchmark evidence, not a production claim.

## If something fails

- **Model download slow:** wait; the first run downloads about 1 GB.
- **Port in use:** stop the old worker or gateway; the benchmark uses ephemeral ports.
- **Low p95 difference:** the CPU is shared with the load generator; use a quiet machine and 10-second runs.
- **No summary:** read the log in the output directory.

## Do not show

- Raw AWS account IDs, instance IDs, IPs, or ALB DNS names.
- Claims of production readiness, multi-AZ, TLS, streaming, or zero downtime.
