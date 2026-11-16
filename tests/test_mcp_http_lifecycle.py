from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import socket
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import anyio
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
import uvicorn

from mysearch import server
from mysearch.clients import MySearchClient
from mysearch.diagnostics import diagnosed


class LocalClient(MySearchClient):
    def __init__(self):
        super().__init__(config=SimpleNamespace(
            timeout_seconds=10, research_timeout_seconds=10, crawl_timeout_seconds=10,
            xai_social_timeout_seconds=10, max_parallel_workers=2, search_cache_ttl_seconds=60,
            extract_cache_ttl_seconds=60, proxy_base_url="", proxy_api_key="",
        ), keyring=Mock())
        self.entered, self.release, self.returned = Event(), Event(), Event()
        self.calls = []
        self.states = []

    def new_request(self, *args, **kwargs):
        state = super().new_request(*args, **kwargs)
        self.states.append(state)
        return state

    @diagnosed("search")
    def search(self, query, **kwargs):
        self.calls.append(query)
        if query == "blocked":
            self.entered.set()
            if not self.release.wait(5):
                raise TimeoutError("Test worker was not released")
            self.returned.set()
        return {"provider": "tavily", "results": [{"title": "Synthetic", "url": "https://result.test"}], "citations": []}


class HttpLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def build(self, client, stateless=True):
        config = SimpleNamespace(server_name="test", mcp_host="127.0.0.1", mcp_port=8000,
            mcp_mount_path="/", mcp_sse_path="/sse", mcp_streamable_http_path="/mcp", mcp_stateless_http=stateless)
        with patch.object(server, "MySearchClient", return_value=client), patch.object(server.atexit, "register"):
            _, mcp = server.build_mcp(config)
        self.assertIsInstance(mcp, FastMCP)
        self.addAsyncCleanup(server._close_client, client)
        return mcp

    @asynccontextmanager
    async def running(self, mcp, transport):
        app = mcp.sse_app() if transport == "sse" else mcp.streamable_http_app()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        address = f"http://127.0.0.1:{listener.getsockname()[1]}"
        host = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on", timeout_graceful_shutdown=3))
        task = asyncio.create_task(host.serve(sockets=[listener]))
        try:
            with anyio.fail_after(3):
                while not host.started:
                    if task.done():
                        await task
                        self.fail("Local ASGI host stopped before startup")
                    await anyio.sleep(0.005)
            yield address
        finally:
            host.should_exit = True
            try:
                with anyio.fail_after(5):
                    await task
            finally:
                listener.close()

    @asynccontextmanager
    async def session(self, address, transport):
        connection = sse_client(address + "/sse") if transport == "sse" else streamable_http_client(address + "/mcp")
        async with connection as streams:
            async with ClientSession(streams[0], streams[1], read_timeout_seconds=timedelta(seconds=4)) as session:
                await session.initialize()
                yield session

    async def wait_for(self, predicate):
        with anyio.fail_after(3):
            while not predicate():
                await anyio.sleep(0.005)

    async def test_sequential_sessions_and_stateless_http_requests_keep_shared_client_open(self):
        for transport, stateless in (("streamable-http", True), ("streamable-http", False), ("sse", False)):
            with self.subTest(transport=transport, stateless=stateless):
                client = LocalClient()
                async with self.running(self.build(client, stateless), transport) as address:
                    for index in range(2):
                        async with self.session(address, transport) as session:
                            self.assertEqual(len((await session.list_tools()).tools), 6)
                            for call in range(2):
                                result = await session.call_tool("search", {"query": f"session_{index}_call_{call}"})
                                self.assertFalse(result.isError, result.content)
                                self.assertEqual(result.structuredContent["diagnostics"]["outcome"], "success")
                        self.assertFalse(client._closing)
                        self.assertFalse(client._http.is_closed)
                self.assertEqual(len(client.calls), 4)
                self.assertEqual(len({state.request_id for state in client.states}), 4)
                self.assertTrue(client._closed)
                self.assertTrue(client._http.is_closed)

    async def test_disconnecting_one_session_does_not_close_another_sessions_worker(self):
        for transport, stateless in (("streamable-http", True), ("streamable-http", False), ("sse", False)):
            with self.subTest(transport=transport, stateless=stateless):
                client = LocalClient()
                disconnect, first_ready, first_done = anyio.Event(), anyio.Event(), anyio.Event()
                second_done = anyio.Event()
                results = []
                async with self.running(self.build(client, stateless), transport) as address:
                    async def first():
                        async with self.session(address, transport) as session:
                            result = await session.call_tool("search", {"query": "first"})
                            self.assertFalse(result.isError, result.content)
                            first_ready.set()
                            await disconnect.wait()
                        first_done.set()

                    async def second():
                        async with self.session(address, transport) as session:
                            results.append(await session.call_tool("search", {"query": "blocked"}))
                        second_done.set()

                    try:
                        async with anyio.create_task_group() as group:
                            group.start_soon(first)
                            with anyio.fail_after(3):
                                await first_ready.wait()
                            group.start_soon(second)
                            await self.wait_for(client.entered.is_set)
                            disconnect.set()
                            with anyio.fail_after(3):
                                await first_done.wait()
                            self.assertFalse(client._closing)
                            self.assertFalse(client._http.is_closed)
                            self.assertFalse(second_done.is_set())
                            client.release.set()
                            with anyio.fail_after(3):
                                await second_done.wait()
                    finally:
                        client.release.set()
                        disconnect.set()
                    self.assertFalse(results[0].isError, results[0].content)
                    self.assertEqual(client.states[-1].final["outcome"], "success")
                self.assertTrue(client._closed)

    async def test_asgi_shutdown_acknowledgement_waits_for_cancelled_workers_and_http_close(self):
        for transport in ("streamable-http", "sse"):
            with self.subTest(transport=transport):
                client = LocalClient()
                mcp = self.build(client)
                app = mcp.sse_app() if transport == "sse" else mcp.streamable_http_app()
                incoming = asyncio.Queue()
                started, stopped = anyio.Event(), anyio.Event()

                async def send(message):
                    if message["type"] == "lifespan.startup.complete":
                        started.set()
                    if message["type"] == "lifespan.shutdown.complete":
                        self.assertTrue(client.returned.is_set())
                        self.assertTrue(client._http.is_closed)
                        stopped.set()

                lifespan = asyncio.create_task(app({"type": "lifespan", "state": {}}, incoming.get, send))
                worker = None
                try:
                    await incoming.put({"type": "lifespan.startup"})
                    with anyio.fail_after(3):
                        await started.wait()
                    worker = asyncio.create_task(server._offload(client, "search", query="blocked"))
                    await self.wait_for(client.entered.is_set)
                    worker.cancel()
                    await self.wait_for(lambda: client.states[0].cancelled.is_set())
                    await incoming.put({"type": "lifespan.shutdown"})
                    await self.wait_for(lambda: client._closing)
                    self.assertFalse(stopped.is_set())
                    self.assertFalse(client._http.is_closed)
                    self.assertFalse(lifespan.done())
                finally:
                    client.release.set()
                    if worker is not None:
                        result = await asyncio.gather(worker, return_exceptions=True)
                        self.assertIsInstance(result[0], asyncio.CancelledError)
                    with anyio.fail_after(3):
                        await lifespan
                self.assertTrue(stopped.is_set())
                self.assertFalse(client._active_requests)
                self.assertEqual(client.states[0].final["outcome"], "cancelled")

    async def test_stdio_closes_after_transport_exit_including_errors(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                client = LocalClient()
                mcp = self.build(client)
                transport = AsyncMock(side_effect=RuntimeError("synthetic transport failure") if fail else None)
                with patch.object(FastMCP, "run_stdio_async", transport):
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "synthetic transport failure"):
                            await mcp.run_stdio_async()
                    else:
                        await mcp.run_stdio_async()
                transport.assert_awaited_once()
                self.assertTrue(client._closed)
                self.assertTrue(client._http.is_closed)

    async def test_asgi_startup_failure_closes_before_failure_acknowledgement(self):
        client = LocalClient()
        self.addAsyncCleanup(server._close_client, client)

        @asynccontextmanager
        async def fail_startup(app):
            raise RuntimeError("synthetic startup failure")
            yield

        app = Starlette(lifespan=fail_startup)
        app.add_middleware(server._ClientLifetimeMiddleware, client=client)
        messages = []

        async def receive():
            return {"type": "lifespan.startup"}

        async def send(message):
            self.assertTrue(client._closed)
            messages.append(message["type"])

        with self.assertRaisesRegex(RuntimeError, "synthetic startup failure"):
            await app({"type": "lifespan", "state": {}}, receive, send)
        self.assertEqual(messages, ["lifespan.startup.failed"])

    async def test_close_error_never_sends_successful_shutdown_acknowledgement(self):
        client = Mock()
        client.close.side_effect = RuntimeError("synthetic close failure")
        app = Starlette()
        app.add_middleware(server._ClientLifetimeMiddleware, client=client)
        events = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
        messages = []

        async def receive():
            return next(events)

        async def send(message):
            messages.append(message["type"])

        with self.assertRaisesRegex(RuntimeError, "synthetic close failure"):
            await app({"type": "lifespan", "state": {}}, receive, send)
        client.close.assert_called_once()
        self.assertEqual(messages, ["lifespan.startup.complete"])
