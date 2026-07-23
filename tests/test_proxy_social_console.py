from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class ProxySocialConsoleTests(unittest.TestCase):
    def test_social_console_uses_grok2api_v3_defaults(self) -> None:
        javascript = (REPO_ROOT / "proxy/static/js/console.js").read_text(encoding="utf-8")
        settings = (REPO_ROOT / "proxy/templates/components/_settings_modal.html").read_text(
            encoding="utf-8"
        )
        console = (REPO_ROOT / "proxy/templates/console.html").read_text(encoding="utf-8")
        rendered = "\n".join((javascript, settings, console))

        self.assertNotIn("grok-4.20-fast", rendered)
        self.assertNotIn("grok-4.20-0309-non-reasoning", rendered)
        self.assertNotIn("grok-4.3-beta", rendered)
        self.assertIn("SOCIAL_GATEWAY_UPSTREAM_API_KEY=YOUR_GROK2API_G2A_CLIENT_KEY", javascript)
        self.assertIn("SOCIAL_GATEWAY_ADMIN_USERNAME=${adminUsername}", javascript)
        self.assertIn("SOCIAL_GATEWAY_ADMIN_PASSWORD=YOUR_GROK2API_ADMIN_PASSWORD", javascript)
        self.assertNotIn("\nSOCIAL_GATEWAY_ADMIN_APP_KEY=YOUR_GROK2API_APP_KEY", javascript)
        self.assertIn("# SOCIAL_GATEWAY_ADMIN_APP_KEY=YOUR_GROK2API_V2_APP_KEY", javascript)
        self.assertIn('id="settings-social-admin-username"', settings)
        self.assertIn('id="settings-social-admin-password"', settings)
        self.assertIn('id="settings-social-mode"', settings)
        self.assertIn('data-social-mode="local"', settings)
        self.assertIn('data-social-mode="upstream"', settings)

    def test_console_uses_operations_first_workspace_shell(self) -> None:
        javascript = (REPO_ROOT / "proxy/static/js/console.js").read_text(encoding="utf-8")
        console = (REPO_ROOT / "proxy/templates/console.html").read_text(encoding="utf-8")
        hero = (REPO_ROOT / "proxy/templates/components/_hero.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('class="ops-bar card"', hero)
        self.assertIn('id="hero-focus"', hero)
        self.assertIn('class="service-switcher"', console)
        self.assertIn('id="services-root" class="services-root"', console)
        self.assertNotIn("真正可交付的基础设施", hero)
        self.assertIn('class="workspace-tabs"', javascript)
        self.assertIn('role="tabpanel"', javascript)
        self.assertIn("panel.classList.toggle('is-inactive'", javascript)

    def test_console_accessibility_contracts_cover_overlays_and_dynamic_controls(self) -> None:
        javascript = (REPO_ROOT / "proxy/static/js/console.js").read_text(encoding="utf-8")
        settings = (REPO_ROOT / "proxy/templates/components/_settings_modal.html").read_text(
            encoding="utf-8"
        )
        console = (REPO_ROOT / "proxy/templates/console.html").read_text(encoding="utf-8")
        mysearch = (REPO_ROOT / "proxy/templates/mysearch.html").read_text(encoding="utf-8")

        self.assertIn('class="skip-link"', console)
        self.assertIn('class="skip-link"', mysearch)
        self.assertIn('id="dashboard-error"', console)
        self.assertIn('id="dashboard-error"', mysearch)
        self.assertIn('onclick="retryConsoleRefresh()"', console)
        self.assertIn('function setDashboardError(message = \'\')', javascript)
        self.assertIn('await refresh({ force: true });', javascript)
        login_block = javascript[
            javascript.index("async function doLogin(event)"):
            javascript.index("function logout()")
        ]
        self.assertIn("setDashboardError(`控制台加载失败：${error.message}`);", login_block)
        status_block = javascript[
            javascript.index("function setStatus(id, message, isError = false)"):
            javascript.index("function describeConfiguredSecret")
        ]
        self.assertLess(
            status_block.index("el.setAttribute('role', role);"),
            status_block.index("el.textContent = message;"),
        )
        self.assertIn('role="status" aria-live="polite" aria-atomic="true"', settings)
        self.assertIn("dashboard.inert = overlayOpen", javascript)
        self.assertIn("shell.setAttribute('aria-hidden'", javascript)
        self.assertIn('role="radiogroup"', settings)
        self.assertNotIn('class="mode-switch" role="tablist"', settings)
        self.assertNotIn('class="mini-switch" role="tablist"', javascript)
        self.assertIn('aria-labelledby="workspace-${service}-tab-overview"', javascript)
        self.assertIn('aria-label="搜索 ${meta.label} Token"', javascript)
        self.assertIn('aria-label="搜索 ${meta.label} API Key"', javascript)
        self.assertIn('class="inline-meta-base-url"', javascript)
        self.assertIn("const preferCancelFocus = tone === 'danger' && Boolean(cancelText);", javascript)
        self.assertIn("preferCancelFocus ? ' data-overlay-autofocus=\"true\"' : ''", javascript)

    def test_console_secondary_text_and_mode_targets_are_readable(self) -> None:
        stylesheet = (REPO_ROOT / "proxy/static/css/console.css").read_text(encoding="utf-8")

        self.assertIn("--muted: #606c7a;", stylesheet)
        self.assertIn("--warn: #9a5708;", stylesheet)
        self.assertIn(".dashboard-error:not(.hidden)", stylesheet)
        self.assertIn(".mode-switch-btn {", stylesheet)
        self.assertIn("min-height: 44px;", stylesheet)
        self.assertNotIn("font-size: 8px;", stylesheet)

    def test_console_localizes_key_disable_details_and_dates(self) -> None:
        javascript = (REPO_ROOT / "proxy/static/js/console.js").read_text(encoding="utf-8")

        self.assertIn("legacy_failure_threshold: '历史失败停用'", javascript)
        self.assertIn("function formatDisabledDetail(key)", javascript)
        self.assertIn("旧版连续失败阈值曾被触发", javascript)
        self.assertIn("date.toLocaleString('zh-CN', { hour12: false })", javascript)
        self.assertIn("const disabledDetail = formatDisabledDetail(key);", javascript)
        self.assertNotIn("escapeHtml(key.disabled_detail)", javascript)

    def test_console_exposes_social_key_recovery_and_filters_only_schedulable_keys(self) -> None:
        javascript = (REPO_ROOT / "proxy/static/js/console.js").read_text(encoding="utf-8")
        settings = (REPO_ROOT / "proxy/templates/components/_settings_modal.html").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "items = items.filter((key) => getKeyAvailability(key).schedulable);",
            javascript,
        )
        self.assertIn("/api/settings/social/keys/${encodeURIComponent(keyId)}/resume", javascript)
        self.assertIn('id="settings-social-upstream-key-list"', settings)
        self.assertIn('id="settings-social-local-key-list"', settings)
        self.assertIn('id="settings-social-local-api-key"', settings)
        self.assertIn('id="settings-social-clear-upstream-api-key"', settings)
        self.assertIn('id="settings-social-clear-local-api-key"', settings)
        self.assertIn("function setSocialMode(mode)", javascript)
        self.assertIn("Social/X Key 池不会参与请求", javascript)

    def test_console_formats_social_admin_version_once(self) -> None:
        javascript = (REPO_ROOT / "proxy/static/js/console.js").read_text(encoding="utf-8")

        self.assertIn("function socialAdminVersionLabel(version)", javascript)
        self.assertIn(".replace(/^v/i, '')", javascript)
        self.assertNotIn("v${social.admin_api_version", javascript)

    def test_console_masks_credentials_until_an_explicit_copy_action(self) -> None:
        javascript = (REPO_ROOT / "proxy/static/js/console.js").read_text(encoding="utf-8")

        self.assertIn("{ includeSecret = false }", javascript)
        self.assertIn("buildCurlExample(service, 'YOUR_PROXY_TOKEN')", javascript)
        self.assertIn("copyMySearchEnv(this)", javascript)
        self.assertIn("copyTokenById('${service}', ${token.id}, this)", javascript)
        self.assertIn("{ includeSecret: true }", javascript)
        self.assertNotIn("copyText(${JSON.stringify(token.token)}, this)", javascript)
        self.assertNotIn(
            'drawerSection(\'完整 Token\', `<pre class="code-block mono">${escapeHtml(token.token)}</pre>`)',
            javascript,
        )
        self.assertIn("maskToken(token.token)", javascript)

    def test_settings_modal_uses_an_internal_scroll_region_without_sticky_footer(self) -> None:
        stylesheet = (REPO_ROOT / "proxy/static/css/console.css").read_text(encoding="utf-8")
        settings = (REPO_ROOT / "proxy/templates/components/_settings_modal.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("grid-template-rows: auto auto minmax(0, 1fr);", stylesheet)
        self.assertIn("overscroll-behavior: contain;", stylesheet)
        self.assertNotIn("position: sticky;\n  bottom: -22px;", stylesheet)
        self.assertNotIn("settings-head-meta", settings)

    def test_social_settings_sidebar_wraps_and_mobile_prioritizes_controls(self) -> None:
        stylesheet = (REPO_ROOT / "proxy/static/css/console.css").read_text(encoding="utf-8")

        self.assertIn(".settings-secret-meta {", stylesheet)
        self.assertIn("overflow-wrap: anywhere;", stylesheet)
        self.assertIn("#settings-panel-social .settings-panel-main {\n    order: -1;", stylesheet)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr));", stylesheet)
        self.assertNotIn("grid-auto-columns: minmax(210px, 78vw);", stylesheet)

    def test_key_tables_paginate_and_mobile_tables_keep_field_labels(self) -> None:
        javascript = (REPO_ROOT / "proxy/static/js/console.js").read_text(encoding="utf-8")
        stylesheet = (REPO_ROOT / "proxy/static/css/console.css").read_text(encoding="utf-8")

        self.assertIn("const KEY_PAGE_SIZE = 20;", javascript)
        self.assertIn("const MOBILE_KEY_PAGE_SIZE = 5;", javascript)
        self.assertIn('id="key-pagination-${service}"', javascript)
        self.assertIn("filtered.slice(pageStart, pageStart + pageSize)", javascript)
        self.assertIn('data-label="同步 / 状态"', javascript)
        self.assertIn("key.key_masked || maskToken(key.key)", javascript)
        self.assertIn(".table-pagination-actions", stylesheet)

    def test_mysearch_quickstart_uses_task_tabs_and_mobile_touch_targets(self) -> None:
        javascript = (REPO_ROOT / "proxy/static/js/console.js").read_text(encoding="utf-8")
        stylesheet = (REPO_ROOT / "proxy/static/css/console.css").read_text(encoding="utf-8")

        self.assertIn('data-quickstart-tab="config"', javascript)
        self.assertIn('data-quickstart-tab="install"', javascript)
        self.assertIn('data-quickstart-tab="tokens"', javascript)
        self.assertIn("function setQuickstartTab(tabName, focus = true)", javascript)
        self.assertIn(".access-shell-actions .user-btn", stylesheet)
        self.assertIn("min-height: 44px;", stylesheet)


if __name__ == "__main__":
    unittest.main()
