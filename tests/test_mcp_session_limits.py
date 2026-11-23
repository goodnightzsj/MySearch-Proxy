from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import anyio
import httpx
from mcp.server.fastmcp import FastMCP
import uvicorn

from mysearch import server


class McpSessionLimitsTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def http(self, mcp):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        address = f"http://127.0.0.1:{listener.getsockname()[1]}"
        host = uvicorn.Server(uvicorn.Config(
            mcp.streamable_http_app(), log_level="error", lifespan="on", timeout_graceful_shutdown=3,
        ))
        task = asyncio.create_task(host.serve(sockets=[listener]))
        try:
            with anyio.fail_after(3):
                while not host.started:
                    if task.done():
                        await task
                        self.fail("HTTP server stopped before startup")
                    await anyio.sleep(0.005)
            async with httpx.AsyncClient(base_url=address, trust_env=False, headers={
                "Accept": "application/json, text/event-stream",
            }) as client:
                yield client
        finally:
            host.should_exit = True
            try:
                with anyio.fail_after(5):
                    await task
            finally:
                listener.close()

    async def initialize(self, http):
        return await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "limits", "version": "1"},
        }})

    async def test_idle_expiry_and_delete_reclaim_capacity(self):
        mcp = FastMCP(host="127.0.0.1", json_response=True, session_idle_timeout=0.15, max_sessions=1)
        async with self.http(mcp) as http:
            first = await self.initialize(http)
            self.assertEqual(first.status_code, 200)
            session_id = first.headers["mcp-session-id"]
            self.assertEqual((await self.initialize(http)).status_code, 503)
            with anyio.fail_after(3):
                while True:
                    await anyio.sleep(0.02)
                    replacement = await self.initialize(http)
                    if replacement.status_code == 200:
                        break
                    self.assertEqual(replacement.status_code, 503)
            self.assertNotEqual(replacement.headers["mcp-session-id"], session_id)
            expired = await http.post("/mcp", headers={"mcp-session-id": session_id}, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/list",
            })
            self.assertEqual(expired.status_code, 404)
            deleted = await http.delete("/mcp", headers={"mcp-session-id": replacement.headers["mcp-session-id"]})
            self.assertEqual(deleted.status_code, 200)
            self.assertEqual((await self.initialize(http)).status_code, 200)

    async def test_input_limit_keeps_large_full_output_and_six_tools(self):
        content = "full page\n" * 500000 + "FULL END"
        client = Mock()
        client.search.return_value = {"results": [{"content": content}]}
        config = SimpleNamespace(server_name="limits", mcp_host="127.0.0.1", mcp_port=8000,
            mcp_mount_path="/", mcp_sse_path="/sse", mcp_streamable_http_path="/mcp", mcp_stateless_http=True)
        with patch.object(server, "MySearchClient", return_value=client), patch.object(server.atexit, "register"):
            _, mcp = server.build_mcp(config)
        self.assertEqual(mcp.settings.max_request_body_size, 4 * 1024 * 1024)
        self.assertEqual(mcp.settings.session_idle_timeout, 1800)
        self.assertEqual(mcp.settings.max_sessions, 10000)
        async with self.http(mcp) as http:
            oversized = await http.post("/mcp", content=b"x" * (4 * 1024 * 1024 + 1), headers={
                "Content-Type": "application/json",
            })
            self.assertEqual(oversized.status_code, 413)
            client.search.assert_not_called()
            listed = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertEqual({tool["name"] for tool in listed.json()["result"]["tools"]}, {
                "search", "extract_url", "research", "map_site", "crawl_site", "mysearch_health",
            })
            full = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "search", "arguments": {"query": "query", "include_content": True},
            }})
            self.assertEqual(full.status_code, 200)
            result = full.json()["result"]
            self.assertFalse(result.get("isError", False))
            self.assertEqual(json.loads(result["content"][0]["text"])["results"][0]["content"], content)
