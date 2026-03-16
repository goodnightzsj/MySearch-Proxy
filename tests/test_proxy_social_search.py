import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))

import server as proxy_server  # noqa: E402


class ProxySocialSearchTests(unittest.TestCase):
    def test_parse_secret_values_supports_csv_and_list(self) -> None:
        self.assertEqual(
            proxy_server.parse_secret_values(" key-a, key-b ,key-a "),
            ["key-a", "key-b"],
        )
        self.assertEqual(
            proxy_server.parse_secret_values(["key-a", "key-b", "key-a"]),
            ["key-a", "key-b"],
        )

    def test_build_social_token_stats_matches_grok2api_console_formula(self) -> None:
        stats = proxy_server.build_social_token_stats(
            {
                "ssoBasic": [
                    {"token": "tok-a", "status": "active", "quota": 64, "use_count": 100},
                    {"token": "tok-b", "status": "cooling", "quota": 0, "use_count": 12},
                ],
                "ssoSuper": [
                    {
                        "token": "tok-c",
                        "status": "expired",
                        "quota": 0,
                        "use_count": 3,
                        "tags": ["nsfw"],
                    },
                    {"token": "tok-d", "status": "active", "quota": 140, "use_count": 40},
                ],
            }
        )

        self.assertEqual(stats["token_total"], 4)
        self.assertEqual(stats["token_normal"], 2)
        self.assertEqual(stats["token_limited"], 1)
        self.assertEqual(stats["token_invalid"], 1)
        self.assertEqual(stats["chat_remaining"], 204)
        self.assertEqual(stats["image_remaining"], 102)
        self.assertEqual(stats["total_calls"], 155)
        self.assertEqual(stats["nsfw_enabled"], 1)
        self.assertEqual(stats["pool_count"], 2)

    def test_normalize_social_search_response_prefers_json_results(self) -> None:
        payload = {
            "model": "grok-4.1-fast",
            "output_text": (
                '{"answer":"MCP discussions are active.","results":['
                '{"title":"Post A","url":"https://x.com/a/status/1","text":"A text",'
                '"author":"Alice","handle":"alice","created_at":"2026-03-16","why_relevant":"recent"}'
                "]}"
            ),
        }

        response = proxy_server.normalize_social_search_response(
            "MCP on X",
            payload,
            3,
        )

        self.assertEqual(response["answer"], "MCP discussions are active.")
        self.assertEqual(len(response["results"]), 1)
        self.assertEqual(response["results"][0]["url"], "https://x.com/a/status/1")
        self.assertEqual(response["citations"][0]["url"], "https://x.com/a/status/1")

    def test_normalize_social_search_response_falls_back_to_annotations(self) -> None:
        payload = {
            "model": "grok-4.1-fast",
            "output": [
                {
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Plain summary without JSON.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://x.com/b/status/2",
                                    "title": "Post B",
                                }
                            ],
                        }
                    ]
                }
            ],
        }

        response = proxy_server.normalize_social_search_response(
            "MCP on X",
            payload,
            3,
        )

        self.assertEqual(response["answer"], "Plain summary without JSON.")
        self.assertEqual(len(response["results"]), 1)
        self.assertEqual(response["results"][0]["url"], "https://x.com/b/status/2")
        self.assertEqual(response["citations"][0]["title"], "Post B")

    def test_build_social_search_upstream_payload_applies_filters(self) -> None:
        payload = proxy_server.build_social_search_upstream_payload(
            {
                "query": "MCP discussions",
                "max_results": 4,
                "allowed_x_handles": ["openai"],
                "from_date": "2026-03-01",
                "include_x_images": True,
            }
        )

        self.assertEqual(payload["model"], proxy_server.SOCIAL_GATEWAY_MODEL)
        self.assertEqual(payload["tools"][0]["type"], "x_search")
        self.assertEqual(payload["tools"][0]["allowed_x_handles"], ["openai"])
        self.assertEqual(payload["tools"][0]["from_date"], "2026-03-01")
        self.assertTrue(payload["tools"][0]["enable_image_understanding"])
        self.assertIn("Return up to 4 results", payload["input"][0]["content"])


if __name__ == "__main__":
    unittest.main()
