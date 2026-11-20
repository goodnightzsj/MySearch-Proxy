"""Compare Tavily depths on paired JSON rows from stdin; routing stays unchanged."""
from __future__ import annotations

import json
import sys
import time

from mysearch.clients import MySearchClient
from mysearch.config import MySearchConfig
from mysearch.errors import MySearchError, _redact_provider_secret


def run_experiment(client, rows):
    observations = []
    request = client._request_json
    try:
        for index, row in enumerate(rows):
            for repeat in range(3):
                # Counterbalance order without giving either arm different questions.
                depths = ("fast", "ultra-fast") if (index + repeat) % 2 == 0 else ("ultra-fast", "fast")
                for depth in depths:
                    def send(*, payload, **kwargs):
                        return request(payload={**payload, "search_depth": depth}, **kwargs)
                    client._request_json = send
                    started = time.perf_counter()
                    record = {"id": row["id"], "query": row["query"], "repeat": repeat + 1, "depth": depth}
                    try:
                        result = client._search_tavily_once(
                            query=row["query"], max_results=5, topic="general",
                            include_answer=False, include_content=False,
                            include_domains=None, exclude_domains=None, strategy="fast",
                        )
                        record.update(result=result, status="success")
                    except MySearchError as exc:
                        record.update(error=str(exc), status="error")
                    record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
                    observations.append(record)
    finally:
        client._request_json = request
    return observations


if __name__ == "__main__":
    rows = json.load(sys.stdin)
    client = MySearchClient(MySearchConfig.from_env())
    try:
        output = json.dumps(run_experiment(client, rows), ensure_ascii=False)
        # Normal application authentication stays opaque, including any reflected token.
        print(_redact_provider_secret(output, client.config.proxy_api_key))
    finally:
        client.close()
