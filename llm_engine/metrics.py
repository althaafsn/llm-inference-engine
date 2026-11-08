"""Small counters and latency windows for the gateway and worker."""

from __future__ import annotations

import math
from collections import deque


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(p * len(ordered)) - 1)
    return round(ordered[index], 3)


class Metrics:
    def __init__(self, window: int = 2000):
        self.window = window
        self.accepted = 0
        self.rejected = 0
        self.completed = 0
        self.failed = 0
        self.cache_hits = 0
        self.retries = 0
        self.total_tokens = 0
        self._latencies: deque[float] = deque(maxlen=window)

    def record_accepted(self) -> None:
        self.accepted += 1

    def record_rejected(self) -> None:
        self.rejected += 1

    def record_retry(self) -> None:
        self.retries += 1

    def record_completed(self, latency_ms: float, tokens: int, cached: bool = False) -> None:
        self.completed += 1
        self.total_tokens += tokens
        self._latencies.append(latency_ms)
        if cached:
            self.cache_hits += 1

    def record_failed(self, latency_ms: float | None = None) -> None:
        self.failed += 1
        if latency_ms is not None:
            self._latencies.append(latency_ms)

    def snapshot(self, extra: dict | None = None) -> dict:
        samples = list(self._latencies)
        result = {
            "accepted": self.accepted,
            "rejected": self.rejected,
            "completed": self.completed,
            "failed": self.failed,
            "cache_hits": self.cache_hits,
            "retries": self.retries,
            "total_tokens": self.total_tokens,
            "latency_ms": {
                "p50": percentile(samples, 0.50),
                "p95": percentile(samples, 0.95),
                "p99": percentile(samples, 0.99),
            },
        }
        if extra:
            result.update(extra)
        return result

    def prometheus(self, labels: dict[str, str] | None = None) -> str:
        prefix = ""
        if labels:
            prefix = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
        lines = [
            f"llm_requests_accepted_total{prefix} {self.accepted}",
            f"llm_requests_rejected_total{prefix} {self.rejected}",
            f"llm_requests_completed_total{prefix} {self.completed}",
            f"llm_requests_failed_total{prefix} {self.failed}",
            f"llm_cache_hits_total{prefix} {self.cache_hits}",
            f"llm_retries_total{prefix} {self.retries}",
            f"llm_tokens_total{prefix} {self.total_tokens}",
        ]
        return "\n".join(lines) + "\n"
