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
- Add `expected_url_patterns` for exact canonical resource rows and `expected_answer_patterns=3.14.6` for the time-sensitive Python factual row. Expected-answer matching is literal, boundary-aware, negation-aware, and affects freshness rather than manufacturing source authority.
- Raise only the pure Social/X cold-start budget from 20 seconds to 30 seconds. This matches the runtime's documented minimum configurable Social budget and the observed 25.66-second cold call; the web+X Hybrid budget remains 20 seconds.
- Synchronize all input-owned contract fields before scoring reused rows so a partial rerun cannot publish stale matrix metadata.

## Rationale

- Existing rows already expose the discovered blind spots; adding rows or dimensions would not improve coverage. The row count remains 41, while the input contract becomes 19 columns and the output contract becomes 84 columns.
- The output schema and runner behavior change materially, so prior clean-loop counts cannot be earned from an old CSV.
- A fresh full 41-row comparison is mandatory after release and deployment.
