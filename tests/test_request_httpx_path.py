"""覆盖 _request_json / _request_text 的 httpx 分支。

背景：clients.py 里有一个 `prefer_urlopen = "unittest.mock" in type(urlopen).__module__`
分支，当 urlopen 被 mock 时走 urllib，否则走 httpx。绝大多数测试通过
patch("mysearch.clients.urlopen") 走 urllib 分支，导致**生产实际使用的 httpx
分支长期没有测试覆盖**。

这里用一个本地 HTTP server 提供真实响应，不 mock 任何网络函数，因此走的就是
生产的 httpx 路径。
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mysearch.clients import MySearchClient, MySearchError, MySearchHTTPError  # noqa: E402
from mysearch.config import MySearchConfig, ProviderConfig  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    """按路径给出确定响应，用于驱动真实的 httpx 调用链。"""

    def log_message(self, *args):  # 静默，避免污染测试输出
        pass

    def _respond(self, status: int, payload: dict | None, raw: str | None = None, headers=None):
        body = raw.encode("utf-8") if raw is not None else json.dumps(payload or {}).encode("utf-8")
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/ok":
            self._respond(200, {"ok": True, "echo_auth": self.headers.get("Authorization", "")})
        elif self.path == "/retry":
            self._respond(429, {"error": "slow down"}, headers={"Retry-After": "7"})
        elif self.path == "/bad-json":
            self._respond(200, None, raw="not json at all")
        elif self.path == "/text":
            self._respond(200, None, raw="<html>hello</html>")
        else:
            self._respond(404, {"error": "missing"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/echo":
            self._respond(200, {"received": body, "echo_auth": self.headers.get("Authorization", "")})
        else:
            self._respond(404, {"error": "missing"})


class HttpxRequestBodyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls._thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()
        cls.base_url = f"http://127.0.0.1:{cls._server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server.shutdown()
        cls._server.server_close()
        cls._thread.join(timeout=5)

    def setUp(self) -> None:
        self.client = MySearchClient(MySearchConfig.from_env())
        self.provider = ProviderConfig(
            name="tavily",
            base_url=self.base_url,
            auth_mode="bearer",
            auth_header="Authorization",
            auth_scheme="Bearer",
            auth_field="api_key",
            default_paths={},
        )

    def tearDown(self) -> None:
        self.client.close()

    def test_bearer_auth_is_sent_and_json_is_parsed(self) -> None:
        # 不 mock urlopen，因此这里走的是生产的 httpx 分支。
        data = self.client._request_json(
            provider=self.provider, method="GET", path="/ok", payload=None, key="secret-key"
        )
        self.assertTrue(data["ok"])
        self.assertEqual(data["echo_auth"], "Bearer secret-key")

    def test_body_auth_mode_injects_key_into_payload(self) -> None:
        provider = ProviderConfig(
            name="tavily",
            base_url=self.base_url,
            auth_mode="body",
            auth_header="Authorization",
            auth_scheme="",
            auth_field="api_key",
            default_paths={},
        )
        data = self.client._request_json(
            provider=provider, method="POST", path="/echo", payload={"q": "x"}, key="body-key"
        )
        self.assertEqual(data["received"]["api_key"], "body-key")
        self.assertEqual(data["received"]["q"], "x")

    def test_http_error_carries_status_and_retry_after(self) -> None:
        with self.assertRaises(MySearchHTTPError) as ctx:
            self.client._request_json(
                provider=self.provider, method="GET", path="/retry", payload=None, key="k"
            )
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.retry_after_seconds, 7)

    def test_non_json_success_body_raises(self) -> None:
        # 2xx 但正文不是 JSON -> MySearchError；只有 >=400 才包装成 MySearchHTTPError。
        with self.assertRaises(MySearchError) as ctx:
            self.client._request_json(
                provider=self.provider, method="GET", path="/bad-json", payload=None, key="k"
            )
        self.assertIsInstance(ctx.exception, MySearchError)

    def test_request_text_returns_raw_body(self) -> None:
        status_code, text = self.client._request_text(url=f"{self.base_url}/text")
        self.assertEqual(status_code, 200)
        self.assertIn("hello", text)


if __name__ == "__main__":
    unittest.main()
