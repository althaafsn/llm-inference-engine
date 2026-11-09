"""AIMD policy for the gateway in-flight limit."""

from __future__ import annotations


def next_limit(
    current: int,
    observed_p95_ms: float,
    samples: int,
    min_limit: int,
    max_limit: int,
    target_p95_ms: float,
    sample_threshold: int = 20,
) -> int:
    if min_limit <= 0 or max_limit < min_limit:
        return current
    clamped = min(max_limit, max(min_limit, current))
    if samples < sample_threshold or observed_p95_ms <= 0 or target_p95_ms <= 0:
        return clamped
    if observed_p95_ms * 10 > target_p95_ms * 11:
        step = max(1, clamped // 5)
        return max(min_limit, clamped - step)
    if observed_p95_ms * 4 < target_p95_ms * 3:
        return min(max_limit, clamped + 1)
    return clamped
