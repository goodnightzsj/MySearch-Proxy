## Loop 7 Code Analysis

- Date: 2026-05-31
- HEAD under analysis: deployed Loop 6 baseline `86d5474` plus local Loop 7 edits
- Method: compare the fresh 41-row Loop 6 baseline against provider capabilities and runtime post-processing paths, with extra focus on factual-version answers and the new `crawl_site` benchmark row.

## New / regressed actionable findings

1. Factual version answers could stay stale after blended merge and rerank (runtime, Medium)
   - `factual-accuracy-01` in `.codex-tasks/20260530-provider-optimization-loop-v2/raw/loop6-remote-compare.csv` answered “The latest stable version of Python is 3.13...” even though the same raw result set also contained newer `3.14` evidence and a version-reference page.
   - Root cause: `_search_web_blended()` kept the primary provider answer string, and `_postprocess_search()` never rebuilt version answers after merged results were reranked.
   - Fix: add a software-version query detector, version-reference-aware web reranking, and a post-rerank answer override that extracts the strongest stable version from the merged result set. Sync the same fix into `openclaw/runtime/mysearch/clients.py`.

2. `crawl_site` breadth was too narrow for deep-path site crawls (runtime, Medium)
   - `crawl-map-02` on the Loop 6 clean baseline captured `Crawled pages: 1` for `https://fastapi.tiangolo.com/tutorial/background-tasks/`, while Tavily `tavily_crawl` returned 5 pages under the same `max_depth=1` benchmark row.
   - Firecrawl’s current `/v2/crawl` docs say `crawlEntireDomain=false` only follows deeper child paths; it will not walk sibling or parent URLs from a deep starting page.
   - Root cause: MySearch exposed only `max_depth` and silently inherited Firecrawl’s narrow breadth default, which is weaker than the user-facing “crawl site” abstraction and weaker than the benchmark comparator behavior.
   - Fix: add `crawl_entire_domain` to `crawl_site`, default it to `True` in the MySearch MCP surface, and send Firecrawl `crawlEntireDomain` explicitly.

## No-new-issue areas checked

- Loop 6 benchmark-runner hardening for `map_site` / `crawl_site` still passed local regression tests.
- Firecrawl transient retry hardening from Loop 6 still passed existing `tests/test_clients.py`.
- No new benchmark-definition or comparator-accounting issue appeared in the clean Loop 6 baseline; the new findings are runtime semantics only.

## Conclusion

Loop 7 is not a clean loop: it found two new runtime issues on the deployed 41-row baseline. Local fixes and regression tests are complete, but the loop cannot count toward convergence until commit/push, CI, remote redeploy, and a fresh full 41-row comparison are completed.
