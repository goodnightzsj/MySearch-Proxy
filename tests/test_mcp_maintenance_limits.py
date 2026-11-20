from __future__ import annotations

from contextlib import asynccontextmanager
import unittest
from unittest.mock import patch

import anyio
import httpx
from mcp.server.fastmcp import FastMCP

from mysearch import server
import test_mcp_http_lifecycle as http_lifecycle
from test_mcp_runtime import StubClient, mcp_config


class McpMaintenanceLimitsTests(unittest.IsolatedAsyncioTestCase):
    running = http_lifecycle.HttpLifecycleTests.running

    def build(self, client):
        with patch.object(server, "MySearchClient", return_value=client), patch.object(server.atexit, "register"):
            _, mcp = server.build_mcp(mcp_config())
        self.addAsyncCleanup(server._close_client, client)
        return mcp

    @asynccontextmanager
    async def http(self, mcp):
        async with self.running(mcp, "streamable-http") as address, httpx.AsyncClient(
            base_url=address, trust_env=False,
            headers={"Accept": "application/json, text/event-stream"},
        ) as client:
            yield client

    async def initialize(self, http):
        return await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "limit-test", "version": "1"},
        }})

    async def test_idle_and_capacity_limits_have_explicit_reinitialization_path(self):
        mcp = FastMCP(host="127.0.0.1", json_response=True, session_idle_timeout=0.15, max_sessions=1)
        async with self.http(mcp) as http:
            first = await self.initialize(http)
            self.assertEqual(first.status_code, 200)
            first_id = first.headers["mcp-session-id"]
            self.assertEqual((await self.initialize(http)).status_code, 503)
            with anyio.fail_after(3):
                while True:
                    await anyio.sleep(0.02)
                    replacement = await self.initialize(http)
                    if replacement.status_code == 200:
                        break
                    self.assertEqual(replacement.status_code, 503)
            self.assertNotEqual(replacement.headers["mcp-session-id"], first_id)
            expired = await http.post("/mcp", headers={"mcp-session-id": first_id}, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertEqual(expired.status_code, 404)

    async def test_input_size_limit_does_not_limit_full_output(self):
        client = StubClient()
        content = "完整正文" * 400000 + "FULL END"
        client.payloads["search"]["results"][0]["content"] = content
        mcp = self.build(client)
        self.assertEqual(mcp.settings.max_request_body_size, 4 * 1024 * 1024)
        self.assertEqual(mcp.settings.session_idle_timeout, 1800)
        self.assertEqual(mcp.settings.max_sessions, 10000)
        async with self.http(mcp) as http:
            too_large = await http.post("/mcp", content=b"x" * (4 * 1024 * 1024 + 1), headers={"Content-Type": "application/json"})
            self.assertEqual(too_large.status_code, 413)
            self.assertFalse(client.states)
            full = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search", "arguments": {"query": "query"}}})
            self.assertEqual(full.status_code, 200)
            self.assertEqual(full.json()["result"]["structuredContent"]["results"][0]["content"], content)


if __name__ == "__main__":
    unittest.main()
