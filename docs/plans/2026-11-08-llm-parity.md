# LLM engine parity plan

Date: 2026-11-08

## Objective

Bring the LLM engine to the same evidence level as the C++ ONNX engine:

1. A measured optimization campaign (threads, batching, admission, routing, cache, model size).
2. Adaptive admission and a retry-aware client, matching the C++ features.
3. A real multi-host AWS run with an internal ALB, normal/failover/recovery, cost, and cleanup.
4. Resume materials: case study, bullets, learning path, and demo script.

## Non-goals

- Production TLS, authentication, quotas, or a public endpoint.
- GPU tuning, continuous batching, paged attention, or KV-cache reuse.
- Model quality evaluation or fine-tuning.
- Multi-tenant isolation.

## Verified facts

- The LLM repo is public at `althaafsn/llm-inference-engine`.
- 25 tests pass; Ruff and CI are green.
- Local results: baseline p95 13.1 s; admission limit 2 p95 2.03 s; cache hit p50 1.9 ms.
- Single-host AWS Docker run: normal 20/20 (p95 510 ms), failover 20/20 from gateway 2 (p95 724 ms), recovery 20/20 (p95 595 ms); cost $0.024164.
- The C++ project proves the ALB pattern: 2 gateways, 2 workers, load generator, internal ALB, 16.576 s target-health window, 180x502/39x504.
- Remaining shared AWS budget: about $6.18 of $10.

## Assumptions

- The same account, region, AMI, and default VPC remain available.
- `c6i.xlarge` has enough RAM for one 0.5B float32 worker (about 2 GB plus torch).
- A multi-host ALB run costs about $0.35 for 30 minutes and stays below a $1.50 cap.

## Phase 1 — Optimization matrix (local)

Add:

- `llm_engine/adaptive.py`: pure AIMD policy, 20-sample window, 20% decrease above 110% of target, +1 increase below 75%, deadband otherwise.
- Gateway flags: `--admission-adaptive`, `--admission-min`, `--admission-target-p95-ms`, `--admission-interval-ms`.
- Gateway latency window and p95 sampling for the controller.
- Bench retry flags: `--retry-attempts`, `--retry-backoff-ms`, `--retry-max-backoff-ms`, `--retry-jitter`; retry on 502/503/504 and connection errors; honor `Retry-After`.
- `llm_engine/bench_matrix.py`: runs a named list of configurations and writes one combined JSON.

Matrix dimensions:

| Dimension | Values |
| --- | --- |
| Worker threads | 2, 4, 8 |
| Batch window | 0, 10, 25 ms |
| Admission limit | 0, 1, 2, 4 |
| Routing | least_loaded, hash |
| Cache | unique, repeated |
| Model | Qwen2.5-0.5B, SmolLM2-135M |

Not every combination. Use a base configuration and vary one dimension at a time. Each run is 5 seconds at 2 requests/s, 8 output tokens.

Acceptance:

- Each run writes status counts, p50/p95/p99, accepted throughput, tokens/s, cache hits, and shed rate.
- The report names the best thread count, batch window, admission limit, and routing policy for this host.
- Tests cover the AIMD policy and retry delay.
- `docs/optimization.md` records the tables and decisions.

## Phase 2 — Multi-host AWS ALB

Add `deploy/aws_multihost.py`:

- Preflight: account, AMI, default public subnet, ELB permissions, no active tagged resources.
- 2 gateway hosts `c6i.large`, 2 worker hosts `c6i.xlarge`, 1 load host `c6i.large`.
- Security groups: SSH from owner; ALB to gateway 8100; gateway to worker 8101; load to ALB 8100.
- Internal ALB, target group `HTTP:8100 /health`, listener, 5 s interval, 2 healthy/2 unhealthy.
- Build the Docker image on each host in parallel, or install Python directly if the image build is slower than the budget allows.
- Run normal 20 s, failover 20 s with gateway 1 terminated, recovery 20 s with a replacement gateway.
- Collect summaries, request rows, target health, and container logs.
- Clean up ALB, listener, target group, instances, volumes, security groups, key pair.
- Cap: `$1.50`; expected about `$0.35`.

Acceptance:

- All resources are removed; read-only checks return empty.
- The result records the ALB target-health time and the client-visible 502/504 window separately.
- `docs/results.md` and `README.md` state the result and the limits.

## Phase 3 — Resume materials

Add `docs/portfolio/`:

- `case-study.md`: problem, design, optimization table, ALB result, failure analysis, limits.
- `resume-entry.md`: 3-4 bullets, evidence map, "do not claim" list, defensibility questions.
- `learning-path.md`: 8 sessions tied to the LLM code.
- `demo-script.md`: 5-minute local demo.

## Risks

- Model load and Docker build on five hosts can exceed the run window. Build in parallel and watch the 45-minute limit.
- The ALB health-check floor repeats the C++ 16.6 s weakness; measure it, do not hide it.
- Cache contamination across phases; use per-phase prompt prefixes.
- Port collisions on the local host; always allocate free ports.
- The 0.5B model on CPU is slow; caps and rates must match measured capacity.

## Verification

- Unit and API tests pass; Ruff passes; CI is green.
- Every benchmark number traces to a JSON artifact.
- AWS cleanup is verified with read-only calls.
- The resume material states the fork/provenance, the measured limits, and the AI-assisted implementation.
