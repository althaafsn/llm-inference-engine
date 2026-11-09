# Design

## Goal

Serve a small Hugging Face causal language model through a gateway and a pool of workers, with the same reliability ideas as the C++ inference engine: batching, caching, admission control, circuit breaking, metrics, and gateway failover.

The goal is to make the system-level tradeoffs visible and measurable, not to maximize tokens per second.

## Non-goals

- Production TLS, authentication, quotas, or a public endpoint.
- GPU optimization, paged attention, or a KV-cache server.
- Multi-AZ or multi-host deployment.
- A global admission limit across gateways.
- Training or fine-tuning.

## Why Python and transformers

The C++ engine exists and is the stronger performance artifact. This project asks a different question: what changes when the unit of work is a token stream instead of one forward pass?

Python and `transformers` make that question cheap to test with a real model. The process boundary, HTTP contracts, admission behavior, routing, and failover are the same. A future C++ or llama.cpp backend can replace `HFBackend` without changing the gateway.

The alternatives were rejected for scope:

- **vLLM** brings continuous batching and paged attention, but needs a GPU for useful throughput and hides the scheduling logic this project is meant to expose.
- **llama.cpp** is a better CPU runtime, but building and wrapping it would consume the session before the reliability work was measured.
- **A fake-only backend** would prove the tests but not the real model behavior.

## Components

### Worker

One process per worker. It owns the model, the tokenizer, the batch scheduler, the cache, and the worker metrics.

The worker is the only process that touches the model. It serializes batches: one `model.generate` call at a time per worker, because the model is not thread-safe and CPU inference is already saturated.

### Gateway

One process per gateway. It owns routing, admission, retries, the circuit breaker, and the gateway metrics. It does not load the model.

### HA proxy

A local test harness in front of two gateways. It polls `/health` every second, round-robins healthy gateways, and retries once on a connection error. It is not a production load balancer.

## Request lifecycle

1. The client posts `/generate` to the gateway.
2. The gateway validates the JSON and generation parameters.
3. If admission is enabled and the in-flight count is at the limit, the gateway returns `503` with `Retry-After: 1`. The request never reaches a worker.
4. The gateway chooses a worker by least-loaded or consistent-hash routing.
5. The gateway posts to the worker and increments its in-flight count.
6. The worker checks the response cache for a greedy request. A hit returns immediately.
7. A miss creates a future, puts the request in the queue for its `(max_tokens, temperature, top_p)` group, and wakes the scheduler.
8. The scheduler waits for the batch window, drains up to `max_batch_size`, and runs one `model.generate` call in a thread.
9. The worker decodes each row, stores the greedy result in the cache, and resolves each future.
10. The gateway records the result and returns it. On a connection error, it marks the worker failed and retries the next candidate once.

## Batching

The batch scheduler groups requests by generation parameters. This is necessary because `model.generate` applies one `max_new_tokens`, `temperature`, and `top_p` to the whole batch. Grouping prevents one request's settings from changing another's.

The scheduler waits `batch_window_ms` after the first request. A larger window collects larger batches and improves throughput, but adds latency to the first request. The default is 10 ms, matching the C++ engine's batch deadline.

Left padding is used so prompts of different lengths can share a batch. A request with a different parameter set waits for its own batch.

The worker does not implement continuous batching. A batch runs to completion before the next batch starts. This is a deliberate simplification; continuous batching would require a token-level scheduler and a KV cache.

## Cache

The cache is an LRU keyed by prompt, `max_tokens`, `temperature`, and `top_p`. It stores only `temperature=0` responses because sampled responses are not deterministic.

This is a **response cache**, not a KV cache. It helps repeated prompts and identical requests. It does not share prefill or attention state across different prompts.

## Admission control

The gateway holds an in-flight counter and a lock. Before forwarding, it checks the limit and increments; after the response, it decrements. The check and increment happen under the same lock, so two requests cannot both take the last slot.

The limit is per gateway. With two gateways at 2, the configured total is 4. During failover, the survivor keeps its limit, so total capacity drops. A global limit would need a shared counter or coordinator, with its own consistency and failure handling.

Admission is a load-shedding contract, not a capacity increase. It protects the latency of accepted requests by rejecting excess work with an explicit retryable status.

## Routing and circuit breaking

`least_loaded` picks the worker with the fewest in-flight requests. It adapts to slow or stuck workers and is the default.

`hash` maps the prompt to a worker through a consistent-hash ring with 100 virtual nodes per worker. It keeps repeated prompts on the same worker, which makes the response cache effective. When a worker is removed, only its keys move.

A worker circuit opens after five request failures or two failed health checks. A background watcher polls each worker `/health` every two seconds; two consecutive failures remove the worker from routing, and a success restores it. The gateway retries the next candidate once. A success resets the request failure count.

The retry is bounded to one alternate worker. More attempts would amplify load during an outage.

## Failover

The HA proxy polls each gateway's `/health` every second. On a connection error, it marks the upstream unhealthy and retries the next healthy gateway once.

In the measured run, one request was served by gateway 1 before the kill; all later requests were served by gateway 2, with no client-visible 502/503/504. The proxy's one-second poll is an upper bound on detection; the connection-error retry is what removes the client-visible failure.

This is not an ALB result. An ALB health check has a different interval, threshold, and failure mode; the C++ project measured that separately.

## Metrics

Both processes expose JSON `/stats` and Prometheus `/metrics`:

- accepted, rejected, completed, failed
- cache hits
- retries
- tokens
- latency p50/p95/p99
- queue depth and in-flight count
- per-worker circuit state

The worker also reports the mean batch size for the fake backend and queue depth for the real backend.

## Failure analysis

The baseline run returned HTTP 200 for all 40 requests but took 19.3 seconds to drain a 10-second workload, with p95 at 13.1 seconds. The queue was unbounded. This is the same failure the C++ engine showed: success status does not mean usable latency.

Admission limit 2 cut p95 to 2.03 seconds and shed 75% of requests. Limit 4 admitted slightly more work but doubled p95. The limit is a policy choice, not a fixed optimum.

The cache run showed the expected split: 10 of 12 repeated requests hit, p50 about 2 ms, while the misses paid full inference. Hash routing is what keeps the repeated prompt on the same worker.

## Tradeoffs

- **Latency vs goodput**: a lower admission limit improves p95 but accepts fewer requests.
- **Batch window vs first-token latency**: a larger window improves throughput but delays the first request.
- **Cache vs correctness**: only greedy responses are cached; sampled responses always run.
- **Local proxy vs ALB**: the proxy is fast and simple but has no draining, TLS, or multi-AZ behavior.
- **Python vs C++**: faster to build and measure, slower per token.

## Next

1. Add streaming and measure time to first token.
2. Add a shared admission limit across gateways.
3. Replace the local proxy with an internal ALB on separate hosts.
4. Add a KV cache and continuous batching for a stronger throughput result.
5. Test multi-AZ and a real instance kill.
