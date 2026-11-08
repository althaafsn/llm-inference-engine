"""An LRU response cache for deterministic LLM generations."""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock


class LRUCache:
    def __init__(self, capacity: int):
        self.capacity = max(0, capacity)
        self._data: OrderedDict[str, dict] = OrderedDict()
        self._lock = Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict | None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                self.hits += 1
                return self._data[key]
            self.misses += 1
            return None

    def put(self, key: str, value: dict) -> None:
        if self.capacity <= 0:
            return
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "capacity": self.capacity,
                "size": len(self._data),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
            }
