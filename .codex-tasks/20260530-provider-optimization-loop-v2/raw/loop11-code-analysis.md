## Loop 11 Code Analysis

- Date: 2026-07-17
- Surface: benchmark runner, proxy console runtime, accessibility, docs, tests
- Baseline: deployed MySearch image `sha-47b417f`; active 41-row capability-chain matrix

## Findings

1. The benchmark CSV still used the pre-Loop-9 score schema. All ten active capability-chain dimensions lacked score columns, successful dual captures remained `pending-review`, and the file could not support the stated convergence decision.
2. Repeat runs only retained the first response. `repeat_variance` measured latency spread without preserving per-run summaries or URLs, so result stability and cold/warm behavior were not auditable.
3. `latency_budget_ms` was present in the matrix but not enforced. Rows could exceed their budget and still remain `captured` with an unrestricted efficiency result.
4. `provider_trace` was truncated after JSON serialization and could become invalid JSON. The old `fallback_used` heuristic also mixed normal multi-provider orchestration, retry hints, and actual fallback execution.
5. The proxy console prioritized a marketing-style hero and repeated provider explanation above live status and the active workspace, increasing page length and operational scanning cost.
6. The settings modal lived inside `#dashboard` while its open handler marked `#dashboard` inert. The modal therefore inherited inert from its own ancestor. Detail and confirmation overlays did not share a single background-isolation contract.
7. Workspace tabs, Tavily mode controls, dynamic token/key inputs, login errors, and mobile controls had incomplete ARIA or accessible-name contracts.
8. The remote benchmark runner embedded the Tavily bearer in a base64 payload passed as a `python3` argv value. Encoding did not protect the credential from local or remote process-list inspection.

## Severity And Outcome

- Findings 1-4 are actionable benchmark-integrity issues. Loop 11 cannot reuse the Loop 10 comparison after the runner contract changes.
- Findings 5-7 are actionable console runtime and accessibility issues requested by the user for the same release.
- Finding 8 is an actionable credential-handling issue in the benchmark transport and invalidated the first post-deploy comparison attempt.
- Loop 11 is not a clean loop even after these fixes pass validation.
