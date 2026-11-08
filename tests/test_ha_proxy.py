"""Tests for the health-checked HA proxy."""

import asyncio
import unittest

from aiohttp.test_utils import TestClient, TestServer

from llm_engine.backend import FakeBackend
from llm_engine.ha_proxy import HAProxy, create_app
from llm_engine.worker import LLMWorker
from llm_engine.worker import create_app as create_worker_app


class PickTests(unittest.TestCase):
    def test_pick_skips_unhealthy(self):
        proxy = HAProxy(["a", "b"])
        proxy.upstreams[0].healthy = True
        proxy.upstreams[1].healthy = False
        self.assertEqual(proxy._pick().url, "a")

    def test_pick_returns_none_when_all_down(self):
        proxy = HAProxy(["a", "b"])
        self.assertIsNone(proxy._pick())


class HAProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clients = []
        urls = []
        for index in range(2):
            worker = LLMWorker(
                f"worker-{index + 1}",
                FakeBackend(delay_ms=2),
                cache_capacity=10,
            )
            client = TestClient(TestServer(create_worker_app(worker)))
            await client.start_server()
            self.clients.append(client)
            urls.append(str(client.server.make_url("")).rstrip("/"))
        self.proxy = HAProxy(urls, health_interval=0.1)
        self.proxy_client = TestClient(TestServer(create_app(self.proxy)))
        await self.proxy_client.start_server()
        await asyncio.sleep(0.3)

    async def asyncTearDown(self):
        await self.proxy_client.close()
        for client in self.clients:
            try:
                await client.close()
            except Exception:
                pass

    async def test_forwards_requests(self):
        response = await self.proxy_client.post(
            "/generate",
            json={"prompt": "hello", "max_tokens": 4, "temperature": 0.0},
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(body["text"].startswith("fake:"))

    async def test_failover_to_healthy_upstream(self):
        first = await self.proxy_client.post("/generate", json={"prompt": "one", "max_tokens": 4})
        self.assertEqual(first.status, 200)
        await self.clients[0].close()
        await asyncio.sleep(0.4)
        second = await self.proxy_client.post("/generate", json={"prompt": "two", "max_tokens": 4})
        self.assertEqual(second.status, 200)


if __name__ == "__main__":
    unittest.main()
