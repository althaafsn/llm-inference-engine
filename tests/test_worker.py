"""Tests for the worker API, batching, and cache."""

import asyncio
import unittest

from aiohttp.test_utils import TestClient, TestServer

from llm_engine.backend import FakeBackend
from llm_engine.worker import LLMWorker, create_app


class WorkerApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = FakeBackend(delay_ms=20)
        self.worker = LLMWorker(
            "worker-1",
            self.backend,
            cache_capacity=10,
            max_batch_size=8,
            batch_window_ms=10,
            max_queue=64,
        )
        self.client = TestClient(TestServer(create_app(self.worker)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_generate_returns_text(self):
        response = await self.client.post(
            "/generate",
            json={"prompt": "hello", "max_tokens": 4, "temperature": 0.0},
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(body["text"].startswith("fake:"))
        self.assertGreaterEqual(body["tokens"], 1)
        self.assertEqual(body["worker"], "worker-1")

    async def test_cache_hit_for_greedy_request(self):
        payload = {"prompt": "cache me", "max_tokens": 4, "temperature": 0.0}
        first = await (await self.client.post("/generate", json=payload)).json()
        second = await (await self.client.post("/generate", json=payload)).json()
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])

    async def test_batching_groups_concurrent_requests(self):
        responses = await asyncio.gather(
            *[
                self.client.post(
                    "/generate",
                    json={
                        "prompt": f"prompt-{index}",
                        "max_tokens": 4,
                        "temperature": 0.0,
                    },
                )
                for index in range(6)
            ]
        )
        self.assertTrue(all(response.status == 200 for response in responses))
        stats = await (await self.client.get("/stats")).json()
        self.assertGreater(stats["mean_batch_size"], 1.0)

    async def test_rejects_invalid_prompt(self):
        response = await self.client.post("/generate", json={"prompt": "", "max_tokens": 4})
        self.assertEqual(response.status, 400)

    async def test_rejects_invalid_max_tokens(self):
        response = await self.client.post("/generate", json={"prompt": "hello", "max_tokens": 0})
        self.assertEqual(response.status, 400)

    async def test_health_and_metrics(self):
        health = await (await self.client.get("/health")).json()
        self.assertEqual(health["status"], "ok")
        metrics = await self.client.get("/metrics")
        text = await metrics.text()
        self.assertIn("llm_requests_accepted_total", text)


class WorkerQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_full_returns_503(self):
        backend = FakeBackend(delay_ms=200)
        worker = LLMWorker(
            "worker-1",
            backend,
            cache_capacity=0,
            max_batch_size=1,
            batch_window_ms=100,
            max_queue=1,
        )
        client = TestClient(TestServer(create_app(worker)))
        await client.start_server()
        try:
            responses = await asyncio.gather(
                *[
                    client.post(
                        "/generate",
                        json={"prompt": f"prompt-{index}", "max_tokens": 4},
                    )
                    for index in range(2)
                ]
            )
            statuses = sorted(response.status for response in responses)
            self.assertEqual(statuses, [200, 503])
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
