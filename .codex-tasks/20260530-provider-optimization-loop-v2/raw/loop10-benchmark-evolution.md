## Loop 10 Benchmark Decision

- Date: 2026-07-17
- Decision: keep the 41-row capability-chain matrix unchanged.

## Rationale

- The new finding is a provider compatibility regression in the Social/X model and authentication path.
- Existing `provider_orchestration`, `freshness_signal`, `resilience`, and Social/X rows already exercise this behavior.
- No provider added or removed a capability that requires a new dimension or row.
- The stable dimensions remain `authority_precision`, `semantic_discovery`, `provider_orchestration`, `multi_source_fusion`, `content_fidelity`, `freshness_signal`, `site_coverage`, `traceability`, `resilience`, and `efficiency`.

## Required Comparison

- After deployment, rerun the full 41-row matrix using the Loop 9 capability-chain input.
- Loop 10 cannot count as clean because it found actionable runtime issues before the final rerun.
