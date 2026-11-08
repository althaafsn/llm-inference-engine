"""Tests for the LRU cache."""

import unittest

from llm_engine.cache import LRUCache


class LRUCacheTests(unittest.TestCase):
    def test_get_and_put(self):
        cache = LRUCache(2)
        cache.put("a", {"text": "A"})
        self.assertEqual(cache.get("a"), {"text": "A"})
        self.assertEqual(cache.stats()["hits"], 1)

    def test_evicts_least_recently_used(self):
        cache = LRUCache(2)
        cache.put("a", {"text": "A"})
        cache.put("b", {"text": "B"})
        cache.get("a")
        cache.put("c", {"text": "C"})
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("a"), {"text": "A"})
        self.assertEqual(cache.get("c"), {"text": "C"})

    def test_disabled_cache_keeps_nothing(self):
        cache = LRUCache(0)
        cache.put("a", {"text": "A"})
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.stats()["size"], 0)

    def test_hit_rate(self):
        cache = LRUCache(2)
        cache.put("a", {"text": "A"})
        cache.get("a")
        cache.get("missing")
        self.assertEqual(cache.stats()["hit_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
