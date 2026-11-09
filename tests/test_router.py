"""Tests for worker routing and circuit breaking."""

import unittest

from llm_engine.router import Router


class RouterTests(unittest.TestCase):
    def test_least_loaded_prefers_idle_worker(self):
        router = Router(["a", "b"], policy="least_loaded")
        router.workers[0].inflight = 2
        self.assertEqual(router.candidates("prompt")[0].url, "b")

    def test_hash_is_deterministic(self):
        router = Router(["a", "b", "c"], policy="hash")
        first = router.candidates("same prompt")[0].url
        second = router.candidates("same prompt")[0].url
        self.assertEqual(first, second)

    def test_hash_spreads_prompts(self):
        router = Router(["a", "b", "c"], policy="hash")
        chosen = {router.candidates(f"prompt-{index}")[0].url for index in range(100)}
        self.assertEqual(chosen, {"a", "b", "c"})

    def test_circuit_opens_after_failures(self):
        router = Router(["a", "b"], policy="least_loaded", failure_threshold=2, open_seconds=60)
        worker = router.workers[0]
        router.record_failure(worker)
        router.record_failure(worker)
        self.assertTrue(router.snapshot()[0]["circuit_open"])
        self.assertEqual(router.candidates("prompt")[0].url, "b")

    def test_success_closes_circuit(self):
        router = Router(["a"], policy="least_loaded", failure_threshold=1, open_seconds=60)
        worker = router.workers[0]
        router.record_failure(worker)
        router.record_success(worker)
        self.assertFalse(router.snapshot()[0]["circuit_open"])

    def test_health_failures_open_and_recover_circuit(self):
        router = Router(["a"], policy="least_loaded", open_seconds=60)
        worker = router.workers[0]
        router.mark_health(worker, False)
        self.assertFalse(router.snapshot()[0]["circuit_open"])
        router.mark_health(worker, False)
        self.assertTrue(router.snapshot()[0]["circuit_open"])
        router.mark_health(worker, True)
        self.assertFalse(router.snapshot()[0]["circuit_open"])
        self.assertEqual(router.snapshot()[0]["health_failures"], 0)

    def test_unknown_policy_rejected(self):
        with self.assertRaises(ValueError):
            Router(["a"], policy="random")


if __name__ == "__main__":
    unittest.main()
