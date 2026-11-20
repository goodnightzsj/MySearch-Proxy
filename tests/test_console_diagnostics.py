from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = (
    "escapeHtml", "fmtNum", "formatTime", "drawerMetric", "drawerSection", "setStatus",
    "diagnosticDuration", "renderDiagnosticDistribution", "renderDiagnosticSummary",
    "renderDiagnosticUsage", "renderDiagnosticAttempt", "renderDiagnosticRequest",
    "renderRequestDiagnostics", "loadRequestDiagnostics", "openRequestDiagnostics",
    "openDetailDrawer", "closeDetailDrawer",
)


HARNESS = r"""
const assert = require('node:assert/strict');
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) {
    const classes = new Set();
    nodes.set(id, {id, innerHTML: '', textContent: '', disabled: false, attributes: {}, dataset: {},
      classList: {
        add: (...names) => names.forEach(name => classes.add(name)),
        remove: (...names) => names.forEach(name => classes.delete(name)),
        contains: name => classes.has(name),
        toggle(name, force) { if (force) classes.add(name); else classes.delete(name); },
      },
      setAttribute(name, value) { this.attributes[name] = value; },
      focus() { document.activeElement = this; },
    });
  }
  return nodes.get(id);
}
const document = {getElementById: node, activeElement: null};
const focusMemory = {};
const rememberOverlayFocus = id => { focusMemory[id] = document.activeElement; };
const restoreOverlayFocus = id => focusMemory[id]?.focus();
const syncOverlayState = () => {};
const focusOverlay = () => {};
let diagnosticsLoadGeneration = 0;
let PAGE_KIND = 'console';
let activeService = 'tavily';
const getServiceDisplayLabel = service => service;
let api = async () => { throw new Error('API test not configured'); };
function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return {promise, resolve, reject};
}
function summary(count, p50, p95) {
  return {count, success_count: count, failure_count: 0, degraded_count: 0,
    outcomes: {success: count}, failure_reasons: {}, p50_ms: p50, p95_ms: p95};
}
function payload() {
  return {hours: 24, service: 'mysearch', since: '2026-11-17 00:00:00',
    logical_requests: summary(1, 400, 400), physical_attempts: summary(2, 100, 300),
    recent_requests: [], recent_attempts: []};
}
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is required for console behavior checks")
class ConsoleDiagnosticTests(unittest.TestCase):
    def run_console(self, scenario: str) -> None:
        source = (ROOT / "proxy/static/js/console.js").read_text(encoding="utf-8")
        functions = []
        for name in FUNCTIONS:
            match = re.search(rf"^(?:async )?function {name}\(", source, re.MULTILINE)
            self.assertIsNotNone(match, name)
            start = match.start()
            end = start + re.search(r"^}$", source[start:], re.MULTILINE).end()
            functions.append(source[start:end])
        script = HARNESS + "\n".join(functions) + "\n(async () => {\n" + scenario + "\n})().catch(error => { console.error(error); process.exitCode = 1; });"
        result = subprocess.run(["node"], input=script, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_render_separates_logical_attempts_and_usage_units_without_private_fields(self) -> None:
        self.run_console(r"""
const data = payload();
data.physical_attempts.failure_reasons = {auth_rejected: 1};
const attempt = {provider: 'exa', operation: 'search', outcome: 'success', elapsed_ms: 300,
  status_code: 200, phase: 'search', request_id: 'request-fixture', call_id: 'call-fixture',
  usage: [{name: 'cost', unit: 'usd', value: 0.000001}], query: 'PRIVATE_QUERY', key: 'PRIVATE_KEY', url: 'PRIVATE_URL'};
data.recent_requests = [{request_id: 'request-fixture', tool: 'search', outcome: 'success',
  started_at: '2026-11-17T00:00:00Z', elapsed_ms: 400, cache_hit: null,
  diagnostics: {call_count: 1, calls: [{call_id: 'runtime-call-fixture', provider: 'exa', operation: 'search', outcome: 'success', elapsed_ms: 400}], usage: [
    {provider: 'exa', name: 'cost', unit: 'usd', value: 0.000001},
    {provider: 'tavily', name: 'credits', unit: 'credits', value: 0},
  ]}, attempts: [attempt], query: 'PRIVATE_QUERY', key: 'PRIVATE_KEY', url: 'PRIVATE_URL'}];
data.recent_attempts = [attempt];
const html = renderRequestDiagnostics(data);
assert.ok(html.includes('逻辑请求') && html.includes('物理尝试'));
assert.ok(html.includes('400 ms') && html.includes('100 ms') && html.includes('300 ms'));
assert.ok(html.includes('auth_rejected') && html.includes('call-fixture'));
assert.ok(html.includes('运行时调用') && html.includes('runtime-call-fixture'));
assert.ok(html.includes('exa · cost：0.000001 usd'));
assert.ok(html.includes('tavily · credits：0 credits'));
assert.ok(html.includes('缓存：未知'));
for (const value of ['PRIVATE_QUERY', 'PRIVATE_KEY', 'PRIVATE_URL']) assert.ok(!html.includes(value));
assert.match(renderDiagnosticUsage([]), /用量未知/);
assert.equal(diagnosticDuration(null), '未知');
assert.equal(diagnosticDuration(0), '0 ms');
assert.match(renderDiagnosticUsage([{name: 'cost', unit: 'usd', value: null}], 'exa'), /未知 usd/);
assert.match(renderDiagnosticDistribution({'<img src=x>': 1}, ''), /&lt;img/);
assert.ok(!renderDiagnosticAttempt({...attempt, failure_reason: '<script>bad</script>'}).includes('<script>'));
""")

    def test_recent_request_attempts_are_bounded_and_explicitly_truncated(self) -> None:
        self.run_console(r"""
