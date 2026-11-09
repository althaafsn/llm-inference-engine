# Learning path

Eight sessions, about 60–90 minutes each. Do them in order. The goal is to explain each decision and its evidence without notes.

## Before you start

```sh
python3 -m venv .venv
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -t .
```

The tests use the fake backend and do not load the model. The benchmark downloads `Qwen/Qwen2.5-0.5B-Instruct` on first use.

## Session 1 — Run the system and map the request path

**Read:** `README.md` (Architecture), `llm_engine/gateway.py` (`generate`, `_forward`), `llm_engine/worker.py` (`generate`, `_scheduler`).

**Run:**

```sh
.venv/bin/python -m llm_engine.bench --mode normal \
  --workers 2 --rate 2 --duration 5 --max-tokens 8 \
  --output /tmp/llm-learn-01
```

**Prove:** open `/tmp/llm-learn-01/summary.json`. Write down requests, successes, p50, p95.

**Answer:**

1. Which process loads the model?
2. What does the gateway do before it forwards a request?
3. What does the gateway not do?

## Session 2 — Batching and cache

**Read:** `llm_engine/worker.py` (`_scheduler`, `_run_batch`), `llm_engine/cache.py`, `llm_engine/backend.py`.

**Run:** the Session 1 command twice: once with `--repeat --routing hash`, once with `--cache-capacity 0 --repeat --routing hash`. Then run once with `--quantize` and compare p95 and tokens/s with the FP32 run.

**Prove:** compare cache hits, p50, p95, and tokens/s. Explain why the no-cache run is slower and why int8 is faster.

**Answer:**

1. Why does the scheduler group requests by generation settings?
2. Why does a larger batch improve throughput but hurt the first request?
3. Why is the cache only for greedy responses?
4. What did int8 quantization change, and what did you not measure?

## Session 3 — Admission and adaptive control

**Read:** `llm_engine/gateway.py` (`generate`, `_adaptive_loop`, `_update_limit`), `llm_engine/adaptive.py`, `docs/optimization.md`.

**Run:** two 10-second runs at 4 requests/s: `--admission-max 0` and `--admission-max 2`. Then run `--admission-max 6 --admission-adaptive --admission-min 1 --admission-target-p95-ms 1000 --admission-interval-ms 250` for 20 seconds.

**Prove:** record accepted, 503s, p95, and `limit_changes` from `gateway_stats`. Compare with the report.

**Answer:**

1. Why does the baseline return 200 but still fail?
2. Why is admission a latency-versus-goodput choice?
3. Why did adaptive not beat static 2 in this workload?

## Session 4 — Metrics and retry

**Read:** `llm_engine/metrics.py`, `llm_engine/bench.py` (`parse_retry_after`, `retry_delay_seconds`, `run_load`).

**Run:** a run with `--admission-max 2 --retry-attempts 3 --retry-backoff-ms 100 --retry-jitter 0.2`.

**Prove:** record `retry.retried_requests`, `retry.retry_successes`, `retry.retry_exhausted`, and p95. Note that p95 includes the wait between attempts.

**Answer:**

1. What does `Retry-After` tell the client?
2. Why add jitter?
3. Why does retry not create capacity?

## Session 5 — Local HA

**Read:** `llm_engine/ha_proxy.py`, `README.md` (Deploy with Docker Compose), `docs/results.md` (local failover).

**Run:**

```sh
.venv/bin/python -m llm_engine.bench --mode failover \
  --workers 2 --rate 1 --duration 10 --max-tokens 8 \
  --output /tmp/llm-learn-05
```

**Prove:** record successes, errors, p95, and the `gateway` field in `requests.jsonl`.

**Answer:**

1. Why does the proxy fail over without client-visible errors?
2. What happens to the per-gateway admission total during failover?
3. Why is this not an ALB result?

## Session 6 — Docker single-host deployment

**Read:** `deploy/Dockerfile`, `deploy/docker-compose.yml`, `deploy/aws_single_host.py`.

**Run:**

```sh
docker compose -f deploy/docker-compose.yml up -d --build
curl -s http://127.0.0.1:8180/health
docker compose -f deploy/docker-compose.yml run --rm bench \
  python -m llm_engine.bench --target http://proxy:8180 \
  --rate 2 --duration 10 --max-tokens 8 --output /results/manual
docker compose -f deploy/docker-compose.yml down -v
```

**Prove:** record the health output, the bench summary, and the cleanup.

**Answer:**

1. Why does the worker bind to `0.0.0.0` inside the container?
2. Why does only the proxy publish a port?
3. What does the `hf-cache` volume keep?

## Session 7 — AWS multi-host ALB

**Read:** `deploy/aws_multihost.py`, `docs/results.md` (AWS multi-host section), `deploy/README.md`.

**Run:** only the read-only preflight:

```sh
python3 deploy/aws_multihost.py
```

The `--run` path creates billable resources. Do not run it without a budget and cleanup review.

**Prove:** write the run order from memory: preflight, groups, five instances, setup, workers, gateways, ALB, normal, failover, recovery, cleanup. Mark where state is saved.

**Answer:**

1. Why did the ALB take 15.8 s to mark the target unhealthy?
2. Why were there both 502 and 504 responses?
3. Why did the last 504 finish about 23 s after the load marker?

## Session 8 — Interview drill

**Read:** `docs/portfolio/case-study.md` and `docs/portfolio/resume-entry.md`.

**Run:** record a two-minute explanation: problem, design, result, failure, limit, next step.

**Prove:** answer all 14 questions in the resume entry without notes. Mark the three weakest and reread those code paths.

**Answer:**

1. What is the strongest number, and what is its context?
2. What is the most interesting failure, and what did you learn?
3. What did you personally do, and where did AI help?

## After the eight sessions

- Use the primary resume entry.
- Link the repository and the case study.
- Revisit one session before each interview. Do not add features just before applying.
