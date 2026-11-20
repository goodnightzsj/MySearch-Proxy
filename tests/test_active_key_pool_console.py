from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path


CONSOLE = Path(__file__).resolve().parents[1] / "proxy/static/js/console.js"


@unittest.skipUnless(shutil.which("node"), "Node.js is required for console behavior checks")
class ActiveKeyPoolConsoleTests(unittest.TestCase):
    def run_console(self, names, scenario):
        source = CONSOLE.read_text(encoding="utf-8")
        functions = []
        for name in names:
            match = re.search(rf"^(?:async )?function {re.escape(name)}\(", source, re.MULTILINE)
            self.assertIsNotNone(match, name)
            start = match.start()
            end = start + re.search(r"^}$", source[start:], re.MULTILINE).end()
            functions.append(source[start:end])
        script = "const assert = require('node:assert/strict');\n" + "\n".join(functions)
        script += "\n(async () => {\n" + scenario + "\n})().catch(error => { console.error(error); process.exitCode = 1; });"
        result = subprocess.run(["node"], input=script, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_exa_operation_limits_show_scope_and_allow_explicit_restore(self):
        self.run_console(("getKeyAvailability", "formatDisabledDetail", "renderKeyStatusSummary",
                          "escapeHtml", "formatTime", "fmtNum", "toggleKey"), r"""
const key = {id: 42, active: 1, blocked_operations: {contents: {reason: 'team_blocked', detail: '<fixture>'}}, available_operations: ['search']};
assert.equal(getKeyAvailability(key).schedulable, true);
assert.equal(getKeyAvailability(key).label, '部分端点受限');
assert.match(formatDisabledDetail(key), /contents.*TEAM_BLOCKED/);
assert.match(renderKeyStatusSummary('exa', key), /&lt;fixture&gt;/);
assert.doesNotMatch(renderKeyStatusSummary('exa', key), /<fixture>/);
key.blocked_operations.search = {reason: 'team_blocked'};
key.available_operations = [];
assert.equal(getKeyAvailability(key).schedulable, false);
assert.equal(getKeyAvailability(key).actionLabel, '恢复端点');
key.active = 0;
key.disabled_reason = 'manual';
assert.equal(getKeyAvailability(key).label, '手动禁用');
const calls = [];
global.api = async (...args) => { calls.push(args); return {ok: true}; };
global.refresh = async () => {};
global.getRefreshScopeForService = service => ({service});
await toggleKey('exa', 42, 1);
assert.deepEqual(calls, [['PUT', '/api/keys/42/toggle', {active: 1}]]);
""")

    def test_normal_filter_excludes_cooldown_disabled_and_partial_access(self):
        self.run_console(("getKeyAvailability", "getFilteredKeys", "parseTimeValue", "getKeyRiskScore",
                          "getKeyRemaining", "getKeyRowClass", "hasKeyIssue", "formatTime"), r"""
const state = {search: '', filter: 'active', sort: 'recent'};
global.getKeyTableState = () => state;
const keys = [
  {id: 1, active: 1},
  {id: 2, active: 1, schedule_until: '2999-01-01T00:00:00Z'},
  {id: 3, active: 1, blocked_operations: {contents: {reason: 'team_blocked'}}, available_operations: ['search']},
  {id: 4, active: 0, disabled_reason: 'manual'},
];
assert.deepEqual(getFilteredKeys('exa', keys).map(key => key.id), [1]);
state.filter = 'issue';
assert.deepEqual(getFilteredKeys('exa', keys).map(key => key.id), [2, 3, 4]);
state.filter = 'disabled';
assert.deepEqual(getFilteredKeys('exa', keys).map(key => key.id), [4]);
state.filter = 'all';
assert.equal(getFilteredKeys('exa', keys).length, 4);
""")
