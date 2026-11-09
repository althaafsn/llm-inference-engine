"""Tests for gateway admission, retry, and HTTP behavior."""

import asyncio
import unittest

from aiohttp.test_utils import TestClient, TestServer

from llm_engine.backend import FakeBackend
from llm_engine.gateway import AdmissionRejected, Gateway, create_app
from llm_engine.worker import LLMWorker
from llm_engine.worker import create_app as create_worker_app


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = FakeBackend(delay_ms=5)
        self.worker = LLMWorker("worker-1", self.backend, cache_capacity=10)
        self.worker_client = TestClient(TestServer(create_worker_app(self.worker)))
        await self.worker_client.start_server()
        self.worker_url = str(self.worker_client.server.make_url("")).rstrip("/")
        self.gateway = Gateway("gateway-1", [self.worker_url], admission_max=0)
        await self.gateway.start()

    async def asyncTearDown(self):
        await self.gateway.stop()
        await self.worker_client.close()

    async def test_generate_through_gateway(self):
        result = await self.gateway.generate(
            {"prompt": "hello", "max_tokens": 4, "temperature": 0.0}
        )
        self.assertTrue(result["text"].startswith("fake:"))
        self.assertEqual(result["gateway"], "gateway-1")

    async def test_retry_uses_second_worker(self):
        gateway = Gateway("gateway-1", ["http://127.0.0.1:1", self.worker_url], admission_max=0)
        await gateway.start()
        try:
            result = await gateway.generate(
                {"prompt": "retry", "max_tokens": 4, "temperature": 0.0}
            )
            self.assertEqual(result["worker"], "worker-1")
            self.assertEqual(gateway.metrics.retries, 1)
        finally:
            await gateway.stop()


class WatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_watcher_opens_circuit_for_dead_worker(self):
        gateway = Gateway(
            "gateway-1",
            ["http://127.0.0.1:1"],
            admission_max=0,
            watch_interval=0.05,
        )
        await gateway.start()
        try:
            await asyncio.sleep(0.5)
            self.assertTrue(gateway.router.snapshot()[0]["circuit_open"])
        finally:
            await gateway.stop()


class AdaptiveGatewayTests(unittest.TestCase):
    def test_limit_decreases_on_high_p95(self):
        gateway = Gateway(
            "gateway-1",
            ["http://127.0.0.1:1"],
            admission_max=4,
            admission_adaptive=True,
            admission_min=1,
            admission_target_p95_ms=400,
            admission_interval_ms=1000,
        )
        gateway.latency_window.extend([1000.0] * 25)
        gateway._update_limit()
        self.assertLess(gateway.admission_limit, 4)
        self.assertGreater(gateway.observed_p95_ms, 0)

    def test_limit_increases_on_low_p95(self):
        gateway = Gateway(
            "gateway-1",
            ["http://127.0.0.1:1"],
            admission_max=4,
            admission_adaptive=True,
            admission_min=1,
            admission_target_p95_ms=400,
        )
        gateway.admission_limit = 1
        gateway.latency_window.extend([50.0] * 25)
        gateway._update_limit()
        self.assertEqual(gateway.admission_limit, 2)


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_admission_limit_rejects_second_request(self):
        backend = FakeBackend(delay_ms=100)
        worker = LLMWorker("worker-1", backend, cache_capacity=0, max_batch_size=1)
        worker_client = TestClient(TestServer(create_worker_app(worker)))
        await worker_client.start_server()
        worker_url = str(worker_client.server.make_url("")).rstrip("/")
        gateway = Gateway("gateway-1", [worker_url], admission_max=1)
        await gateway.start()
        try:
            results = await asyncio.gather(
                gateway.generate({"prompt": "one", "max_tokens": 4}),
                gateway.generate({"prompt": "two", "max_tokens": 4}),
                return_exceptions=True,
            )
            rejected = [result for result in results if isinstance(result, AdmissionRejected)]
            self.assertEqual(len(rejected), 1)
        finally:
            await gateway.stop()
            await worker_client.close()


class GatewayHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_health_and_validation(self):
        worker = LLMWorker("worker-1", FakeBackend(delay_ms=1), cache_capacity=10)
        worker_client = TestClient(TestServer(create_worker_app(worker)))
        await worker_client.start_server()
        worker_url = str(worker_client.server.make_url("")).rstrip("/")
        gateway = Gateway("gateway-1", [worker_url], admission_max=0)
        client = TestClient(TestServer(create_app(gateway)))
        await client.start_server()
        try:
            health = await (await client.get("/health")).json()
            self.assertEqual(health["status"], "ok")
            response = await client.post("/generate", json={"prompt": ""})
            self.assertEqual(response.status, 400)
        finally:
            await client.close()
            await worker_client.close()


if __name__ == "__main__":
    unittest.main()
