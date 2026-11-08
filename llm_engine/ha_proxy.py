"""Round-robin HTTP proxy with health checks for local gateway HA tests."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from aiohttp import ClientError, ClientSession, web


@dataclass
class Upstream:
    url: str
    healthy: bool = False
    requests: int = 0
    failures: int = 0
    last_error: str | None = None


class HAProxy:
    def __init__(self, upstreams: list[str], health_interval: float = 1.0, timeout: float = 130.0):
        self.upstreams = [Upstream(url=url) for url in upstreams]
        self.health_interval = health_interval
        self.timeout = timeout
        self.session: ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._index = 0

    async def start(self) -> None:
        self.session = ClientSession()
        self._task = asyncio.create_task(self._health_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.session:
            await self.session.close()

    async def _health_loop(self) -> None:
        while True:
            await self._check_all()
            await asyncio.sleep(self.health_interval)

    async def _check_all(self) -> None:
        if self.session is None:
            return
        for upstream in self.upstreams:
            try:
                async with self.session.get(
                    upstream.url + "/health", timeout=min(3.0, self.timeout)
                ) as response:
                    upstream.healthy = response.status == 200
                    if not upstream.healthy:
                        upstream.last_error = f"health status {response.status}"
            except (TimeoutError, ClientError) as exc:
                upstream.healthy = False
                upstream.last_error = str(exc)

    def _healthy(self) -> list[Upstream]:
        return [upstream for upstream in self.upstreams if upstream.healthy]

    def _pick(self) -> Upstream | None:
        healthy = self._healthy()
        if not healthy:
            return None
        self._index = (self._index + 1) % len(healthy)
        return healthy[self._index]

    async def forward(self, request: web.Request) -> web.Response:
        if self.session is None:
            return web.json_response({"error": "proxy is not started"}, status=503)
        body = await request.read()
        headers = {}
        if request.headers.get("Content-Type"):
            headers["Content-Type"] = request.headers["Content-Type"]
        attempts = 0
        last_error = "no healthy upstream"
        while attempts < 2:
            upstream = self._pick()
            if upstream is None:
                break
            attempts += 1
            upstream.requests += 1
            try:
                async with self.session.request(
                    request.method,
                    upstream.url + request.path_qs,
                    data=body,
                    headers=headers,
                    timeout=self.timeout,
                ) as response:
                    payload = await response.read()
                    if response.status in (502, 504):
                        upstream.failures += 1
                        upstream.last_error = f"status {response.status}"
                        last_error = upstream.last_error
                        continue
                    out_headers = {}
                    if "Retry-After" in response.headers:
                        out_headers["Retry-After"] = response.headers["Retry-After"]
                    return web.Response(
                        status=response.status,
                        body=payload,
                        content_type=response.content_type,
                        headers=out_headers,
                    )
            except (TimeoutError, ClientError) as exc:
                upstream.failures += 1
                upstream.last_error = str(exc)
                last_error = str(exc)
                upstream.healthy = False
        return web.json_response({"error": last_error}, status=503)

    def snapshot(self) -> dict:
        return {
            "upstreams": [
                {
                    "url": upstream.url,
                    "healthy": upstream.healthy,
                    "requests": upstream.requests,
                    "failures": upstream.failures,
                    "last_error": upstream.last_error,
                }
                for upstream in self.upstreams
            ]
        }


def create_app(proxy: HAProxy) -> web.Application:
    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        healthy = proxy._healthy()
        status = 200 if healthy else 503
        return web.json_response(
            {"status": "ok" if healthy else "down", **proxy.snapshot()}, status=status
        )

    async def stats(_: web.Request) -> web.Response:
        return web.json_response(proxy.snapshot())

    async def forward(request: web.Request) -> web.Response:
        return await proxy.forward(request)

    app.router.add_get("/health", health)
    app.router.add_get("/stats", stats)
    app.router.add_route("*", "/{tail:.*}", forward)

    async def start(_: web.Application) -> None:
        await proxy.start()

    async def stop(_: web.Application) -> None:
        await proxy.stop()

    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8180)
    parser.add_argument("--upstreams", nargs="+", required=True)
    parser.add_argument("--health-interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=130.0)
    args = parser.parse_args(argv)
    proxy = HAProxy(args.upstreams, args.health_interval, args.timeout)
    web.run_app(create_app(proxy), host="127.0.0.1", port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
