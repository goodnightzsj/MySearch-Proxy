## Loop 11 Fixes And Validation

- Date: 2026-07-18
- Status: runtime release and deployment complete; runner credential-transport follow-up validated locally; final comparison pending.

## Fixes

- Reworked the benchmark runner around the ten active dimensions, complete trace JSON, per-attempt repeat evidence, cold/warm latency, explicit orchestration/fallback fields, latency-budget status, and observable winner scoring.
- Scoped software prerelease markers to the candidate sentence and retained explicit result-year rejection for award extraction.
- Replaced the marketing hero with a compact operations bar, global status strip, sticky Provider rail, one active workspace, and progressive `overview/token/key` tabs.
- Moved all overlays outside `#dashboard` and centralized background inertness, overlay priority, `aria-hidden`, focus trapping, and focus restoration.
- Added skip links, complete tab/radiogroup contracts, labels for dynamic inputs, assertive login errors, reduced-motion scrolling, numeric mobile inputs, and responsive summary/provider state bands.
- Kept the mobile Base URL metadata on its own full-width line so the deployed host does not orphan the final port digit at 375px.
- Updated grok2api v3 model, client-key, and endpoint guidance in the console.
- Removed the benchmark bearer from process arguments. The one-time remote script and encoded payload now travel only through SSH stdin, while the visible command remains `python3 -`.

## Local Validation

- `node --check proxy/static/js/console.js`: passed.
- Python `py_compile` for touched runtime and benchmark files: passed.
- `python3 -m pytest -q`: 637 passed.
- Runner credential regression set: 35 passed.
- Real one-row SSH benchmark passed, and process inspection confirmed neither the local runner nor child SSH command exposed the bearer in argv.
- Browser smoke at 320, 375, 768, and 1440 CSS pixels: no page-level horizontal overflow, duplicate IDs, broken `aria-controls`, unlabeled visible inputs, or nameless visible buttons.
- Keyboard smoke: workspace/settings/radiogroup arrow navigation passed; detail drawer, settings modal, and nested confirmation dialog inert/focus behavior passed.
- Light, dark, desktop, mobile, and `/mysearch` page screenshots were captured under `/tmp` for visual inspection.

## Release And Deploy Verification

- Runtime commits `71840fe`, `6399b8a`, and `d5deccc` were pushed to `main`.
- Docker workflow `29631966606` succeeded for `d5deccc`.
- Remote `mysearch-stack` runs image revision `d5deccc1a8a1ba1424bcd894df5c5d42972a34b9` with the required ports, mount, and `restart=always` policy.
- Proxy health reports grok2api v3 admin connectivity; MCP initialize returned HTTP 200 with a session ID.
- Live authenticated browser smoke passed at 375px and 1440px with no horizontal overflow or duplicate IDs; settings overlay background inertness and `aria-modal` passed.

## Pending Gates

- Commit and push the runner credential-transport follow-up, then wait for CI. It is runner-only and does not require another runtime deploy.
- Run the fresh full 41-row comparison under the Loop 11 runner contract and update convergence.
