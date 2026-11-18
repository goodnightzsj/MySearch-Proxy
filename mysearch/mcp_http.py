"""Content encoding for finite JSON responses on the MCP HTTP transport."""

import gzip
import re
from functools import partial

import anyio
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


_CODING = re.compile(r"(gzip|identity|\*)(?:\s*;\s*q\s*=\s*(0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?))?",
                     re.I | re.ASCII)
_MIN_BYTES = 4096


def _prefers_gzip(headers: Headers) -> bool:
    weights = {}
    for header in headers.getlist("accept-encoding"):
        for item in header.split(","):
            match = _CODING.fullmatch(item.strip())
            if match:
                coding, weight = match.groups()
                weights[coding.lower()] = float(weight or 1)
    gzip_weight = weights.get("gzip", weights.get("*", 0))
    return gzip_weight > 0 and gzip_weight >= weights.get("identity", 0)


def gzip_json_responses(app: ASGIApp) -> ASGIApp:
    async def handle(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            return await app(scope, receive, send)
        start: Message | None = None

        async def send_json(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start" and message["status"] == 200:
                headers = MutableHeaders(scope=message)
                if (headers.get("content-type", "").partition(";")[0] == "application/json"
                        and int(headers.get("content-length", 0)) >= _MIN_BYTES and "content-encoding" not in headers):
                    headers.add_vary_header("Accept-Encoding")
                    if _prefers_gzip(Headers(scope=scope)):
                        start = message
                        return
            elif start is not None:
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    compress = partial(gzip.compress, message["body"], compresslevel=1, mtime=0)
                    compressed = await anyio.to_thread.run_sync(compress)
                    headers = MutableHeaders(scope=start)
                    headers["content-encoding"] = "gzip"
                    headers["content-length"] = str(len(compressed))
                    message = {**message, "body": compressed}
                await send(start)
                start = None
            await send(message)

        await app(scope, receive, send_json)

    return handle
