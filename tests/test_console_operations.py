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
function node(id) {
  if (!nodes.has(id)) {
    const classes = new Set();
    nodes.set(id, {
      id, value: '', checked: false, disabled: false, textContent: '', innerHTML: '',
      dataset: {}, attributes: {}, isConnected: true, inert: false,
      classList: {
        add: (...items) => items.forEach(item => classes.add(item)),
        remove: (...items) => items.forEach(item => classes.delete(item)),
        contains: item => classes.has(item),
        toggle(item, force) {
          const add = force === undefined ? !classes.has(item) : force;
          if (add) classes.add(item); else classes.delete(item);
          return add;
        },
      },
      setAttribute(name, value) { this.attributes[name] = value; },
      removeAttribute(name) { delete this.attributes[name]; },
      closest: () => null,
      querySelector: () => null,
      querySelectorAll(selector) {
        return selector === 'fieldset'
          ? ['console', 'tavily', 'social'].map(kind => node(`settings-${kind}-fields`))
          : [];
      },
    });
  }
  return nodes.get(id);
}
const document = {getElementById: node, querySelectorAll: () => []};
const timers = [];
const setTimeout = callback => timers.push(callback);
const sleep = async () => {};
const flushTimers = () => { while (timers.length) timers.shift()(); };
function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return {promise, resolve, reject};
}
const storage = new Map();
const localStorage = {removeItem: key => storage.delete(key), setItem: (key, value) => storage.set(key, value)};
const STORAGE_KEY = 'multi_service_proxy_pwd';
const LEGACY_STORAGE_KEY = 'tavily_proxy_pwd';
let PWD = '';
let settingsLoadGeneration = 0;
let latestSettings = {};
const pendingMutations = new Set();
const BUTTON_MIN_BUSY_MS = 0;
const SERVICE_META = {tavily: {label: 'Tavily', syncSupported: true}};
const toasts = [];
const showToast = (message, type) => toasts.push({message, type});
let api = async () => ({});
let refresh = async () => {};
let showConfirmDialog = async () => true;
const closeDetailDrawer = () => {};
const syncOverlayState = () => {};
const restoreOverlayFocus = () => {};
const renderSettingsSummaries = () => {};
const getRefreshScopeForService = service => ({service});
const setQuickstartTab = () => {};
const event = button => ({preventDefault() {}, submitter: button});
"""


COMMON_FUNCTIONS = (
    "setStatus", "setDashboardError", "refreshAfterMutation", "clearSettingsProbe",
    "ensureButtonLabel", "resetButtonState", "flashButtonState", "runWithBusyButton",
    "describeConfiguredSecret", "tavilyModeLabel", "tavilyModeSourceLabel", "socialModeLabel",
    "socialAdminVersionLabel", "socialTokenSourceLabel", "setTavilyMode", "setSocialMode",
    "fmtNum", "escapeHtml", "formatTime", "renderSocialKeySchedule", "renderSocialSettingsMeta",
    "fillTavilySettingsForm", "fillSocialSettingsForm", "fillSettingsForm",
    "collectTavilySettingsForm", "collectSocialSettingsForm",
)


@unittest.skipUnless(shutil.which("node"), "Node.js is required for console behavior checks")
class ConsoleOperationTests(unittest.TestCase):
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

    def test_exa_operation_limits_show_scope_and_allow_explicit_restore(self) -> None:
        self.run_console(("getKeyAvailability", "formatDisabledDetail", "renderKeyStatusSummary", "toggleKey"), r"""
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
api = async (...args) => { calls.push(args); return {ok: true}; };
await toggleKey('exa', 42, 1, node('restore'));
assert.deepEqual(calls, [['PUT', '/api/keys/42/toggle', {active: 1}]]);
""")

    def test_normal_key_filter_excludes_cooldown_disabled_and_partial_access(self) -> None:
        self.run_console(("getKeyAvailability", "getFilteredKeys", "parseTimeValue", "getKeyRiskScore",
                          "getKeyRemaining", "getKeyRowClass", "hasKeyIssue"), r"""
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

    def test_row_keyboard_leaves_nested_button_activation_alone(self) -> None:
        self.run_console(("handleTableRowKey",), r"""
const opened = [];
global.openTokenDetail = (...args) => opened.push(['token', ...args]);
global.openKeyDetail = (...args) => opened.push(['key', ...args]);
const row = node('row');
for (const key of ['Enter', ' ']) {
  let prevented = false;
  handleTableRowKey({key, target: node('copy'), currentTarget: row, preventDefault() { prevented = true; }}, 'token', 'tavily', 1);
  assert.equal(prevented, false);
  assert.equal(opened.length, 0);
}
let prevented = false;
handleTableRowKey({key: 'Enter', target: row, currentTarget: row, preventDefault() { prevented = true; }}, 'key', 'tavily', 2);
assert.equal(prevented, true);
assert.deepEqual(opened, [['key', 'tavily', 2]]);
""")

    def test_password_change_keeps_only_the_server_session(self) -> None:
        self.run_console(("changePwd", "clearStoredPasswords", "headers", "initConsole"), r"""
PWD = 'old-fixture';
storage.set(STORAGE_KEY, 'old-fixture');
storage.set(LEGACY_STORAGE_KEY, 'legacy-fixture');
node('settings-new-pwd').value = 'new-fixture';
const calls = [];
api = async (...args) => { calls.push(args); return {ok: true}; };
await changePwd(event(node('save-password')));
assert.deepEqual(calls, [['PUT', '/api/password', {password: 'new-fixture'}]]);
assert.equal(PWD, '');
assert.equal(storage.size, 0);
assert.equal(headers()['X-Admin-Password'], undefined);
assert.equal(node('settings-new-pwd').value, '');
assert.equal(node('settings-console-fields').disabled, false);
flushTimers();
node('settings-new-pwd').value = 'retry-fixture';
api = async () => { throw new Error('write unavailable'); };
await changePwd(event(node('save-password')));
assert.equal(node('settings-new-pwd').value, 'retry-fixture');
assert.match(node('settings-password-status').textContent, /保存密码失败/);
PWD = 'obsolete-fixture';
storage.set(STORAGE_KEY, 'obsolete-fixture');
global.INITIAL_AUTHENTICATED = true;
global.showDashboard = () => {};
await initConsole();
assert.equal(PWD, '');
assert.equal(storage.size, 0);
""")

    def test_overlay_focus_skips_controls_disabled_by_fieldset(self) -> None:
        self.run_console(("getFocusableElements",), r"""
const enabled = {matches: () => false, classList: {contains: () => false}, offsetParent: {}};
const disabled = {matches: selector => selector === ':disabled', classList: {contains: () => false}, offsetParent: {}};
assert.deepEqual(getFocusableElements({querySelectorAll: () => [enabled, disabled]}), [enabled]);
""")

    def test_settings_loading_error_retry_and_late_response(self) -> None:
        self.run_console(("loadSettings", "closeSettingsModal"), r"""
const first = deferred();
api = () => first.promise;
const oldLoad = loadSettings();
assert.equal(node('settings-tavily-fields').disabled, true);
assert.equal(node('settings-modal').attributes['aria-busy'], 'true');
closeSettingsModal();
const second = deferred();
api = () => second.promise;
const currentLoad = loadSettings();
second.resolve({tavily: {upstream_base_url: 'https://current.example'}, social: {}});
await currentLoad;
node('settings-tavily-upstream-base-url').value = 'https://new-draft.example';
first.resolve({tavily: {upstream_base_url: 'https://late.example'}, social: {}});
await oldLoad;
assert.equal(node('settings-tavily-upstream-base-url').value, 'https://new-draft.example');
assert.equal(node('settings-tavily-fields').disabled, false);
api = async () => { throw new Error('settings offline'); };
await loadSettings();
assert.equal(node('settings-social-fields').disabled, true);
assert.equal(node('settings-load-retry').classList.contains('hidden'), false);
assert.match(node('settings-load-status').textContent, /settings offline/);
api = async () => ({tavily: {}, social: {}});
await loadSettings();
assert.equal(node('settings-social-fields').disabled, false);
assert.equal(node('settings-load-retry').classList.contains('hidden'), true);
assert.equal(node('settings-modal').attributes['aria-busy'], 'false');
""")

    def test_provider_saves_and_key_recovery_preserve_other_drafts(self) -> None:
        self.run_console(("saveTavilySettings", "saveSocialSettings", "resumeSocialUpstreamKey", "closeSettingsModal"), r"""
latestSettings = {tavily: {mode: 'auto'}, social: {model: 'existing-social'}};
node('settings-social-model').value = 'unsaved-social';
node('settings-social-upstream-api-key').value = 'unsaved-fixture';
node('settings-social-clear-local-api-key').checked = true;
const save = deferred();
api = () => save.promise;
const saving = saveTavilySettings(event(node('save-tavily')));
assert.equal(node('settings-tavily-fields').disabled, true);
assert.equal(node('settings-social-fields').disabled, false);
save.resolve({tavily: {mode: 'pool'}, social: {model: 'server-social'}});
await saving;
assert.equal(node('settings-social-model').value, 'unsaved-social');
assert.equal(node('settings-social-upstream-api-key').value, 'unsaved-fixture');
assert.equal(node('settings-social-clear-local-api-key').checked, true);
assert.equal(latestSettings.social.model, 'existing-social');
node('settings-tavily-upstream-base-url').value = 'https://unsaved-tavily.example';
api = async () => ({tavily: {}, social: {model: 'saved-social'}});
await saveSocialSettings(event(node('save-social')));
assert.equal(node('settings-tavily-upstream-base-url').value, 'https://unsaved-tavily.example');
node('settings-social-model').value = 'another-draft';
node('settings-social-upstream-api-key').value = 'another-fixture';
api = async () => ({social: {active_key_scope: 'upstream', upstream_keys: [{id: 'one', index: 1, schedulable: true}]}});
await resumeSocialUpstreamKey('one', node('resume'));
assert.equal(node('settings-social-model').value, 'another-draft');
assert.equal(node('settings-social-upstream-api-key').value, 'another-fixture');
assert.match(node('settings-social-upstream-key-list').innerHTML, /正常/);
flushTimers();
const late = deferred();
api = () => late.promise;
const lateSave = saveTavilySettings(event(node('save-tavily')));
closeSettingsModal();
node('settings-tavily-upstream-base-url').value = 'https://reopened-draft.example';
late.resolve({tavily: {upstream_base_url: 'https://previous-save.example'}});
await lateSave;
assert.equal(node('settings-tavily-upstream-base-url').value, 'https://reopened-draft.example');
""")

    def test_all_mutations_distinguish_write_success_from_refresh_failure(self) -> None:
        names = (
            "createToken", "createMySearchBootstrapToken", "addSingleKey", "importKeys",
            "describeKeyIngestionResult", "delToken", "delKey", "toggleKey", "syncUsage",
            "saveTavilySettings", "saveSocialSettings", "resumeSocialUpstreamKey", "refreshSocialModels",
        )
        self.run_console(names, r"""
const cases = [
  button => createToken('tavily', button),
  button => createMySearchBootstrapToken(button),
  button => addSingleKey('tavily', button),
  button => importKeys('tavily', button),
  button => delToken('tavily', 1, button),
  button => delKey('tavily', 1, button),
  button => toggleKey('tavily', 1, 1, button),
  button => syncUsage('tavily', true, button),
  button => saveTavilySettings(event(button)),
  button => saveSocialSettings(event(button)),
  button => resumeSocialUpstreamKey('one', button),
  button => refreshSocialModels(button),
];
for (const [index, action] of cases.entries()) {
  nodes.clear();
  toasts.length = 0;
  node('single-key-tavily').value = 'key-fixture';
  node('import-text-tavily').value = 'key-fixture';
  let writes = 0, reads = 0;
  api = async () => {
    writes += 1;
    return {ok: true, imported: 1, inserted: 1, tavily: {}, social: {}, probes: [{search_capable: true}]};
  };
  refresh = async () => { reads += 1; throw new Error('stats unavailable'); };
  await action(node(`action-${index}`));
  assert.equal(writes, 1, `case ${index} must write once`);
  assert.equal(reads, 1, `case ${index} must read once`);
  assert.match(node('dashboard-error-message').textContent, /但界面刷新失败.*stats unavailable/);
  assert.ok(toasts.some(toast => toast.type === 'warn'), `case ${index} refresh warning`);
  assert.ok(!toasts.some(toast => toast.type === 'error'), `case ${index} must not report write failure`);
  flushTimers();
  toasts.length = 0;
  node('single-key-tavily').value = 'retry-fixture';
  node('import-text-tavily').value = 'retry-fixture';
  writes = 0; reads = 0;
  api = async () => { writes += 1; throw new Error('write unavailable'); };
  await action(node(`action-${index}`));
  assert.equal(writes, 1);
  assert.equal(reads, 0, `case ${index} failed write must not refresh`);
  const localStatus = ['settings-tavily-status', 'settings-social-status'].map(id => node(id).textContent).join(' ');
  assert.ok(toasts.some(toast => toast.type === 'error') || localStatus.includes('write unavailable'), `case ${index} visible write failure`);
}
""")

    def test_delete_and_toggle_deduplicate_pending_actions_and_release_on_cancel(self) -> None:
        self.run_console(("delToken", "delKey", "toggleKey"), r"""
let writes = 0, confirmations = 0;
const request = deferred();
api = () => { writes += 1; return request.promise; };
const first = toggleKey('tavily', 42, 1, node('row-toggle'));
assert.equal(node('row-toggle').disabled, true);
await toggleKey('tavily', 42, 1, node('drawer-toggle'));
await delKey('tavily', 42, node('drawer-delete'));
assert.equal(writes, 1);
request.resolve({ok: true});
await first;
assert.equal(pendingMutations.size, 0);
const answer = deferred();
showConfirmDialog = () => { confirmations += 1; return answer.promise; };
const removing = delToken('tavily', 7, node('delete-token'));
await delToken('tavily', 7, node('delete-token-again'));
assert.equal(confirmations, 1);
answer.resolve(false);
await removing;
assert.equal(writes, 1);
assert.equal(pendingMutations.size, 0);
showConfirmDialog = async () => true;
api = async () => { writes += 1; throw new Error('delete rejected'); };
await delToken('tavily', 7, node('delete-token'));
assert.equal(writes, 2);
assert.equal(pendingMutations.size, 0);
assert.ok(toasts.some(toast => toast.type === 'error' && toast.message.includes('delete rejected')));
""")


if __name__ == "__main__":
    unittest.main()
