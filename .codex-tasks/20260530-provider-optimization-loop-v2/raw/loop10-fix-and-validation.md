## Loop 10 Fixes And Validation

- Date: 2026-07-17
- Status: local fixes and validation complete; release and full comparison pending.

## Fixes

- Updated built-in Grok defaults to `grok-4.20-0309`, `grok-4.3`, and `grok-4.5`; Social primary/fallback defaults are now `grok-4.20-0309` and `grok-4.3`.
- Synchronized main MySearch runtime, OpenClaw bundled runtime, Proxy defaults, examples, tests, and documentation.
- Changed v2 admin readers to fail explicitly when a successful HTTP response is not a JSON object, preventing grok2api v3 SPA HTML from being reported as a connected admin API.
- Made the standalone Social gateway lock lazy and event-loop scoped so import does not require a current loop under Python 3.9.
- Removed a test-only `sys.modules` reset that corrupted later package-level mocks.

## Validation

- `python3 -m py_compile ...`: pass for all touched Python runtime and test files.
- `pytest -q tests/test_social_normalization.py tests/test_config_bootstrap.py tests/test_clients.py`: 385 passed.
- `pytest -q`: 623 passed.
- `git diff --check`: pass.
- Live grok2api probe: `grok-4.20-0309` returned HTTP 200 and X citations through `/v1/responses`.
- Live grok2api probe: `grok-4.3` returned HTTP 200 through `/v1/responses`.
- Live MySearch Social probe after the runtime setting update: one Social call, selected model `grok-4.20-0309`, two X results.

## Pending Gates

- Commit and push the verified changes.
- Wait for the Docker workflow.
- Build/redeploy `mysearch-stack` from the committed tree.
- Run a fresh full 41-row comparison and record Loop 10 convergence.
