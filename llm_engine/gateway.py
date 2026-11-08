"""Gateway: routing, admission control, retries, and metrics."""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from aiohttp import ClientError, ClientSession, web

from .metrics import Metrics
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
        routing: str = "least_loaded",
        timeout: float = 120.0,
        failure_threshold: int = 5,
        open_seconds: float = 10.0,
    ):
        self.gateway_id = gateway_id
        self.admission_max = max(0, admission_max)
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

    async def stop(self) -> None:
        if self.session:
            await self.session.close()

    async def generate(self, data: dict) -> dict:
        request = parse_generate_request(data)
        admitted = False
        if self.admission_max > 0:
            async with self._lock:
                if self.inflight >= self.admission_max:
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
                "admission_max": self.admission_max,
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
    parser.add_argument("--routing", choices=("hash", "least_loaded"), default="least_loaded")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--failure-threshold", type=int, default=5)
    parser.add_argument("--open-seconds", type=float, default=10.0)
    args = parser.parse_args(argv)
    gateway = Gateway(
        gateway_id=args.id,
        worker_urls=args.workers,
        admission_max=args.admission_max,
        routing=args.routing,
        timeout=args.timeout,
        failure_threshold=args.failure_threshold,
        open_seconds=args.open_seconds,
    )
    web.run_app(create_app(gateway), host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
