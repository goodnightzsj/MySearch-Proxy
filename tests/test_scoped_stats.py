from __future__ import annotations

import asyncio
import importlib.util
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "proxy") not in sys.path:
    sys.path.insert(0, str(ROOT / "proxy"))
spec = importlib.util.spec_from_file_location("scoped_stats_test_server", ROOT / "proxy/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class ScopedStatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server.reset_stats_cache()
        self.service = AsyncMock(side_effect=lambda name, **kwargs: {"service": name})
        self.social = AsyncMock(return_value={"service": "social"})
        self.mysearch = AsyncMock(return_value={"service": "mysearch"})
        for name, replacement in (
            ("build_service_dashboard", self.service),
            ("build_social_dashboard", self.social),
            ("build_mysearch_dashboard", self.mysearch),
            ("STATS_CACHE_TTL_SECONDS", 30),
        ):
            patcher = patch.object(server, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        server.app.dependency_overrides[server.verify_admin] = lambda: True
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        server.app.dependency_overrides.clear()
        server.reset_stats_cache()

    async def get(self, query=""):
        return await self.client.get(f"/api/stats{query}")

    async def test_no_scope_keeps_full_response_and_existing_metadata(self):
        response = await self.get()
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"services", "social", "mysearch", "meta"})
        self.assertEqual(set(payload["services"]), {"tavily", "firecrawl", "exa"})
        self.assertEqual(set(payload["meta"]), {"generated_at", "stats_cache_ttl_seconds", "dashboard_auto_sync_on_stats", "exa_enabled"})
        self.assertEqual(self.service.await_count, 3)
        self.social.assert_awaited_once()
        self.mysearch.assert_awaited_once()

    async def test_scoped_response_builds_only_selected_sections_and_deduplicates(self):
        response = await self.get("?service=exa&service=mysearch&service=exa")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"services", "mysearch", "meta"})
        self.assertEqual(response.json()["services"], {"exa": {"service": "exa"}})
        self.service.assert_awaited_once_with("exa", auto_sync=server.DASHBOARD_AUTO_SYNC_ON_STATS)
        self.social.assert_not_awaited()
        self.mysearch.assert_awaited_once()
        social = await self.get("?service=social")
        self.assertEqual(set(social.json()), {"social", "meta"})
        self.assertEqual(self.service.await_count, 1)

    async def test_empty_or_unknown_service_is_400_without_building(self):
        for query in ("?service=", "?service=unknown", "?service=exa&service=", "?service=exa,mysearch"):
            response = await self.get(query)
            self.assertEqual(response.status_code, 400, query)
        self.service.assert_not_awaited()
        self.social.assert_not_awaited()
        self.mysearch.assert_not_awaited()

    async def test_cache_is_canonical_and_separate_for_different_sections(self):
        await self.get("?service=exa&service=mysearch")
        await self.get("?service=mysearch&service=exa&service=exa")
        self.assertEqual(self.service.await_count, 1)
        self.assertEqual(self.mysearch.await_count, 1)
        await self.get("?service=firecrawl")
        self.assertEqual(self.service.await_count, 2)
        await self.get("?service=exa&service=mysearch&force=1")
        self.assertEqual(self.service.await_count, 3)
        server.reset_stats_cache()
        await self.get("?service=firecrawl")
        await self.get("?service=exa&service=mysearch")
        self.assertEqual(self.service.await_count, 5)
        self.social.assert_not_awaited()

    async def test_slow_full_request_does_not_lock_unrelated_scoped_request(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_social():
            started.set()
            await release.wait()
            return {"service": "social"}

        self.social.side_effect = slow_social
        full = asyncio.create_task(self.get())
        try:
            await asyncio.wait_for(started.wait(), 1)
            scoped = await asyncio.wait_for(self.get("?service=exa"), 0.3)
            self.assertEqual(scoped.status_code, 200)
            self.assertFalse(full.done())
        finally:
            release.set()
            await full

    async def test_same_scope_concurrent_reads_share_one_build(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def build(name, **kwargs):
            started.set()
            await release.wait()
            return {"service": name}

        self.service.side_effect = build
        first = asyncio.create_task(self.get("?service=exa"))
        await asyncio.wait_for(started.wait(), 1)
        second = asyncio.create_task(self.get("?service=exa&service=exa"))
        release.set()
        responses = await asyncio.gather(first, second)
        self.assertEqual(responses[0].json(), responses[1].json())
        self.service.assert_awaited_once()

    async def test_reset_during_build_does_not_repopulate_stale_cache(self):
        started, release = asyncio.Event(), asyncio.Event()
        count = 0

        async def build(name, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                started.set()
                await release.wait()
            return {"service": name, "version": count}

        self.service.side_effect = build
        first = asyncio.create_task(self.get("?service=exa"))
        await asyncio.wait_for(started.wait(), 1)
        server.reset_stats_cache()
        release.set()
        self.assertEqual((await first).json()["services"]["exa"]["version"], 1)
        self.assertEqual((await self.get("?service=exa")).json()["services"]["exa"]["version"], 2)

    async def test_failed_build_leaves_other_scope_cache_available(self):
        original = await self.get("?service=exa")
        self.social.side_effect = RuntimeError("synthetic unavailable")
        with self.assertRaisesRegex(RuntimeError, "synthetic unavailable"):
            await self.get("?service=social")
        cached = await self.get("?service=exa")
        self.assertEqual(cached.json(), original.json())
        self.service.assert_awaited_once()


HARNESS = r"""
const assert = require('node:assert/strict');
const SERVICE_META = {tavily: {}, firecrawl: {}, exa: {}};
let latestServices = {tavily: {version: 'original-tavily'}, exa: {version: 'original-exa'}};
let latestSocial = {version: 'original-social'};
let latestMySearch = {version: 'original-mysearch'};
let latestStatsMeta = {generated_at: 'original'};
let statsRefreshGeneration = 0;
let statsMetaGeneration = 0;
const statsSectionRequests = {};
const rendered = [], messages = [], requests = [];
const renderDashboardScope = scope => rendered.push(scope);
const setDashboardError = message => messages.push(message);
const showToast = () => {};
let api = async () => ({});
function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return {promise, resolve, reject};
}
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is required for console behavior checks")
class ScopedStatsConsoleTests(unittest.TestCase):
    def run_console(self, scenario):
        source = (ROOT / "proxy/static/js/console.js").read_text(encoding="utf-8")
        functions = []
        for name in ("normalizeRefreshScope", "getRefreshScopeForService", "refresh", "refreshAfterMutation"):
            match = re.search(rf"^(?:async )?function {name}\(", source, re.MULTILINE)
            start = match.start()
            end = start + re.search(r"^}$", source[start:], re.MULTILINE).end()
            functions.append(source[start:end])
        script = HARNESS + "\n".join(functions) + "\n(async () => {\n" + scenario + "\n})().catch(error => { console.error(error); process.exitCode = 1; });"
        result = subprocess.run(["node"], input=script, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mutation_refresh_reads_only_affected_sections_and_preserves_others(self):
        self.run_console(r"""
api = async (method, url) => {
  requests.push(url);
  return {services: {exa: {version: 'new-exa'}}, mysearch: {version: 'new-mysearch'}, meta: {generated_at: 'new'}};
};
assert.equal(await refreshAfterMutation('Token updated', {force: true, scope: getRefreshScopeForService('exa')}), true);
const url = new URL(requests[0], 'http://test');
assert.deepEqual(url.searchParams.getAll('service'), ['exa', 'mysearch']);
assert.equal(url.searchParams.get('force'), '1');
assert.deepEqual(latestServices, {tavily: {version: 'original-tavily'}, exa: {version: 'new-exa'}});
assert.equal(latestSocial.version, 'original-social');
assert.equal(latestMySearch.version, 'new-mysearch');
assert.equal(rendered[0].core, true);
assert.deepEqual(rendered[0].services, ['exa']);
assert.equal(rendered[0].social, false);
""")

    def test_failed_read_preserves_state_and_reports_mutation_status(self):
        self.run_console(r"""
const before = JSON.stringify([latestServices, latestSocial, latestMySearch, latestStatsMeta]);
api = async () => { throw new Error('unavailable'); };
assert.equal(await refreshAfterMutation('Token updated', {scope: getRefreshScopeForService('exa')}), false);
assert.equal(JSON.stringify([latestServices, latestSocial, latestMySearch, latestStatsMeta]), before);
assert.equal(rendered.length, 0);
assert.match(messages[0], /Token updated.*unavailable/);
""")

    def test_out_of_order_full_response_updates_only_non_overlapping_sections(self):
        self.run_console(r"""
const older = deferred(), newer = deferred();
api = async (method, url) => { requests.push(url); return requests.length === 1 ? older.promise : newer.promise; };
const first = refresh();
const second = refresh({scope: getRefreshScopeForService('exa')});
newer.resolve({services: {exa: {version: 'new-exa'}}, mysearch: {version: 'new-mysearch'}, meta: {generated_at: 'new'}});
await second;
older.resolve({services: {exa: {version: 'old-exa'}, tavily: {version: 'old-tavily'}}, social: {version: 'old-social'}, mysearch: {version: 'old-mysearch'}, meta: {generated_at: 'old'}});
await first;
assert.equal(requests[0], '/api/stats');
assert.equal(latestServices.exa.version, 'new-exa');
assert.equal(latestMySearch.version, 'new-mysearch');
assert.equal(latestServices.tavily.version, 'old-tavily');
assert.equal(latestSocial.version, 'old-social');
assert.equal(latestStatsMeta.generated_at, 'new');
assert.deepEqual(rendered[1].services, ['tavily']);
""")

    def test_newer_failed_request_does_not_allow_old_overlapping_result(self):
        self.run_console(r"""
const older = deferred();
let calls = 0;
api = async () => { if (++calls === 1) return older.promise; throw new Error('new read failed'); };
const first = refresh({scope: getRefreshScopeForService('exa')});
await assert.rejects(refresh({scope: getRefreshScopeForService('exa')}), /new read failed/);
older.resolve({services: {exa: {version: 'stale'}}});
await first;
assert.equal(latestServices.exa.version, 'original-exa');
assert.equal(rendered.length, 0);
""")


if __name__ == "__main__":
    unittest.main()
