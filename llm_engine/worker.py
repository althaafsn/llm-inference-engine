"""LLM worker: batching, response cache, metrics, and an HTTP API."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass

from aiohttp import web

from .backend import GenerateRequest, build_backend
from .cache import LRUCache
from .metrics import Metrics
from .protocol import parse_generate_request


class QueueFullError(RuntimeError):
    pass


class RequestTimeout(RuntimeError):
    pass


@dataclass
class Pending:
    request: GenerateRequest
    future: asyncio.Future
    enqueued_at: float
    cache_key: str | None


def cache_key(request: GenerateRequest) -> str:
    payload = json.dumps(
        [request.prompt, request.max_tokens, request.temperature, request.top_p],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LLMWorker:
    def __init__(
        self,
        worker_id: str,
        backend,
        cache_capacity: int = 256,
        max_batch_size: int = 8,
        batch_window_ms: float = 10.0,
        max_queue: int = 256,
        request_timeout: float = 120.0,
    ):
        self.worker_id = worker_id
        self.backend = backend
        self.cache = LRUCache(cache_capacity)
        self.metrics = Metrics()
        self.max_batch_size = max(1, max_batch_size)
        self.batch_window = max(0.0, batch_window_ms / 1000.0)
        self.max_queue = max(1, max_queue)
        self.request_timeout = request_timeout
        self.pending: dict[tuple, deque[Pending]] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False

    def queue_depth(self) -> int:
        return sum(len(queue) for queue in self.pending.values())

    async def start(self) -> None:
        self._task = asyncio.create_task(self._scheduler())

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task:
            await self._task

    def _key(self, request: GenerateRequest) -> tuple:
        return (request.max_tokens, request.temperature, request.top_p)

    async def generate(self, request: GenerateRequest) -> dict:
        key = cache_key(request) if request.temperature == 0 else None
        if key:
            cached = self.cache.get(key)
            if cached:
                self.metrics.record_accepted()
                self.metrics.record_completed(0.0, cached["tokens"], cached=True)
                return {
                    **cached,
                    "cached": True,
                    "queue_wait_ms": 0.0,
                    "generation_ms": 0.0,
                    "latency_ms": 0.0,
                }
        if self.queue_depth() >= self.max_queue:
            raise QueueFullError("worker queue is full")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        item = Pending(request, future, time.monotonic(), key)
        self.pending.setdefault(self._key(request), deque()).append(item)
        self._wake.set()
        self.metrics.record_accepted()
        try:
            return await asyncio.wait_for(future, timeout=self.request_timeout)
        except TimeoutError as exc:
            future.cancel()
            raise RequestTimeout("generation timed out") from exc

    def _next_queue(self) -> tuple[tuple | None, deque[Pending] | None]:
        best_key = None
        best_queue = None
        for key, queue in self.pending.items():
            if not queue:
                continue
            if best_queue is None or len(queue) > len(best_queue):
                best_key, best_queue = key, queue
        return best_key, best_queue

    async def _scheduler(self) -> None:
        while not self._stopping:
            await self._wake.wait()
            self._wake.clear()
            while not self._stopping:
                key, queue = self._next_queue()
                if queue is None:
                    break
                if self.batch_window > 0:
                    await asyncio.sleep(self.batch_window)
                batch: list[Pending] = []
                while queue and len(batch) < self.max_batch_size:
                    item = queue.popleft()
                    if not item.future.done():
                        batch.append(item)
                if not queue:
                    self.pending.pop(key, None)
                if batch:
                    await self._run_batch(batch)
                if not any(self.pending.values()):
                    break

    async def _run_batch(self, batch: list[Pending]) -> None:
        requests = [item.request for item in batch]
        started = time.monotonic()
        try:
            results = await asyncio.to_thread(self.backend.generate_batch, requests)
        except Exception as exc:
            for item in batch:
                if not item.future.done():
                    item.future.set_exception(exc)
            self.metrics.record_failed()
            return
        finished = time.monotonic()
        for item, result in zip(batch, results, strict=True):
            latency_ms = round((finished - item.enqueued_at) * 1000.0, 3)
            queue_wait_ms = round((started - item.enqueued_at) * 1000.0, 3)
            generation_ms = round((finished - started) * 1000.0, 3)
            payload = {
                "text": result.text,
                "tokens": result.tokens,
                "cached": False,
                "queue_wait_ms": queue_wait_ms,
                "generation_ms": generation_ms,
                "latency_ms": latency_ms,
            }
            if item.cache_key:
                self.cache.put(item.cache_key, payload)
            self.metrics.record_completed(latency_ms, result.tokens)
            if not item.future.done():
                item.future.set_result(payload)


def create_app(worker: LLMWorker) -> web.Application:
    app = web.Application()

    async def generate(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        try:
            parsed = parse_generate_request(data)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        try:
            result = await worker.generate(parsed)
        except QueueFullError as exc:
            return web.json_response({"error": str(exc)}, status=503, headers={"Retry-After": "1"})
        except RequestTimeout as exc:
            return web.json_response({"error": str(exc)}, status=504)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response(
            {"request_id": data.get("request_id"), "worker": worker.worker_id, **result}
        )

    async def health(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "worker": worker.worker_id,
                "backend": type(worker.backend).__name__,
                "queue_depth": worker.queue_depth(),
            }
        )

    async def stats(_: web.Request) -> web.Response:
        extra = {
            "worker": worker.worker_id,
            "queue_depth": worker.queue_depth(),
            "cache": worker.cache.stats(),
            "backend": type(worker.backend).__name__,
            "max_batch_size": worker.max_batch_size,
            "batch_window_ms": worker.batch_window * 1000.0,
        }
        batch_sizes = getattr(worker.backend, "batch_sizes", None)
        if batch_sizes is not None:
            extra["batches"] = len(batch_sizes)
            extra["mean_batch_size"] = (
                round(sum(batch_sizes) / len(batch_sizes), 2) if batch_sizes else 0.0
            )
        return web.json_response(worker.metrics.snapshot(extra))

    async def metrics(_: web.Request) -> web.Response:
        text = worker.metrics.prometheus({"worker": worker.worker_id})
        cache = worker.cache.stats()
        text += f'llm_cache_entries{{worker="{worker.worker_id}"}} {cache["size"]}\n'
        text += f'llm_queue_depth{{worker="{worker.worker_id}"}} {worker.queue_depth()}\n'
        return web.Response(text=text, content_type="text/plain")

    app.router.add_post("/generate", generate)
    app.router.add_get("/health", health)
    app.router.add_get("/stats", stats)
    app.router.add_get("/metrics", metrics)

    async def start(_: web.Application) -> None:
        await worker.start()

    async def stop(_: web.Application) -> None:
        await worker.stop()

    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--id", default="worker-1")
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument("--backend", choices=("hf", "fake"), default="hf")
    parser.add_argument("--cache-capacity", type=int, default=256)
    parser.add_argument("--max-batch-size", type=int, default=8)
    parser.add_argument("--batch-window-ms", type=float, default=10.0)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--quantize", action="store_true")
    parser.add_argument("--attn", choices=("sdpa", "eager"), default="sdpa")
    parser.add_argument("--warmup", action="store_true")
    parser.add_argument("--interop-threads", type=int, default=1)
    parser.add_argument("--max-queue", type=int, default=256)
    parser.add_argument("--request-timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    backend = build_backend(
        args.backend,
        args.model,
        args.threads,
        quantize=args.quantize,
        attn=args.attn,
        warmup=args.warmup,
        interop_threads=args.interop_threads,
    )
    worker = LLMWorker(
        worker_id=args.id,
        backend=backend,
        cache_capacity=args.cache_capacity,
        max_batch_size=args.max_batch_size,
        batch_window_ms=args.batch_window_ms,
        max_queue=args.max_queue,
        request_timeout=args.request_timeout,
    )
    web.run_app(create_app(worker), host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
