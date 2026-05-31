## Loop 7 Provider Capability Research

- Date: 2026-05-31
- Scope: targeted refresh for the remaining `crawl_site` coverage gap on the clean Loop 6 baseline.

## Firecrawl official findings

- Official source: `https://docs.firecrawl.dev/v1/api-reference/endpoint/crawl-post`
- Current docs still expose `maxDiscoveryDepth` plus breadth controls including `crawlEntireDomain` and the deprecated `allowBackwardLinks`.
- The docs explicitly state that `crawlEntireDomain=false` only follows deeper child paths. From a deep path such as `/tutorial/background-tasks/`, sibling or parent URLs are not crawled unless broader internal traversal is enabled.
- The docs also say `maxDiscoveryDepth=1` should crawl the entered URL plus linked URLs on that page when discovery breadth allows it.

## Interpretation for MySearch

- The Loop 6 `crawl-map-02` result of `count=1` is consistent with MySearch inheriting Firecrawl’s narrow default breadth, not with a benchmark-runner bug.
- Because MySearch exposes this provider capability as the user-facing `crawl_site` tool, keeping the narrow provider default is a product-semantic gap: the abstraction says “crawl site”, but a deep page only crawls deeper descendants unless the caller knows and controls an upstream-specific breadth flag that MySearch did not expose.

## Other providers

- No new Tavily, Exa, or xAI capability delta was needed to explain the Loop 7 findings.
- The factual-version-answer issue was traced to MySearch’s own merged-result post-processing rather than to a newly changed upstream provider contract.

## Decision

- Actionable provider-alignment change: expose Firecrawl `crawlEntireDomain` through MySearch `crawl_site`, default it to broader internal traversal, and keep the public parameter provider-agnostic in snake_case as `crawl_entire_domain`.
