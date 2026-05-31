## Loop 7 Fixes And Validation

- Date: 2026-05-31
- Status: local fixes and regression validation complete; commit/push, CI, deploy, and full benchmark rerun pending.

## Fixes

1. Software-version answer refresh after blended search
   - `mysearch/clients.py` now detects software-version queries, reranks version-reference pages above low-signal community threads, and refreshes the final answer after merged-result reranking.
   - `openclaw/runtime/mysearch/clients.py` is kept in sync with the same logic.

2. Broader `crawl_site` semantics for deep-path crawls
   - `mysearch/server.py` now exposes `crawl_entire_domain`.
   - `mysearch/clients.py` now sends Firecrawl `crawlEntireDomain` explicitly and defaults `crawl_site` to broader internal traversal while preserving an opt-out.

3. Loop 7 regression coverage
   - Added `tests/test_loop7_fixes.py` to lock in:
     - stale software-version answers are corrected from merged results,
     - version-reference pages rerank above community threads,
     - `crawl_site` defaults to `crawlEntireDomain=True`,
     - callers can still opt out with `crawl_entire_domain=False`.

## Validation

- `python3 -m py_compile mysearch/clients.py mysearch/server.py openclaw/runtime/mysearch/clients.py tests/test_loop7_fixes.py`: PASS.
- `pytest tests/test_loop7_fixes.py tests/test_loop4_fixes.py tests/test_loop5_fixes.py`: PASS, 19 tests.
- `pytest tests/test_clients.py`: PASS, 359 tests.
- Real Loop 6 factual baseline replay: the raw `factual-accuracy-01` payload now rewrites `3.13` to `3.14`, and reranking moves the Liquid Web version page above the Reddit thread.

## Pending completion gates

- Commit and push verified Loop 7 changes to `main`.
- Wait for GitHub Actions release workflow.
- Redeploy `mysearch-stack` because runtime image inputs changed.
- Run a fresh full 41-row MCP comparison and record the loop outcome.
