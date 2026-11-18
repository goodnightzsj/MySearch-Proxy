from __future__ import annotations

import asyncio
import copy
import importlib.util
import io
import json
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import anyio
from mcp.shared.memory import create_connected_server_and_client_session

from mysearch import server
from mysearch.clients import MySearchClient
from mysearch.diagnostics import RequestState, diagnosed, finish_payload, phase
from mysearch.errors import MySearchError


PAYLOADS = {
    "search": {"provider": "tavily", "results": [{"title": "Result", "url": "https://result.test"}], "citations": []},
    "extract_url": {"provider": "firecrawl", "url": "https://result.test", "content": "Body", "metadata": {}},
    "research": {"provider": "hybrid", "query": "query", "web_search": {}, "pages": [], "citations": [], "summary": "Summary"},
    "map_site": {"provider": "firecrawl", "url": "https://result.test", "links": [{"url": "https://result.test/a"}], "count": 1, "metadata": {}},
    "crawl_site": {"provider": "firecrawl", "url": "https://result.test", "status": "completed", "pages": [], "count": 0, "metadata": {}},
    "mysearch_health": {"server_name": "test", "providers": {}, "timeout_seconds": 45},
}
ARGUMENTS = {
    "search": {"query": "query"}, "extract_url": {"url": "https://result.test"},
    "research": {"query": "query"}, "map_site": {"url": "https://result.test"},
    "crawl_site": {"url": "https://result.test"}, "mysearch_health": {},
}


def mcp_config():
    return SimpleNamespace(server_name="test", mcp_host="127.0.0.1", mcp_port=8000, mcp_mount_path="/",
                           mcp_sse_path="/sse", mcp_streamable_http_path="/mcp", mcp_stateless_http=True)


class StubClient:
    def __init__(self):
        self.states = []
        self.terminals = []
        self.closed = False
        self.payloads = copy.deepcopy(PAYLOADS)
        self.actions = {}

    def new_request(self, tool, **kwargs):
        state = RequestState(deadline=time.monotonic() + 10, tool="mysearch_health" if tool == "health" else tool)
        self.states.append(state)
        return state

    def finish_request(self, state, result=None, error=None):
        payload = finish_payload(state, result, error)
        if payload is not None:
            self.terminals.append(payload)

    def close(self):
        self.closed = True

    def __getattr__(self, tool):
        name = "mysearch_health" if tool == "health" else tool
        if name not in self.payloads:
            raise AttributeError(tool)

        def invoke(**kwargs):
            if name in self.actions:
                self.actions[name]()
            return copy.deepcopy(self.payloads[name])
        return invoke


class McpRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def build(self, client):
        with patch.object(server, "MySearchClient", return_value=client), patch.object(server.atexit, "register"):
            _, mcp = server.build_mcp(mcp_config())
        self.addAsyncCleanup(server._close_client, client)
        return mcp

    async def test_six_tools_expose_schemas_annotations_and_matching_text_structured_results(self):
        client = StubClient()
        for payload in client.payloads.values():
            payload["provider_extension"] = {"nested": [1, "retained"]}
        mcp = self.build(client)
        async with create_connected_server_and_client_session(mcp) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            self.assertEqual(set(tools), set(PAYLOADS))
            for name, tool in tools.items():
                with self.subTest(tool=name):
                    self.assertNotIn("ctx", tool.inputSchema["properties"])
                    self.assertIsNotNone(tool.outputSchema)
                    self.assertEqual(tool.outputSchema["type"], "object")
                    self.assertTrue(tool.outputSchema.get("required"))
                    self.assertIsNotNone(tool.annotations)
                    self.assertFalse(tool.annotations.destructiveHint)
                    self.assertTrue(tool.annotations.openWorldHint)
                    self.assertEqual(tool.annotations.idempotentHint, name != "crawl_site")
                    self.assertEqual(tool.annotations.readOnlyHint, name != "crawl_site")
                    result = await session.call_tool(name, ARGUMENTS[name])
                    self.assertFalse(result.isError, result.content)
                    self.assertEqual({key: value for key, value in result.structuredContent.items() if key != "diagnostics"}, client.payloads[name])
                    self.assertEqual(result.structuredContent["diagnostics"], client.terminals[-1]["diagnostics"])
                    text = [item.text for item in result.content if item.type == "text"]
                    self.assertEqual(len(text), 1)
                    self.assertEqual(json.loads(text[0]), result.structuredContent)
        self.assertFalse(client.closed)
        self.assertEqual(len(client.terminals), 6)

    async def test_invalid_required_output_fails_without_fabricated_success(self):
        client = StubClient()
        client.payloads["extract_url"].pop("content")
        async with create_connected_server_and_client_session(self.build(client)) as session:
            result = await session.call_tool("extract_url", ARGUMENTS["extract_url"])
        self.assertTrue(result.isError)
        self.assertIsNone(result.structuredContent)
        self.assertEqual(len(client.terminals), 1)
        self.assertEqual(client.terminals[0]["outcome"], "error")
        self.assertEqual(client.terminals[0]["failure_reason"], "ValidationError")

    async def test_compact_search_only_references_exact_duplicate_candidate_bodies(self):
        body = "正文 / ~ 🧪\n" * 100
        other = body + "different end"
        full = {
            **PAYLOADS["search"], "results": [{"url": "a", "content": body}],
            "citations": [{"url": "a"}], "answer": "answer", "extra": {"content": body},
            "primary_search": {"results": [{"content": body, "source": "one"}, {"content": other}]},
            "secondary_search": {"secondary_search": {"results": [
                {"content": other, "source": "two"}, {"content": ""}, {}, {"content": None},
                {"content": {"$ref": "/original/extension"}}, {"content": "short"}, {"content": "short"},
            ]}},
        }
        client = StubClient()
        client.payloads["search"] = full
        async with create_connected_server_and_client_session(self.build(client)) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            spec = tools["search"].inputSchema["properties"]["response_format"]
            self.assertEqual((spec["default"], set(spec["enum"])), ("full", {"full", "compact"}))
            for args in ({}, {"response_format": "compact"}, {"response_format": "full"}):
                response = await session.call_tool("search", {"query": "q", **args})
                self.assertFalse(response.isError)
                output = response.structuredContent.copy()
                self.assertEqual(output, json.loads(response.content[0].text))
                self.assertEqual(output.pop("diagnostics"), client.terminals[-1]["diagnostics"])
                if args.get("response_format") == "compact":
                    expected = copy.deepcopy(full)
                    expected["primary_search"]["results"][0]["content"] = {"$ref": "/results/0/content"}
                    nested = expected["secondary_search"]["secondary_search"]["results"]
                    nested[0]["content"] = {"$ref": "/primary_search/results/1/content"}
                    nested[4]["content"] = {"$literal": {"$ref": "/original/extension"}}
                    self.assertEqual(output, expected)
                else:
                    self.assertEqual(output, full)
            invalid = await session.call_tool("search", {"query": "q", "response_format": "typo"})
            self.assertTrue(invalid.isError)
            self.assertEqual(len(client.terminals), 3)

    def test_compact_preserves_shared_runtime_rows_and_literal_marker(self):
        body = "long" * 64
        row = {"content": body, "url": "https://example.test/a"}
        literal = {"$literal": "provider extension"}
        payload = {"results": [row], "primary_search": {"results": [row, {"content": literal}]}}
        before = copy.deepcopy(payload)
        compact = server.compact_search_result(payload)
        self.assertEqual(payload, before)
        self.assertEqual(compact["results"], before["results"])
        self.assertEqual(compact["primary_search"]["results"][0]["content"], {"$ref": "/results/0/content"})
        self.assertEqual(compact["primary_search"]["results"][1]["content"], {"$literal": literal})

    async def test_decorated_client_waits_for_output_validation_before_only_terminal(self):
        config = SimpleNamespace(timeout_seconds=45, research_timeout_seconds=90, crawl_timeout_seconds=90,
                                 xai_social_timeout_seconds=120, max_parallel_workers=2,
                                 search_cache_ttl_seconds=60, extract_cache_ttl_seconds=60,
                                 proxy_base_url="", proxy_api_key="")

        class ContractClient(MySearchClient):
            @diagnosed("extract_url")
            def extract_url(self, **kwargs):
                return copy.deepcopy(self.output)

        for invalid in (False, True):
            with self.subTest(invalid=invalid):
                client = ContractClient(config=config, keyring=Mock())
                client.output = copy.deepcopy(PAYLOADS["extract_url"])
                if invalid:
                    client.output.pop("content")
                states = []
                original_new = client.new_request

                def new_request(*args, **kwargs):
                    state = original_new(*args, **kwargs)
                    states.append(state)
                    return state

                with patch.object(client, "new_request", side_effect=new_request), patch.object(
                    client, "finish_request", wraps=client.finish_request,
                ) as finish:
                    async with create_connected_server_and_client_session(self.build(client)) as session:
                        result = await session.call_tool("extract_url", ARGUMENTS["extract_url"])
                finish.assert_called_once()
                self.assertEqual(result.isError, invalid)
                self.assertEqual(states[0].final["outcome"], "error" if invalid else "success")
                self.assertEqual(states[0].final["failure_reason"], "ValidationError" if invalid else "")
                self.assertFalse(client._active_requests)
                self.assertFalse(client._closed)
                if not invalid:
                    self.assertEqual(result.structuredContent["diagnostics"], states[0].final["diagnostics"])
                    self.assertNotIn("diagnostics", client.output)

    async def test_real_sdk_progress_flushes_fast_completed_phases(self):
        client = StubClient()

        def action():
            with phase("discovery"):
                pass
            with phase("extraction"):
                pass

        client.actions["research"] = action
        progress = []

        async def on_progress(value, total, message):
            progress.append((value, total, message))

        async with create_connected_server_and_client_session(self.build(client)) as session:
            result = await session.call_tool("research", ARGUMENTS["research"], progress_callback=on_progress)
        self.assertFalse(result.isError)
        self.assertEqual([row[2] for row in progress], ["queued", "discovery", "extraction", "completed"])
        self.assertEqual([row[0] for row in progress], sorted({row[0] for row in progress}))
        self.assertTrue(all(row[1] is None for row in progress))

    async def test_real_sdk_reports_failed_completion_for_tool_error(self):
        client = StubClient()

        def action():
            raise MySearchError("synthetic upstream failure")

        client.actions["search"] = action
        messages = []

        async def progress(value, total, message):
            messages.append(message)

        async with create_connected_server_and_client_session(self.build(client)) as session:
            result = await session.call_tool("search", ARGUMENTS["search"], progress_callback=progress)
        self.assertTrue(result.isError)
        self.assertEqual(messages, ["queued", "failed"])
        self.assertEqual(len(client.terminals), 1)
        self.assertEqual(client.terminals[0]["outcome"], "error")
        self.assertFalse(client.closed)

    async def test_cancel_queued_call_has_one_terminal_and_never_runs_worker(self):
        client = StubClient()
        entered = Event()
        client.actions["search"] = entered.set
        limiter = anyio.to_thread.current_default_thread_limiter()
        old_total = limiter.total_tokens
        limiter.total_tokens = 1
        borrower = object()
        await limiter.acquire_on_behalf_of(borrower)
        scope = anyio.CancelScope()
        done = anyio.Event()

        async def run():
            try:
                with scope:
                    await server._offload(client, "search", query="query")
            finally:
                done.set()

        try:
            async with anyio.create_task_group() as group:
                group.start_soon(run)
                with anyio.fail_after(2):
                    while not client.states:
                        await anyio.sleep(0)
                scope.cancel()
                await anyio.sleep(0)
                limiter.release_on_behalf_of(borrower)
                borrower = None
                with anyio.fail_after(2):
                    await done.wait()
        finally:
            if borrower is not None:
                limiter.release_on_behalf_of(borrower)
            limiter.total_tokens = old_total
        self.assertFalse(entered.is_set())
        self.assertEqual(len(client.terminals), 1)
        self.assertEqual(client.terminals[0]["outcome"], "cancelled")
        self.assertTrue(client.states[0].cancelled.is_set())

    async def test_cancel_running_worker_finishes_once_before_http_close(self):
        config = SimpleNamespace(timeout_seconds=45, research_timeout_seconds=90, crawl_timeout_seconds=90,
                                 xai_social_timeout_seconds=120, max_parallel_workers=2,
                                 search_cache_ttl_seconds=60, extract_cache_ttl_seconds=60,
                                 proxy_base_url="https://proxy.test", proxy_api_key="mysp-synthetic")
        entered, release, returned, closed = Event(), Event(), Event(), Event()

        class BlockingClient(MySearchClient):
            @diagnosed("search")
            def search(self, **kwargs):
                entered.set()
                release.wait(3)
                returned.set()
                return copy.deepcopy(PAYLOADS["search"])

        client = BlockingClient(config=config, keyring=Mock())
        states = []
        original_new = client.new_request

        def new_request(*args, **kwargs):
            state = original_new(*args, **kwargs)
            states.append(state)
            return state

        scope = anyio.CancelScope()
        done, close_done = anyio.Event(), anyio.Event()
        reports = []

        def report(*args, **kwargs):
            reports.append(copy.deepcopy(kwargs["json"]))
            return Mock(raise_for_status=Mock())

        async def run():
            try:
                with scope:
                    await server._offload(client, "search", query="query")
            finally:
                done.set()

        async def close():
            await anyio.to_thread.run_sync(client.close)
            close_done.set()

        with patch.object(client, "new_request", side_effect=new_request), patch.object(client._http, "post", side_effect=report), patch.object(
            client._http, "close", side_effect=closed.set,
        ):
            try:
                async with anyio.create_task_group() as group:
                    group.start_soon(run)
                    with anyio.fail_after(2):
                        while not entered.is_set():
                            await anyio.sleep(0.005)
                    scope.cancel()
                    with anyio.fail_after(2):
                        while not states[0].cancelled.is_set():
                            await anyio.sleep(0.005)
                    group.start_soon(close)
                    await anyio.sleep(0.02)
                    self.assertFalse(closed.is_set())
                    self.assertFalse(done.is_set())
                    release.set()
                    with anyio.fail_after(2):
                        await done.wait()
                        await close_done.wait()
            finally:
                release.set()
                await anyio.to_thread.run_sync(client.close)
        self.assertTrue(returned.is_set())
        self.assertTrue(closed.is_set())
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["outcome"], "cancelled")

    async def test_native_task_cancel_keeps_http_open_until_worker_returns(self):
        config = SimpleNamespace(timeout_seconds=45, research_timeout_seconds=90, crawl_timeout_seconds=90,
                                 xai_social_timeout_seconds=120, max_parallel_workers=2,
                                 search_cache_ttl_seconds=60, extract_cache_ttl_seconds=60,
                                 proxy_base_url="https://proxy.test", proxy_api_key="mysp-synthetic")
        entered, release, returned, closed, closing = Event(), Event(), Event(), Event(), Event()

        class BlockingClient(MySearchClient):
            @diagnosed("search")
            def search(self, **kwargs):
                entered.set()
                release.wait(3)
                returned.set()
                return copy.deepcopy(PAYLOADS["search"])

        client = BlockingClient(config=config, keyring=Mock())
        states, reports = [], []
        original_new = client.new_request

        def new_request(*args, **kwargs):
            state = original_new(*args, **kwargs)
            states.append(state)
            return state

        def report(*args, **kwargs):
            reports.append(copy.deepcopy(kwargs["json"]))
            return Mock(raise_for_status=Mock())

        def close():
            closing.set()
            client.close()

        close_task = None
        with patch.object(client, "new_request", side_effect=new_request), patch.object(client._http, "post", side_effect=report), patch.object(
            client._http, "close", side_effect=closed.set,
        ):
            task = asyncio.create_task(server._offload(client, "search", query="query"))
            try:
                with anyio.fail_after(2):
                    while not entered.is_set():
                        await anyio.sleep(0.005)
                task.cancel()
                with anyio.fail_after(2):
                    while not states[0].cancelled.is_set():
                        await anyio.sleep(0.005)
                close_task = asyncio.create_task(anyio.to_thread.run_sync(close))
                with anyio.fail_after(2):
                    while not closing.is_set():
                        await anyio.sleep(0.005)
                await anyio.sleep(0.02)
                self.assertFalse(returned.is_set())
                self.assertFalse(closed.is_set(), "Native task cancellation closed HTTP beneath a running worker")
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                if close_task is not None:
                    await close_task
                else:
                    await anyio.to_thread.run_sync(client.close)
        self.assertTrue(returned.is_set())
        self.assertTrue(closed.is_set())
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["outcome"], "cancelled")

    async def test_lifespan_close_does_not_starve_cancelled_queue_finalization(self):
        config = SimpleNamespace(timeout_seconds=45, research_timeout_seconds=90, crawl_timeout_seconds=90,
                                 xai_social_timeout_seconds=120, max_parallel_workers=1,
                                 search_cache_ttl_seconds=60, extract_cache_ttl_seconds=60,
                                 proxy_base_url="", proxy_api_key="")
        client = MySearchClient(config=config, keyring=Mock())
        mcp = self.build(client)
        limiter = anyio.to_thread.current_default_thread_limiter()
        old_total = limiter.total_tokens
        limiter.total_tokens = 1
        borrower = object()
        await limiter.acquire_on_behalf_of(borrower)
        scope = anyio.CancelScope()
        done, close_done = anyio.Event(), anyio.Event()
        states = []
        original_new = client.new_request

        def new_request(*args, **kwargs):
            state = original_new(*args, **kwargs)
            states.append(state)
            return state

        async def run():
            try:
                with scope:
                    await server._offload(client, "search", query="query")
            finally:
                done.set()

        async def shutdown():
            messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

            async def receive():
                return next(messages)

            async def send(message):
                if message["type"] == "lifespan.shutdown.complete":
                    self.assertTrue(client._closed)

            await mcp.sse_app()({"type": "lifespan", "state": {}}, receive, send)
            close_done.set()

        timely = False
        with patch.object(client, "new_request", side_effect=new_request), patch.object(client, "search") as search:
            try:
                async with anyio.create_task_group() as group:
                    group.start_soon(run)
                    with anyio.fail_after(2):
                        while not states:
                            await anyio.sleep(0)
                    group.start_soon(shutdown)
                    await anyio.sleep(0.01)
                    scope.cancel()
                    await anyio.sleep(0)
                    limiter.release_on_behalf_of(borrower)
                    borrower = None
                    try:
                        with anyio.move_on_after(0.5):
                            await done.wait()
                            await close_done.wait()
                            timely = True
                    finally:
                        # Release the deliberate saturation even when the regression deadlocks.
                        limiter.total_tokens = max(2, old_total)
                        with anyio.fail_after(2):
                            await done.wait()
                            await close_done.wait()
            finally:
                if borrower is not None:
                    limiter.release_on_behalf_of(borrower)
                limiter.total_tokens = old_total
                await anyio.to_thread.run_sync(client.close)
        self.assertTrue(timely, "Shutdown occupied the worker needed to finalize a cancelled queued call")
        search.assert_not_called()
        self.assertEqual(states[0].final["outcome"], "cancelled")
        self.assertTrue(client._closed)


class OpenClawLifetimeTests(unittest.TestCase):
    def test_main_closes_client_on_success_and_error(self):
        path = Path(__file__).resolve().parents[1] / "openclaw/scripts/mysearch_openclaw.py"
        spec = importlib.util.spec_from_file_location("openclaw_lifetime_test", path)
        wrapper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wrapper)
        for fail in (False, True):
            with self.subTest(fail=fail):
                client = Mock()
                client.health.return_value = copy.deepcopy(PAYLOADS["mysearch_health"])
                if fail:
                    client.health.side_effect = MySearchError("synthetic failure")
                with patch.object(wrapper, "_load_openclaw_skill_env"), patch.object(wrapper, "_load_env_file"), patch.object(
                    wrapper, "_runtime_imports", return_value=(Mock(return_value=client), MySearchError),
                ), patch.object(sys, "argv", [str(path), "--format", "json", "health"]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    result = wrapper.main()
                self.assertEqual(result, 1 if fail else 0)
                client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
