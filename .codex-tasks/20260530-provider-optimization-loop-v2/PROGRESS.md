# Progress

## Current State

- Epic restarted on 2026-05-30.
- This epic replaces the old loop only as the active truth source; `.codex-tasks/20260529-provider-optimization-loop/` remains the completed historical reference.
- User requested a stricter per-loop protocol: every loop must analyze whether the benchmark itself should change based on provider capabilities and collaboration design.
- Loop 1 is complete under the final 37-row definition.
- Latest pushed commit after Loop 8: `7ec7a62`.
- Latest runtime deploy used the Loop 8 runtime built from commit `7ec7a62` as remote image tag `helloworldz1024/mysearch-stack:7ec7a62-local`.
- Remote `mysearch-stack` is healthy after the Loop 8 redeploy.
- Loop 2 completed on the same final Loop 1 code state without additional code changes.

## Loop State

- Current loop: 11
- Consecutive loops with no new issues: 0 (Loop 10 found actionable grok2api v3 model and admin-health compatibility issues)
- Stop condition: three consecutive loops with no new Critical, High, Medium, or Low findings after the full benchmark has been rerun under the loop's final benchmark definition.

## Protocol Changes For v2

- Benchmark evolution analysis is now a mandatory step in every loop, not an implicit side effect.
- Benchmark dimension review remains mandatory every loop, but dimension redesign is trigger-based rather than automatic.
- A loop only counts toward the no-new-issues streak after the full benchmark has been rerun under that loop's final benchmark and runner definition.
- Deploy is now an explicit gate: runtime changes require remote redeploy; benchmark-only changes must explicitly record that no redeploy was needed.
- The optimized loop now runs as six gates: Inspect, Benchmark Decision, Implement, Validate, Release Gate, and Compare And Converge.

## Next

