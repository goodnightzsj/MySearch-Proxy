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
        self.assertIn("dashboard.inert = overlayOpen", javascript)
        self.assertIn("shell.setAttribute('aria-hidden'", javascript)
        self.assertIn('role="radiogroup"', settings)
        self.assertNotIn('class="mode-switch" role="tablist"', settings)
        self.assertNotIn('class="mini-switch" role="tablist"', javascript)
        self.assertIn('aria-labelledby="workspace-${service}-tab-overview"', javascript)
        self.assertIn('aria-label="搜索 ${meta.label} Token"', javascript)
        self.assertIn('aria-label="搜索 ${meta.label} API Key"', javascript)
        self.assertIn('class="inline-meta-base-url"', javascript)

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
        self.assertIn('id="settings-social-clear-upstream-api-key"', settings)


if __name__ == "__main__":
    unittest.main()
