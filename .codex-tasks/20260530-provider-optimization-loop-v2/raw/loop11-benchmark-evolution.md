## Loop 11 Benchmark Decision

- Date: 2026-07-17
- Decision: keep the 41 rows and ten capability-chain dimensions unchanged; replace the runner evidence and scoring contract.

## Runner Contract Changes

- Emit score columns for `authority_precision`, `semantic_discovery`, `provider_orchestration`, `multi_source_fusion`, `content_fidelity`, `freshness_signal`, `site_coverage`, `traceability`, `resilience`, and `efficiency`.
- Produce a deterministic winner only from observable contract evidence. The reason explicitly states that semantic correctness is not inferred.
- Preserve complete valid JSON provider traces.
- Store every repeat observation with summary, URLs, latency, success state, and cold/warm marker.
- Separate normal orchestration from `fallback_attempted`, `fallback_used`, and `fallback_reason`.
- Enforce `latency_budget_ms` in row status and efficiency scoring.

## Rationale

- Existing rows already expose the discovered blind spots; adding rows or dimensions would not improve coverage.
- The output schema and runner behavior change materially, so prior clean-loop counts cannot be earned from an old CSV.
- A fresh full 41-row comparison is mandatory after release and deployment.