- Loop 4 reopened the converged loop under the user goal "make mysearch superior in every dimension" and found three regressions/gaps vs Tavily (factual news-misrouting, extract hCaptcha noise, missing crawl/map). All fixed, pushed (`3ccd805`, `0dee81f`), deployed (image `6d7f566a`), and verified live.
- Loop 5 found three further new/regressed issues by code analysis (crawl `maxDepth` should be Firecrawl v2 `maxDiscoveryDepth`; `_strip_trailing_hcaptcha` over-truncation risk; news over-suppression of entertainment queries). All fixed, pushed (`81d438e`), deployed (image `f341c5ba`, CI run `26691539290`), full 37-row comparison captured 37/37 with 0 structural failures, factual routing re-verified live. Local suite 604/604.
- Loop 6 expanded the formal matrix from 37 to 41 rows with explicit `factual-accuracy`, `extract-cleanliness`, and `crawl-map` rows. It found benchmark runner gaps for map/crawl support and stale/cross-provider structural-failure accounting, a low-risk Exa provider-contract issue (`type="neural"` -> `type="auto"`), and a remaining Firecrawl map/crawl transient-failure hardening gap after deploy validation.
- Loop 6 fixes are complete: benchmark runner now isolates rate-limit-sensitive map/crawl rows into single-run special batches with cooldown, and the runtime now retries one transient `429/502/503/504` on Firecrawl map/crawl requests. Local validation passed with `py_compile`, `pytest tests/test_remote_benchmark_config.py` (29 passed), and `pytest tests/test_clients.py` (359 passed).
- Loop 6 commit `86d5474` was pushed to `main`; GitHub Actions run `26702764424` succeeded.
- Loop 6 runtime was redeployed to `root@192.168.31.122` as image `helloworldz1024/mysearch-stack:86d5474-local`; proxy health and MCP initialize both passed after replacement.
- Loop 6 full fresh post-deploy comparison is complete: `.codex-tasks/20260530-provider-optimization-loop-v2/raw/loop6-remote-compare.csv` captured 41/41 rows with 0 partial-error, 0 structural_failure, 0 timeout, and 0 empty-result. The predeploy artifact was preserved as `.codex-tasks/20260530-provider-optimization-loop-v2/raw/loop6-remote-compare-predeploy.csv`.
- Loop 7 found two further runtime issues on the clean Loop 6 baseline: blended software-version queries could keep a stale primary-provider answer after merged rerank, and `crawl_site` inherited Firecrawl’s narrow deep-path crawl default because MySearch did not expose broader internal traversal. Both were fixed, pushed (`699c8a7`), validated locally (`pytest tests/test_loop7_fixes.py tests/test_loop4_fixes.py tests/test_loop5_fixes.py` and `pytest tests/test_clients.py`), deployed as image `699c8a7-local`, and rerun through the full 41-row matrix.
- Loop 7 full fresh post-deploy comparison is complete: `.codex-tasks/20260530-provider-optimization-loop-v2/raw/loop7-remote-compare.csv` captured 41/41 rows with 0 partial-error, 0 timeout, 0 empty-result, and one non-actionable Tavily comparator structural failure `tavily-research-upstream-plan-limited` on `research-01`.
- Loop 8 found a further runtime issue on that deployed Loop 7 baseline: software-version extraction could treat `future Python 3.16` from `devguide.python.org/versions` as the latest stable version even when `python.org/downloads` still exposed stable `3.14.5`. The runtime fix was committed as `7ec7a62`, GitHub Actions run `26708697235` succeeded, remote `mysearch-stack` was rebuilt as `helloworldz1024/mysearch-stack:7ec7a62-local`, and the fresh full 41-row comparison captured 41/41 with 0 partial-error, 0 timeout, 0 empty-result, and one non-actionable Tavily comparator structural failure `tavily-search-upstream-plan-limited` on `crawl-map-01`.
- Loop 9 re-labeled the same 41-row comparison around capability-chain dimensions (`authority_precision`, `semantic_discovery`, `provider_orchestration`, `multi_source_fusion`, `content_fidelity`, `freshness_signal`, `site_coverage`, `traceability`, `resilience`, `efficiency`) without changing the runner or row set. The full 41-row comparison remains complete with 41/41 captured, 0 partial-error, 0 timeout, 0 empty-result, and one non-actionable Tavily comparator structural failure `tavily-search-upstream-plan-limited` on `crawl-map-01`.
- Loop 10 found that the deployed grok2api v3 no longer exposed the old `grok-4.20-fast` default, while its inference route remained `POST /v1/responses` and authentication changed to explicit `g2a_` client keys. It also found that v2 admin fallback paths could return the v3 SPA with HTTP 200 and be misreported as a connected admin API. The first repaired 41-row comparison then exposed another runtime issue: Python's official `development versions of Python 3.15` wording bypassed the prerelease marker and was promoted above stable `3.14.6`. All three findings now have local fixes; both full-suite runs passed with 623 tests, and the live Social setting completes with one `grok-4.20-0309` call instead of an invalid primary plus fallback.
- Loop 10 release gate is complete: commits `19a566e` and `47b417f` were pushed, Docker workflows `29572859355` and `29574992460` succeeded, and remote `mysearch-stack` now runs `helloworldz1024/mysearch-stack:sha-47b417f` with health and MCP initialize verified.
- Loop 10 final comparison is complete: `raw/loop10-remote-compare-final.csv` captured 41/41 unique rows with zero missing IDs, structural failures, timeouts, empty MySearch results, or row errors. All referenced raw artifacts exist, and `factual-accuracy-01` now answers Python `3.14.6`.
- Loop 11 Inspect found benchmark-integrity gaps: the old output schema did not score the ten active dimensions, preserve per-repeat evidence, enforce latency budgets, keep trace JSON valid, or distinguish normal orchestration from actual fallback. The user also requested a complete console optimization, which exposed an overlay `inert` ancestor bug and incomplete control semantics.
- Loop 11 local fixes are complete. The runner now emits auditable ten-dimension evidence and the console now uses an operations-first single-workspace layout with unified overlay isolation. The full local suite passes with 634 tests, and browser checks pass at 320/375/768/1440 widths.
- Streak remains `0 / 3`; Loop 11 found actionable issues and cannot count as clean. Current work is its release/deploy gate followed by a fresh full 41-row comparison under the changed runner contract.

