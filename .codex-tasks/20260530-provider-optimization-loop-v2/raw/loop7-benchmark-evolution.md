## Loop 7 Benchmark Evolution Analysis

- Date: 2026-05-31
- Mandatory per-loop decision: should benchmark rows / query shapes / tool mappings / repeat counts / timeout budgets / structural-failure accounting / scoring change this loop?

## Decision: benchmark definition and runner stay unchanged in Loop 7

- Keep the formal 41-row benchmark definition unchanged.
- Keep the current runner mappings, rate-limit-aware special batching, timeout budgets, and structural-failure accounting unchanged.

## Evidence

- `factual-accuracy-01` exposed a runtime answer-synthesis issue, not a missing benchmark row or a comparator failure.
- `crawl-map-02` exposed a runtime breadth/semantics gap in `crawl_site`, not a runner mapping issue. The row already measures the right behavior by starting from a deep documentation page and comparing coverage against Tavily `tavily_crawl`.
- Loop 6’s clean rerun already proved the current runner can capture all 41 rows with 0 structural failures once the runtime behaves correctly.

## Convergence implication

Loop 7 does not reset or expand the formal benchmark definition. After the Loop 7 runtime fixes are committed, pushed, and redeployed, the next required gate is a fresh full 41-row comparison under the existing final matrix.
