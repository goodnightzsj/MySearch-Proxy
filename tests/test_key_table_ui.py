from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CONSOLE = REPO_ROOT / "proxy/static/js/console.js"


HARNESS = r"""
const assert = require('node:assert/strict');
const nodes = new Map();
const document = {getElementById: node};
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {innerHTML: ''});
  return nodes.get(id);
}
"""


COMMON_FUNCTIONS = ('fmtNum', 'escapeHtml', 'formatTime', 'renderSocialKeySchedule')


@unittest.skipUnless(shutil.which("node"), "Node.js is required for console behavior checks")
class KeyTableUITests(unittest.TestCase):
    def run_console(self, names: tuple[str, ...], scenario: str) -> None:
        source = CONSOLE.read_text(encoding="utf-8")
        functions = []
        for name in dict.fromkeys((*COMMON_FUNCTIONS, *names)):
            match = re.search(rf"^(?:async )?function {re.escape(name)}\(", source, re.MULTILINE)
            self.assertIsNotNone(match, name)
            start = match.start()
            end = start + re.search(r"^}$", source[start:], re.MULTILINE).end()
            functions.append(source[start:end])
        script = HARNESS + "\n".join(functions) + "\n(async () => {\n" + scenario + "\n})().catch(error => { console.error(error); process.exitCode = 1; });"
        result = subprocess.run(["node"], input=script, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_scheduling_history_and_unknown_quota_are_not_current_health(self) -> None:
        self.run_console(("getKeyAvailability", "renderKeyHealthEvidence", "renderKeyQuota", "renderAccountQuota"), r"""
const key = {active: 1, total_used: 115, total_failed: 17, last_used_at: '2026-11-20T21:55:09Z'};
assert.equal(getKeyAvailability(key).label, '可调度');
assert.match(getKeyAvailability(key).detail, /不代表实时健康/);
assert.match(renderKeyHealthEvidence(key), /有历史成功/);
assert.doesNotMatch(renderKeyHealthEvidence(key), /最新成功|实时健康|当前健康/);
assert.match(renderKeyHealthEvidence({active: 1}), /尚无成功证据/);
assert.match(renderKeyHealthEvidence({total_failed: 2}), /仅有失败记录/);
assert.match(renderKeyQuota('tavily', key), /额度未知/);
assert.match(renderAccountQuota('tavily', key), /额度未知/);
assert.doesNotMatch(renderKeyQuota('tavily', key), /NaN|剩余 0/);
""")

    def test_key_table_summary_keeps_errors_in_detail_and_zero_distinct_from_unknown(self) -> None:
        self.run_console(("getKeyAvailability", "formatDisabledDetail", "renderKeyStatusSummary",
                          "renderKeyQuotaSummary", "renderKeyHealthEvidence", "renderKeyQuota", "renderAccountQuota", "quotaBar",
                          "drawerMetric", "drawerSection", "renderDrawerActionGroup", "openKeyDetail"), r"""
const key = {id: 251, key_masked: 'fc-demo***41f3', email: 'synthetic@example.invalid', active: 0,
  disabled_reason: 'quota_exhausted', disabled_detail: 'RAW_MARKER <unsafe> ' + 'quota exhausted '.repeat(100),
  usage_account_remaining: 0, usage_sync_error: 'SYNC_MARKER <unsafe>'};
for (const service of ['tavily', 'firecrawl', 'exa']) {
  const summary = renderKeyStatusSummary(service, key);
  assert.match(summary, /额度耗尽/);
  assert.match(renderKeyQuotaSummary(key), />0<.*\n/);
  assert.match(summary, /同步异常/);
  assert.doesNotMatch(summary, /RAW_MARKER|SYNC_MARKER|<unsafe>/);
  assert.ok(summary.length < 900, 'upstream diagnostic length must not grow the main row');
  const unknown = renderKeyQuotaSummary({active: 1});
  assert.match(unknown, /未知/);
  assert.doesNotMatch(unknown, /剩余 0/);
  global.getServicePayload = () => ({keys: [key]});
  global.getServiceDisplayLabel = () => service;
  let drawer;
  global.openDetailDrawer = value => { drawer = value; };
  openKeyDetail(service, key.id);
  assert.equal(drawer.title, `${service} Key #${key.id}`);
  assert.ok(drawer.bodyHtml.includes(escapeHtml(key.email)));
  assert.ok(drawer.bodyHtml.includes(escapeHtml(key.disabled_detail.trim())));
  assert.match(drawer.bodyHtml, /SYNC_MARKER &lt;unsafe&gt;/);
  assert.doesNotMatch(drawer.bodyHtml, /<unsafe>/);
  const restricted = {...key, active: 1, blocked_operations: {contents: {reason: 'team_blocked', detail: 'ENDPOINT_MARKER <unsafe>'}}, available_operations: ['search']};
  global.getServicePayload = () => ({keys: [restricted]});
  openKeyDetail(service, key.id);
  assert.match(drawer.bodyHtml, /ENDPOINT_MARKER &lt;unsafe&gt;/);
}
""")

    def test_shared_key_rows_bound_email_and_preserve_actions_and_keyboard_entry(self) -> None:
        self.run_console(("getKeyAvailability", "getKeyRemaining", "getKeyRowClass", "formatDisabledDetail",
                          "renderKeyStatusSummary", "renderKeyQuotaSummary", "renderKeyUsageSummary", "renderKeyHealthEvidence", "renderKeys"), r"""
global.syncKeyToolbar = () => {};
global.getFilteredKeys = (service, keys) => keys;
global.getKeyTableState = () => ({page: 1});
global.getKeyPageSize = () => 10;
global.getServiceDisplayLabel = service => service;
const key = {id: 7, key_masked: 'fc-demo***f93b', email: 'long.<unsafe>@' + 'department'.repeat(20) + '.example.invalid',
  active: 1, usage_account_remaining: 993, total_used: 115, total_failed: 17};
for (const service of ['tavily', 'firecrawl', 'exa']) {
  renderKeys(service, [key]);
  const row = node(`keys-body-${service}`).innerHTML;
  assert.match(row, /class="key-email"/);
  assert.ok(row.includes(`title="${escapeHtml(key.email)}"`));
  assert.doesNotMatch(row, /<unsafe>/);
  assert.doesNotMatch(row, /role="button"|tabindex="0"/);
  assert.match(row, /<button class="key-detail-button" type="button" aria-label=/);
  assert.ok(row.includes(`openKeyDetail('${service}', 7)`));
  assert.ok(row.includes(`event.stopPropagation(); toggleKey('${service}', 7, 0)`));
  assert.match(row, />993<\/div>/);
  assert.match(row, /data-label="剩余额度" class="key-quota-cell"/);
  assert.equal((row.match(/<td /g) || []).length, 5);
  assert.match(row, /#7/);
  assert.doesNotMatch(row, /data-label="历史证据"/);
  renderKeys(service, [{...key, blocked_operations: {contents: {reason: 'team_blocked'}}, available_operations: ['search']}]);
  assert.match(node(`keys-body-${service}`).innerHTML, /恢复端点/);
}
""")

    def test_social_key_lists_separate_identity_status_and_recovery(self) -> None:
        self.run_console((), r"""
const keys = [
  {id: 'ready', index: 1, key_masked: 'xai-fixture***aaaa', schedulable: true},
  {id: 'cooling', index: 2, key_masked: 'xai-fixture***bbbb', schedulable: false,
    disabled_reason: 'rate_limited', schedule_until: '2999-01-01T00:00:00Z'},
  {id: 'empty', index: 3, key_masked: '<unsafe>***cccc', schedulable: false, disabled_reason: 'quota_exhausted'},
  {id: 'invalid', index: 4, key_masked: 'xai-fixture***dddd', schedulable: false, disabled_reason: 'auth_rejected'},
];
for (const mode of ['local', 'upstream']) {
  renderSocialKeySchedule(keys, mode, true);
  const html = node(`settings-social-${mode}-key-list`).innerHTML;
  assert.match(html, /key-identity-line/);
  assert.match(html, /可调度/);
  assert.match(html, /限流冷却/);
  assert.match(html, /额度耗尽/);
  assert.match(html, /凭证失效/);
  assert.match(html, /自动恢复/);
  assert.match(html, /&lt;unsafe&gt;/);
  assert.doesNotMatch(html, /<unsafe>|正常/);
  assert.equal((html.match(/<button/g) || []).length, 3);
  assert.ok(html.includes("resumeSocialUpstreamKey('cooling', this)"));
  renderSocialKeySchedule(keys, mode, false);
  const inactive = node(`settings-social-${mode}-key-list`).innerHTML;
  assert.match(inactive, /已保存，当前未启用/);
  assert.doesNotMatch(inactive, /<button/);
}
""")

    def test_quota_unknown_zero_and_stale_are_distinct(self) -> None:
        self.run_console(("renderKeyQuotaSummary",), r"""
assert.match(renderKeyQuotaSummary({}), /未知/);
assert.match(renderKeyQuotaSummary({usage_account_remaining: 0}), /已耗尽/);
assert.match(renderKeyQuotaSummary({usage_account_remaining: 993, usage_sync_stale: true,
  usage_synced_at: '2026-11-20T14:00:00Z'}), /记录已过期/);
assert.match(renderKeyQuotaSummary({usage_account_remaining: 993, usage_sync_stale: false,
  usage_synced_at: '2026-11-21T14:00:00Z'}), /最近同步/);
""")
