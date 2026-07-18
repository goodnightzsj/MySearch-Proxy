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
9. The first complete postdeploy result still used an intermediate 81-column schema and exposed quality gaps: exact OpenAI/Next.js docs were not consistently ranked first, arXiv merge could keep a generic subject title, and requested content could remain empty without an explicit enrichment failure contract.
10. A second hCaptcha cleanup pass was required after language-block removal because removing the language tail could reveal an earlier Filters/Ask AI widget as the new trailing block.
11. Hybrid timeout handling did not cover the xAI unified branch or the full Tavily/Exa social fallback deadline. A proposed pure-Social 20-second cap would also have contradicted the existing configurable 120-second contract, so the cap is restricted to Hybrid while the Social benchmark cold-start budget is 30 seconds.
12. The benchmark inferred no semantic correctness, allowing Tavily's stale Python `3.14.3` answer to beat MySearch's verified `3.14.6`. The follow-up contract adds an explicit expected-answer field, but only uses boundary- and negation-safe matching for freshness; it does not manufacture authority from answer text.
13. Partial reruns could retain a stale expected-answer value in output rows while rescoring with the current input matrix. Input-owned contract fields now synchronize before every score pass.
14. The first complete 84-column run on `30153c1` captured 41/41 rows without structural failures, but six MySearch rows exceeded their latency budgets. Strict domain-filtered docs still waited on heavy sequential content enrichment, and changelog enrichment tried Firecrawl before the lower-latency Exa fallback.
15. The same run showed `pdf-02` returning no verifier content, `longtail-academic-01` retaining an embedded Cloudflare challenge block, `docs-01` preferring the broad Playwright `TestStep` class over the exact `test.step` anchor, and `news-01` attempting provider extraction before direct official award HTML.

## Severity And Outcome

- Findings 1-4 are actionable benchmark-integrity issues. Loop 11 cannot reuse the Loop 10 comparison after the runner contract changes.
- Findings 5-7 are actionable console runtime and accessibility issues requested by the user for the same release.
- Finding 8 is an actionable credential-handling issue in the benchmark transport and invalidated the first post-deploy comparison attempt.
- Findings 9-13 are actionable runtime and benchmark-integrity issues discovered by the complete postdeploy pass and release review. The 73-column `final` and 81-column `postdeploy` CSVs are historical intermediates, not the final Loop 11 result.
- Findings 14-15 are actionable runtime issues discovered by the first structurally valid 84-column comparison. That `30153c1` artifact is complete evidence, but it cannot close Loop 11 because its findings required another runtime release.
- Loop 11 is not a clean loop even after these fixes pass validation.
