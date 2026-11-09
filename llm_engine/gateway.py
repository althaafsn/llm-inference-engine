"""Gateway: routing, admission control, retries, and metrics."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import deque

from aiohttp import ClientError, ClientSession, web

from .adaptive import next_limit
from .metrics import Metrics, percentile
from .protocol import parse_generate_request
from .router import Router


class AdmissionRejected(RuntimeError):
    pass


class UpstreamError(RuntimeError):
    pass


class Gateway:
    def __init__(
        self,
        gateway_id: str,
        worker_urls: list[str],
        admission_max: int = 0,
        admission_adaptive: bool = False,
        admission_min: int = 1,
        admission_target_p95_ms: float = 400.0,
        admission_interval_ms: float = 1000.0,
        routing: str = "least_loaded",
        timeout: float = 120.0,
        failure_threshold: int = 5,
        open_seconds: float = 10.0,
        watch_interval: float = 2.0,
    ):
        self.gateway_id = gateway_id
        self.admission_max = max(0, admission_max)
        self.admission_min = (
            max(1, min(admission_min, self.admission_max)) if self.admission_max else 0
        )
        self.admission_adaptive = bool(admission_adaptive and self.admission_max > 0)
        self.admission_limit = self.admission_max
        self.admission_target_p95_ms = max(1.0, admission_target_p95_ms)
        self.admission_interval = max(0.1, admission_interval_ms / 1000.0)
        self.latency_window: deque[float] = deque(maxlen=256)
        self.observed_p95_ms = -1.0
        self.limit_changes = 0
        self._adaptive_task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None
        self.watch_interval = max(0.0, watch_interval)
        self.timeout = timeout
        self.router = Router(
            worker_urls,
            policy=routing,
            failure_threshold=failure_threshold,
            open_seconds=open_seconds,
        )
        self.metrics = Metrics()
        self.inflight = 0
        self._lock = asyncio.Lock()
        self.session: ClientSession | None = None

    async def start(self) -> None:
        self.session = ClientSession()
        if self.admission_adaptive:
            self._adaptive_task = asyncio.create_task(self._adaptive_loop())
        if self.watch_interval > 0:
            self._watch_task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        if self._adaptive_task:
            self._adaptive_task.cancel()
            try:
                await self._adaptive_task
            except asyncio.CancelledError:
                pass
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
        if self.session:
            await self.session.close()

    async def _watch_loop(self) -> None:
        while True:
            await asyncio.sleep(self.watch_interval)
            await self._check_workers()

    async def _check_workers(self) -> None:
        if self.session is None:
            return
        for worker in self.router.workers:
            try:
                async with self.session.get(
                    worker.url + "/health", timeout=min(2.0, self.timeout)
                ) as response:
                    healthy = response.status == 200
                    if healthy:
                        body = await response.json()
                        healthy = body.get("status") == "ok"
            except (TimeoutError, ClientError, ValueError):
                healthy = False
            self.router.mark_health(worker, healthy)

    async def _adaptive_loop(self) -> None:
        while True:
            await asyncio.sleep(self.admission_interval)
            self._update_limit()

    def _update_limit(self) -> None:
        samples = len(self.latency_window)
        if samples == 0:
            return
        observed = percentile(list(self.latency_window), 0.95) or 0.0
        self.observed_p95_ms = observed
        new_limit = next_limit(
            self.admission_limit,
            observed,
            samples,
            self.admission_min,
            self.admission_max,
            self.admission_target_p95_ms,
        )
        if new_limit != self.admission_limit:
            self.admission_limit = new_limit
            self.limit_changes += 1

    async def generate(self, data: dict) -> dict:
        request = parse_generate_request(data)
        admitted = False
        if self.admission_limit > 0:
            async with self._lock:
                if self.inflight >= self.admission_limit:
                    self.metrics.record_rejected()
                    raise AdmissionRejected("admission limit reached")
                self.inflight += 1
                admitted = True
        try:
            self.metrics.record_accepted()
            payload = {
                "prompt": request.prompt,
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
                "top_p": request.top_p,
                "request_id": data.get("request_id"),
            }
            return await self._forward(payload)
        finally:
            if admitted:
                async with self._lock:
                    self.inflight -= 1

    async def _forward(self, payload: dict) -> dict:
        if self.session is None:
            raise UpstreamError("gateway is not started")
        candidates = self.router.candidates(payload["prompt"])
        last_error: Exception | None = None
        for attempt, worker in enumerate(candidates[:2]):
            if attempt:
                self.metrics.record_retry()
            worker.inflight += 1
            started = time.monotonic()
            try:
                async with self.session.post(
                    worker.url + "/generate", json=payload, timeout=self.timeout
                ) as response:
                    body = await response.text()
                    if response.status != 200:
                        raise UpstreamError(
                            f"worker {worker.url} returned {response.status}: {body[:200]}"
                        )
                    result = json.loads(body)
                self.router.record_success(worker)
                latency_ms = result.get("latency_ms")
                if not isinstance(latency_ms, (int, float)):
                    latency_ms = (time.monotonic() - started) * 1000.0
                self.metrics.record_completed(
                    float(latency_ms),
                    int(result.get("tokens", 0)),
                    bool(result.get("cached", False)),
                )
                self.latency_window.append((time.monotonic() - started) * 1000.0)
                return {**result, "gateway": self.gateway_id}
            except (TimeoutError, ClientError, ValueError, UpstreamError) as exc:
                self.router.record_failure(worker)
                self.metrics.record_failed()
                last_error = exc
            finally:
                worker.inflight = max(0, worker.inflight - 1)
        raise UpstreamError(str(last_error) if last_error else "no workers available")

    def snapshot(self) -> dict:
        return self.metrics.snapshot(
            {
                "gateway": self.gateway_id,
                "admission_limit": self.admission_limit,
                "admission_max": self.admission_max,
                "admission_min": self.admission_min,
                "admission_adaptive": self.admission_adaptive,
                "admission_target_p95_ms": self.admission_target_p95_ms,
                "observed_p95_ms": self.observed_p95_ms,
                "admission_samples": len(self.latency_window),
                "limit_changes": self.limit_changes,
                "watch_interval": self.watch_interval,
                "inflight": self.inflight,
                "workers": self.router.snapshot(),
            }
        )


def create_app(gateway: Gateway) -> web.Application:
    app = web.Application()

    async def generate(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        try:
            result = await gateway.generate(data)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except AdmissionRejected as exc:
            return web.json_response({"error": str(exc)}, status=503, headers={"Retry-After": "1"})
        except UpstreamError as exc:
            return web.json_response({"error": str(exc)}, status=502)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response(result)

    async def health(_: web.Request) -> web.Response:
        workers = gateway.router.snapshot()
        available = [worker for worker in workers if not worker["circuit_open"]]
        return web.json_response(
            {
                "status": "ok" if available else "degraded",
                "gateway": gateway.gateway_id,
                "workers": workers,
            },
            status=200 if available else 503,
        )

    async def stats(_: web.Request) -> web.Response:
        return web.json_response(gateway.snapshot())

    async def metrics(_: web.Request) -> web.Response:
        text = gateway.metrics.prometheus({"gateway": gateway.gateway_id})
        text += f'llm_inflight{{gateway="{gateway.gateway_id}"}} {gateway.inflight}\n'
        text += f'llm_admission_limit{{gateway="{gateway.gateway_id}"}} {gateway.admission_max}\n'
        return web.Response(text=text, content_type="text/plain")

    app.router.add_post("/generate", generate)
    app.router.add_get("/health", health)
    app.router.add_get("/stats", stats)
    app.router.add_get("/metrics", metrics)

    async def start(_: web.Application) -> None:
        await gateway.start()

    async def stop(_: web.Application) -> None:
        await gateway.stop()

    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--id", default="gateway-1")
    parser.add_argument("--workers", nargs="+", required=True)
    parser.add_argument("--admission-max", type=int, default=0)
    parser.add_argument("--admission-adaptive", action="store_true")
    parser.add_argument("--admission-min", type=int, default=1)
    parser.add_argument("--admission-target-p95-ms", type=float, default=400.0)
    parser.add_argument("--admission-interval-ms", type=float, default=1000.0)
    parser.add_argument("--routing", choices=("hash", "least_loaded"), default="least_loaded")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--failure-threshold", type=int, default=5)
    parser.add_argument("--open-seconds", type=float, default=10.0)
    parser.add_argument("--watch-interval", type=float, default=2.0)
    args = parser.parse_args(argv)
    gateway = Gateway(
        gateway_id=args.id,
        worker_urls=args.workers,
        admission_max=args.admission_max,
        admission_adaptive=args.admission_adaptive,
        admission_min=args.admission_min,
        admission_target_p95_ms=args.admission_target_p95_ms,
        admission_interval_ms=args.admission_interval_ms,
        routing=args.routing,
        timeout=args.timeout,
        failure_threshold=args.failure_threshold,
        open_seconds=args.open_seconds,
        watch_interval=args.watch_interval,
    )
    web.run_app(create_app(gateway), host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
