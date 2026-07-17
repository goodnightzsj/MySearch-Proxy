## Loop 10 Fixes And Validation

- Date: 2026-07-17
- Status: complete; the follow-up fix was released, deployed, and verified by the final 41-row comparison.

## Fixes

- Updated built-in Grok defaults to `grok-4.20-0309`, `grok-4.3`, and `grok-4.5`; Social primary/fallback defaults are now `grok-4.20-0309` and `grok-4.3`.
- Synchronized main MySearch runtime, OpenClaw bundled runtime, Proxy defaults, examples, tests, and documentation.
- Changed v2 admin readers to fail explicitly when a successful HTTP response is not a JSON object, preventing grok2api v3 SPA HTML from being reported as a connected admin API.
- Made the standalone Social gateway lock lazy and event-loop scoped so import does not require a current loop under Python 3.9.
- Removed a test-only `sys.modules` reset that corrupted later package-level mocks.
- Added `development version` to software-version prerelease markers in both runtime copies. This matches the current Python downloads wording without widening the surrounding context window.

## Validation

- `python3 -m py_compile ...`: pass for all touched Python runtime and test files.
- `pytest -q tests/test_social_normalization.py tests/test_config_bootstrap.py tests/test_clients.py`: 385 passed.
- `pytest -q`: 623 passed.
- `git diff --check`: pass.
- Live grok2api probe: `grok-4.20-0309` returned HTTP 200 and X citations through `/v1/responses`.
- Live grok2api probe: `grok-4.3` returned HTTP 200 through `/v1/responses`.
- Live MySearch Social probe after the runtime setting update: one Social call, selected model `grok-4.20-0309`, two X results.
- First post-deploy comparison: 41/41 captured, zero structural failures, zero timeouts, and zero empty results; it exposed the incorrect `Python 3.15` stable-version answer.
- Current Python downloads raw-result replay after the follow-up fix: `The latest stable version of Python is 3.14.6.`
- Follow-up `pytest -q tests/test_loop7_fixes.py tests/test_clients.py`: 364 passed.
- Follow-up `pytest -q`: 623 passed.

## Completed Gates

- Follow-up commit `47b417f` was pushed to `main`.
- Docker workflow `29574992460` completed successfully.
- Remote `mysearch-stack` was replaced with `helloworldz1024/mysearch-stack:sha-47b417f`; proxy health and MCP initialize passed.
- Final comparison `loop10-remote-compare-final.csv` captured 41/41 rows with no structural failures, timeouts, empty MySearch results, or errors.