## Notes

- `.codex-tasks/20260530-provider-optimization-loop-v2/` is the new truth source for this restarted long task.
- `.codex-tasks/20260529-provider-optimization-loop/` already satisfied its own stop condition and should not be mutated further.
- Loop 1 benchmark evolution decision changed the matrix from 36 rows to 37 rows by adding `hybrid-web-x-01`.
- Loop 1 local fixes are recorded in `raw/loop1-code-analysis.md`, `raw/loop1-provider-research.md`, `raw/loop1-benchmark-evolution.md`, `raw/loop1-gap-analysis.md`, and `raw/loop1-fix-and-validation.md`.
- The first full 37-row rerun on commit `10450e7` is recorded in `raw/loop1-benchmark-pass1.md`.
- That rerun found a new comparator resilience gap: Tavily now sometimes returns `session_unavailable`, which the current runner did not yet treat as a recoverable MCP session error.
- The final Loop 1 benchmark summary is recorded in `raw/loop1-benchmark.md`.
- Loop 2 benchmark summary is recorded in `raw/loop2-benchmark.md`.
- Loop 3 benchmark summary is recorded in `raw/loop3-benchmark.md`.
- Loop 4 summary is recorded in `raw/loop4-benchmark.md`.
- Loop 5 analysis/research/decision/summary are recorded in `raw/loop5-code-analysis.md`, `raw/loop5-provider-research.md`, `raw/loop5-benchmark-evolution.md`, and `raw/loop5-benchmark.md`; Loop 5 fixes are covered by `tests/test_loop5_fixes.py`.
- Loop 6 analysis/research/decision/fixes/results are recorded in `raw/loop6-code-analysis.md`, `raw/loop6-provider-research.md`, `raw/loop6-benchmark-evolution.md`, `raw/loop6-fix-and-validation.md`, `raw/loop6-ci.md`, `raw/loop6-deploy.md`, and `raw/loop6-benchmark.md`.
- Loop 7 analysis/research/decision/fixes/results are recorded in `raw/loop7-code-analysis.md`, `raw/loop7-provider-research.md`, `raw/loop7-benchmark-evolution.md`, `raw/loop7-fix-and-validation.md`, `raw/loop7-ci.md`, `raw/loop7-deploy.md`, and `raw/loop7-benchmark.md`; Loop 7 fixes are covered by `tests/test_loop7_fixes.py`.
- Loop 8 analysis/research/decision/fixes/results are recorded in `raw/loop8-code-analysis.md`, `raw/loop8-provider-research.md`, `raw/loop8-benchmark-evolution.md`, `raw/loop8-fix-and-validation.md`, `raw/loop8-ci.md`, `raw/loop8-deploy.md`, and `raw/loop8-benchmark.md`; Loop 8 fixes are covered by `tests/test_loop7_fixes.py`.
- Loop 9 benchmark evolution and comparison are recorded in `raw/loop9-benchmark-evolution.md`, `raw/loop9-benchmark-input.csv`, and `raw/loop9-remote-compare.csv`.
- Loop 10 analysis, provider refresh, benchmark decision, and local validation are recorded in `raw/loop10-code-analysis.md`, `raw/loop10-provider-research.md`, `raw/loop10-benchmark-evolution.md`, and `raw/loop10-fix-and-validation.md`.
- Loop 10 release and final comparison are recorded in `raw/loop10-ci.md`, `raw/loop10-deploy.md`, `raw/loop10-benchmark.md`, `raw/loop10-remote-compare-final.csv`, and `raw/loop10-remote-compare-final-raw/`.
- Loop 11 analysis, provider delta, benchmark decision, and local validation are recorded in `raw/loop11-code-analysis.md`, `raw/loop11-provider-research.md`, `raw/loop11-benchmark-evolution.md`, and `raw/loop11-fix-and-validation.md`.