const attempts = Array.from({length: 21}, (_, index) => ({provider: 'exa', operation: 'search',
  outcome: 'success', elapsed_ms: index, request_id: 'request', call_id: `call-${index}-fixture`, usage: []}));
const html = renderDiagnosticRequest({tool: 'research', outcome: 'degraded', elapsed_ms: 1000,
  diagnostics: {call_count: 129, calls_truncated: 1, usage: []}, attempts});
assert.ok(html.includes('call-19-fixture'));
assert.ok(!html.includes('call-20-fixture'));
assert.ok(html.includes('仅显示前 20 条'));
assert.ok(html.includes('运行时调用明细已截断'));
""")

    def test_phase_summary_excludes_enclosing_tool_and_filter_reaches_server(self) -> None:
        self.run_console(r"""
const html = renderDiagnosticRequest({tool: 'search', diagnostics: {phases: [
  {name: 'search', elapsed_ms: 10000}, {name: 'request', elapsed_ms: 10000},
  {name: 'primary', elapsed_ms: 100}, {name: 'extract_url', elapsed_ms: 900},
] }});
assert.match(html.split('</summary>')[0], /最慢已记录阶段.*extract_url.*900 ms/);
assert.match(renderDiagnosticRequest({tool: 'search'}), /最慢已记录阶段.*未知/);
node('diagnostics-hours').value = '1';
node('diagnostics-outcome').value = 'deadline';
const calls = [];
api = async (...args) => { calls.push(args); return payload(); };
await loadRequestDiagnostics('exa');
assert.equal(calls[0][1], '/api/diagnostics?service=exa&hours=1&limit=20&outcome=deadline');
""")

    def test_open_reads_only_diagnostics_and_restores_focus(self) -> None:
        self.run_console(r"""
const calls = [];
api = async (...args) => { calls.push(args); return payload(); };
document.activeElement = node('diagnostics-trigger');
await openRequestDiagnostics();
assert.deepEqual(calls, [['GET', '/api/diagnostics?service=tavily&hours=24&limit=20']]);
assert.equal(document.activeElement, node('detail-drawer-title'));
assert.equal(node('detail-drawer-title').attributes.tabindex, '-1');
assert.equal(node('diagnostics-refresh').disabled, false);
assert.equal(node('diagnostics-content').attributes['aria-busy'], 'false');
closeDetailDrawer();
assert.equal(document.activeElement, node('diagnostics-trigger'));
PAGE_KIND = 'mysearch';
await openRequestDiagnostics();
assert.equal(calls.at(-1)[1], '/api/diagnostics?service=mysearch&hours=24&limit=20');
""")

    def test_load_error_retry_and_old_generation_do_not_replace_new_content(self) -> None:
        self.run_console(r"""
const first = deferred();
api = () => first.promise;
const oldOpen = openRequestDiagnostics();
assert.equal(node('diagnostics-refresh').disabled, true);
closeDetailDrawer();
const second = deferred();
api = () => second.promise;
activeService = 'exa';
const currentOpen = openRequestDiagnostics();
second.resolve(payload());
await currentOpen;
const currentHtml = node('diagnostics-content').innerHTML;
const stale = payload();
stale.logical_requests = summary(999, 999, 999);
first.resolve(stale);
await oldOpen;
assert.equal(node('diagnostics-content').innerHTML, currentHtml);
api = async () => { throw new Error('https://private.example/?key=PRIVATE_KEY'); };
await loadRequestDiagnostics('exa');
assert.equal(node('diagnostics-content').innerHTML, currentHtml);
assert.match(node('diagnostics-status').textContent, /读取请求诊断失败/);
assert.ok(!node('diagnostics-status').textContent.includes('PRIVATE_KEY'));
assert.equal(node('diagnostics-refresh').disabled, false);
assert.equal(node('diagnostics-refresh').textContent, '重试读取诊断');
api = async () => payload();
await loadRequestDiagnostics('exa');
assert.equal(node('diagnostics-refresh').textContent, '刷新诊断');
const late = deferred();
api = () => late.promise;
const pending = loadRequestDiagnostics('exa');
openDetailDrawer({title: 'Token details', bodyHtml: 'token-fixture'});
late.resolve(stale);
await pending;
assert.equal(node('detail-drawer-body').innerHTML, 'token-fixture');
""")

    def test_both_pages_render_one_accessible_diagnostics_button(self) -> None:
        class Buttons(HTMLParser):
            def __init__(self):
                super().__init__()
                self.buttons = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "button" and attrs.get("onclick") == "openRequestDiagnostics()":
                    self.buttons.append(attrs)

        env = Environment(loader=FileSystemLoader(ROOT / "proxy/templates"))
        for page in ("console.html", "mysearch.html"):
            with self.subTest(page=page):
                html = env.get_template(page).render(initial_authenticated=True, url_for=lambda _, path: path)
                parsed = Buttons()
                parsed.feed(html)
                self.assertEqual(len(parsed.buttons), 1)
                self.assertEqual(parsed.buttons[0]["type"], "button")
                self.assertIn("请求诊断", parsed.buttons[0]["aria-label"])


if __name__ == "__main__":
    unittest.main()
