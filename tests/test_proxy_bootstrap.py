from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PROXY_ROOT = REPO_ROOT / "proxy"
MYSEARCH_ROOT = REPO_ROOT / "mysearch"


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class ProxyBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if str(PROXY_ROOT) not in sys.path:
            sys.path.insert(0, str(PROXY_ROOT))
        cls.proxy_server = _load_module(
            "test_proxy_server_bootstrap",
            PROXY_ROOT / "server.py",
        )
        cls.bootstrap_script = _load_module(
            "test_mysearch_bootstrap_proxy_token",
            MYSEARCH_ROOT / "scripts" / "bootstrap_proxy_token.py",
        )

    def test_issue_mysearch_bootstrap_token_reuses_existing_named_token(self) -> None:
        existing = {"token": "mysp-existing", "name": "docker-mysearch", "service": "mysearch"}
        with patch.object(self.proxy_server.db, "get_token_by_name", return_value=existing), patch.object(
            self.proxy_server.db,
            "create_token",
        ) as create_token:
            token, created = self.proxy_server.issue_mysearch_bootstrap_token("docker-mysearch")

        self.assertFalse(created)
        self.assertEqual(token["token"], "mysp-existing")
        create_token.assert_not_called()

    def test_issue_mysearch_bootstrap_token_creates_when_missing(self) -> None:
        created_row = {"token": "mysp-new", "name": "docker-mysearch", "service": "mysearch"}
        with patch.object(self.proxy_server.db, "get_token_by_name", return_value=None), patch.object(
            self.proxy_server.db,
            "create_token",
            return_value=created_row,
        ) as create_token:
            token, created = self.proxy_server.issue_mysearch_bootstrap_token("docker-mysearch")

        self.assertTrue(created)
        self.assertEqual(token["token"], "mysp-new")
        create_token.assert_called_once_with("docker-mysearch", service="mysearch")

    def test_bootstrap_script_fetches_token(self) -> None:
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"token":"mysp-bootstrap"}'

        env = {
            "MYSEARCH_PROXY_BASE_URL": "http://proxy:9874",
            "MYSEARCH_PROXY_BOOTSTRAP_TOKEN": "bootstrap-secret",
            "MYSEARCH_PROXY_BOOTSTRAP_NAME": "docker-mysearch",
            "MYSEARCH_PROXY_BOOTSTRAP_TIMEOUT_SECONDS": "2",
            "MYSEARCH_PROXY_BOOTSTRAP_INTERVAL_SECONDS": "0.01",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(
            self.bootstrap_script.urllib.request,
            "urlopen",
            return_value=_Response(),
        ):
            with patch("sys.stdout.write") as stdout_write:
                exit_code = self.bootstrap_script.main()

        self.assertEqual(exit_code, 0)
        written = "".join(call.args[0] for call in stdout_write.call_args_list)
        self.assertIn("mysp-bootstrap", written)


class ProxySecurityHardeningTests(unittest.TestCase):
    """r6 Critical 安全修复回归测试。"""

    @classmethod
    def setUpClass(cls) -> None:
        if str(PROXY_ROOT) not in sys.path:
            sys.path.insert(0, str(PROXY_ROOT))
        cls.proxy_server = _load_module(
            "test_proxy_server_security",
            PROXY_ROOT / "server.py",
        )

    def test_c6_mask_key_rows_removes_raw_key(self) -> None:
        """C6: /api/keys 返回必须不含明文 key 字段。"""
        rows = [{"id": 1, "key": "sk-1234567890abcdef-tail", "active": 1}]
        masked = self.proxy_server.mask_key_rows([dict(r) for r in rows])
        self.assertEqual(len(masked), 1)
        self.assertNotIn("key", masked[0], "raw key 必须被删除，仅保留 key_masked")
        self.assertIn("key_masked", masked[0])
        self.assertEqual(masked[0]["key_masked"], "sk-12345***tail")

    def test_c6_mask_key_rows_empty_or_short(self) -> None:
        """C6 边界：极短 key 不挂掉，且仍删原文。"""
        masked = self.proxy_server.mask_key_rows([{"id": 2, "key": "short"}])
        self.assertNotIn("key", masked[0])
        # 极短 key 也脱敏：sh***rt
        self.assertEqual(masked[0]["key_masked"], "sh***rt")

    def test_c4_verify_admin_uses_hmac_compare_digest(self) -> None:
        """C4: verify_admin 不能用 `==` 比较密码（侧信道风险）。"""
        import inspect
        source = inspect.getsource(self.proxy_server.verify_admin)
        # 关键断言：源码不再出现 == pwd 这种比较
        self.assertNotIn("password == pwd", source)
        self.assertNotIn('auth == f"Bearer {pwd}"', source)
        self.assertIn("compare_digest", source)


if __name__ == "__main__":
    unittest.main()
