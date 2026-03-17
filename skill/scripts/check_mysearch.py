#!/usr/bin/env python3
"""MySearch 本地健康检查与烟测脚本。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mysearch.clients import MySearchClient  # noqa: E402


def print_json(title: str, payload: dict) -> None:
    print(f"=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Check MySearch MCP health and smoke-test routes.")
    parser.add_argument("--health-only", action="store_true", help="Only print provider health.")
    parser.add_argument("--web-query", default="", help="Run a normal web search smoke test.")
    parser.add_argument("--docs-query", default="", help="Run a docs-focused search smoke test.")
    parser.add_argument("--social-query", default="", help="Run a social/X search smoke test.")
    parser.add_argument("--extract-url", default="", help="Run a single extract_url smoke test.")
    args = parser.parse_args()

    client = MySearchClient()
    print_json("health", client.health())

    if args.health_only:
        return 0

    if args.web_query:
        result = client.search(query=args.web_query, mode="web", max_results=3)
        print_json("web_search", result)

    if args.docs_query:
        result = client.search(
            query=args.docs_query,
            mode="docs",
            max_results=3,
            include_content=True,
        )
        print_json("docs_search", result)

    if args.social_query:
        result = client.search(query=args.social_query, mode="social", max_results=3)
        print_json("social_search", result)

    if args.extract_url:
        result = client.extract_url(url=args.extract_url, formats=["markdown"])
        print_json("extract_url", result)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
