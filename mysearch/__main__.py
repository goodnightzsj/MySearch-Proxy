from __future__ import annotations

import argparse

from mysearch.server import main


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the MySearch MCP server with stdio, SSE, or streamableHTTP transport.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default="stdio",
        help="MCP transport to use. Defaults to stdio for Codex / Claude Code.",
    )
    parser.add_argument(
        "--host",
        help="Bind host for HTTP-based transports. Defaults to MYSEARCH_MCP_HOST or 127.0.0.1.",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Bind port for HTTP-based transports. Defaults to MYSEARCH_MCP_PORT or 8000.",
    )
    parser.add_argument(
        "--mount-path",
        help="Mount path for SSE mode. Defaults to MYSEARCH_MCP_MOUNT_PATH or '/'.",
    )
    parser.add_argument(
        "--sse-path",
        help="SSE endpoint path. Defaults to MYSEARCH_MCP_SSE_PATH or '/sse'.",
    )
    parser.add_argument(
        "--streamable-http-path",
        help="StreamableHTTP endpoint path. Defaults to MYSEARCH_MCP_STREAMABLE_HTTP_PATH or '/mcp'.",
    )
    parser.add_argument(
        "--stateless-http",
        action="store_true",
        help="Enable stateless StreamableHTTP sessions.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        main(
            transport=args.transport,
            host=args.host,
            port=args.port,
            mount_path=args.mount_path,
            sse_path=args.sse_path,
            streamable_http_path=args.streamable_http_path,
            stateless_http=args.stateless_http if args.stateless_http else None,
        )
    except KeyboardInterrupt:
        pass
