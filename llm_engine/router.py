"""Worker selection with consistent hashing and least-loaded policies."""

from __future__ import annotations

import hashlib
import time
from bisect import bisect_right
from dataclasses import dataclass


def _hash(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)


@dataclass
class WorkerState:
    url: str
    inflight: int = 0
    failures: int = 0
    circuit_open_until: float = 0.0
    total: int = 0
    errors: int = 0
    health_failures: int = 0


class Router:
    def __init__(
        self,
        workers: list[str],
        policy: str = "least_loaded",
        failure_threshold: int = 5,
        open_seconds: float = 10.0,
        virtual_nodes: int = 100,
    ):
        if policy not in ("hash", "least_loaded"):
            raise ValueError("policy must be 'hash' or 'least_loaded'")
        self.policy = policy
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self.workers = [WorkerState(url=url) for url in workers]
        self._ring: list[tuple[int, int]] = []
        if policy == "hash":
            for index, worker in enumerate(self.workers):
                for node in range(virtual_nodes):
                    self._ring.append((_hash(f"{worker.url}:{node}"), index))
            self._ring.sort()
            self._ring_hashes = [item[0] for item in self._ring]

    def _available(self) -> list[WorkerState]:
        now = time.monotonic()
        return [worker for worker in self.workers if now >= worker.circuit_open_until]

    def candidates(self, prompt: str) -> list[WorkerState]:
        available = self._available()
        if not available:
            available = list(self.workers)
        if self.policy == "hash" and self._ring:
            start = bisect_right(self._ring_hashes, _hash(prompt))
            ordered: list[WorkerState] = []
            seen: set[int] = set()
            for offset in range(len(self._ring)):
                _, index = self._ring[(start + offset) % len(self._ring)]
                if index in seen:
                    continue
                seen.add(index)
                worker = self.workers[index]
                if worker in available:
                    ordered.append(worker)
            ordered.extend(worker for worker in available if worker not in ordered)
            return ordered
        return sorted(available, key=lambda worker: (worker.inflight, worker.errors))

    def acquire(self, prompt: str) -> WorkerState:
        worker = self.candidates(prompt)[0]
        worker.inflight += 1
        worker.total += 1
        return worker

    def release(self, worker: WorkerState) -> None:
        worker.inflight = max(0, worker.inflight - 1)

    def record_success(self, worker: WorkerState) -> None:
        worker.failures = 0
        worker.circuit_open_until = 0.0

    def record_failure(self, worker: WorkerState) -> None:
        worker.errors += 1
        worker.failures += 1
        if worker.failures >= self.failure_threshold:
            worker.circuit_open_until = time.monotonic() + self.open_seconds

    def mark_health(self, worker: WorkerState, healthy: bool) -> None:
        if healthy:
            worker.health_failures = 0
            worker.failures = 0
            worker.circuit_open_until = 0.0
            return
        worker.health_failures += 1
        if worker.health_failures >= 2:
            worker.circuit_open_until = time.monotonic() + self.open_seconds

    def snapshot(self) -> list[dict]:
        now = time.monotonic()
        return [
            {
                "url": worker.url,
                "inflight": worker.inflight,
                "total": worker.total,
                "errors": worker.errors,
                "failures": worker.failures,
                "health_failures": worker.health_failures,
                "circuit_open": now < worker.circuit_open_until,
            }
            for worker in self.workers
        ]
