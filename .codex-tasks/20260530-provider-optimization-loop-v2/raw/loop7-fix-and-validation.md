## Loop 7 Fixes And Validation

- Date: 2026-05-31
- Status: completed, pushed, deployed, and benchmarked.

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

## Completion

- Commit `699c8a7` (`Fix version answers and crawl breadth defaults`) was pushed to `main`.
- GitHub Actions run `26708010346` for that commit completed with `success`.
- Remote `mysearch-stack` was rebuilt from the Loop 7 tree as image `helloworldz1024/mysearch-stack:699c8a7-local` and redeployed on `root@192.168.31.122`.
- Post-deploy checks passed: `http://192.168.31.122:9874/health` returned success and MCP `initialize` on `http://192.168.31.122:18000/mcp` returned an `mcp-session-id`.
- Fresh full post-deploy 41-row comparison is recorded in `.codex-tasks/20260530-provider-optimization-loop-v2/raw/loop7-remote-compare.csv` with 41/41 captured, 0 timeout, 0 empty-result, and a single non-actionable comparator structural failure `tavily-research-upstream-plan-limited` on `research-01`.

## Loop Outcome

- Loop 7 is complete but not clean. The benchmark confirms both runtime fixes worked (`factual-accuracy-01` now lands on official Python version pages and `crawl-map-02` captures 5 pages), but the loop itself still found actionable issues, so the no-new-issue streak remains `0 / 3`.
