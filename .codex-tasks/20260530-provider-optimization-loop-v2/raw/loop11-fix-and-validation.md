## Loop 11 Fixes And Validation

- Date: 2026-07-18
- Status: deployed baseline `55cee0c` produced the latest finding-discovery run; the factual-answer and provider-key release candidate is fully validated locally; release and comparison pending.

## Fixes

- Reworked the benchmark runner around the ten active dimensions, complete trace JSON, per-attempt repeat evidence, cold/warm latency, explicit orchestration/fallback fields, latency-budget status, and observable winner scoring.
- Scoped software prerelease markers to the candidate sentence and retained explicit result-year rejection for award extraction.
- Replaced the marketing hero with a compact operations bar, global status strip, sticky Provider rail, one active workspace, and progressive `overview/token/key` tabs.
- Moved all overlays outside `#dashboard` and centralized background inertness, overlay priority, `aria-hidden`, focus trapping, and focus restoration.
- Added skip links, complete tab/radiogroup contracts, labels for dynamic inputs, assertive login errors, reduced-motion scrolling, numeric mobile inputs, and responsive summary/provider state bands.
- Kept the mobile Base URL metadata on its own full-width line so the deployed host does not orphan the final port digit at 375px.
- Updated grok2api v3 model, client-key, and endpoint guidance in the console.
- Removed the benchmark bearer from process arguments. The one-time remote script and encoded payload now travel only through SSH stdin, while the visible command remains `python3 -`.
- Bounded verify and Hybrid provider work, including unified xAI and compatible Tavily/Exa fallback calls, while preserving the configurable pure-Social timeout contract.
- Added exact canonical rescue/ranking for active OpenAI pricing, OpenAI Batch, Next.js generateMetadata, and pricing career-page rejection cases.
- Treat requested content without any returned content as an enrichment failure. Continue the fallback chain, but preserve the successful discovery result with explicit evidence if enrichment remains unavailable.
- Preserve meaningful arXiv titles across canonical URL merge and rerun hCaptcha trailing-widget cleanup after language-block removal.
- Added explicit expected-answer evidence with boundary/negation-safe matching, and synchronized input contract fields during partial reruns.
- Run strict domain-filtered docs through the bounded verify blend, while keeping Tavily on fast discovery and reserving requested-content retrieval for Firecrawl/Exa verifiers.
- Ask Tavily and Exa verifier branches for content when a Firecrawl-primary blend requests content, and prefer Exa before Firecrawl in the changelog fallback chain.
- Add the exact Playwright `test.step` canonical anchor and remove only fully identified embedded Cloudflare challenge blocks without truncating surrounding content.
- Check official Oscars/Grammys HTML before provider extraction so a slow extraction path cannot hide a directly available event answer.
- Separate temporary per-key `429` cooldown from terminal quota/auth isolation across direct, Proxy, and Social paths; preserve the longest cooldown and prevent terminal states from being downgraded by late concurrent responses.
- Preserve complete structured provider error evidence for classification while keeping user-facing summaries concise, and redact secrets before truncating upstream errors.
- Expose manual key recovery, replacement, clearing, and real schedulability in the console; public health now returns a fixed Social admin error summary instead of upstream diagnostics.

## Local Validation

- `node --check proxy/static/js/console.js`: passed.
- Python `py_compile` for touched runtime and benchmark files: passed.
- `python -m unittest discover -s tests`: 655 passed for the early 84-column candidate.
- `pytest -q tests/test_clients.py tests/test_loop4_fixes.py tests/test_remote_benchmark_config.py`: 420 passed.
- Runner credential regression set: 35 passed.
- Real one-row SSH benchmark passed, and process inspection confirmed neither the local runner nor child SSH command exposed the bearer in argv.
- Browser smoke at 320, 375, 768, and 1440 CSS pixels: no page-level horizontal overflow, duplicate IDs, broken `aria-controls`, unlabeled visible inputs, or nameless visible buttons.
- Keyboard smoke: workspace/settings/radiogroup arrow navigation passed; detail drawer, settings modal, and nested confirmation dialog inert/focus behavior passed.
- Light, dark, desktop, mobile, and `/mysearch` page screenshots were captured under `/tmp` for visual inspection.
- Follow-up validation: `python -m pytest -q tests/test_clients.py` passed 373 tests; `python -m unittest discover -s tests` passed 661 tests; runtime sync, `py_compile`, and `git diff --check` passed.
- The provider-key scheduling candidate adds deterministic direct/proxy/Social coverage for all four direct providers, managed proxy-token boundaries, numeric and HTTP-date `Retry-After`, temporary `429` cooldown and recovery, permanent quota/auth isolation, manual restore/replace/clear, account-level quota exhaustion, staggered SQLite cooldowns, Social multi-key rotation/health, and Firecrawl crawl key ownership. The dedicated scheduling set passes 66 tests.
- Final release-candidate validation: runtime mirrors are byte-identical; Python compilation, `node --check`, `git diff --check`, and secret diff scan pass; `python -m unittest discover -s tests` passes 728 tests.

## Release And Deploy Verification

- Runtime commits `71840fe`, `6399b8a`, and `d5deccc` were pushed to `main`.
- Docker workflow `29631966606` succeeded for `d5deccc`.
- Remote `mysearch-stack` runs image revision `d5deccc1a8a1ba1424bcd894df5c5d42972a34b9` with the required ports, mount, and `restart=always` policy.
- Proxy health reports grok2api v3 admin connectivity; MCP initialize returned HTTP 200 with a session ID.
- Live authenticated browser smoke passed at 375px and 1440px with no horizontal overflow or duplicate IDs; settings overlay background inertness and `aria-modal` passed.
- Commit `14badab` was pushed; Docker workflow `29637368712` succeeded; the remote container reports image revision `14badabf9674a0e6b821cb337cfda487ad881df6`.
- The complete 41-row postdeploy run had no structural failures, but its 81-column output is retained only as an intermediate artifact because the findings above changed both runtime and scorer contracts.
- The first 84-column run on `30153c1` captured all 41 unique rows with no structural failure, timeout, empty result, or row error and a 39-2 row win count for MySearch. Six MySearch budget overruns and the content/canonical issues above make it an intermediate artifact rather than a convergence result.
- Commit `55cee0c` passed Docker workflow `29644084979`, was deployed as the immutable stack image, and produced a fresh 41/41, 84-column comparison. MySearch had zero timeout, empty result, budget overrun, or row error and won 40 rows; the run nevertheless exposed a Python stable-version regression on `factual-accuracy-01`, so it is an intermediate finding-discovery artifact.

## Pending Gates

- Commit and push the factual-answer plus provider-key scheduling candidate, wait for Docker CI, and redeploy because runtime files changed.
- Run a fresh full 41-row comparison under the final 84-column Loop 11 contract, record the runner commit/schema, and update convergence.
