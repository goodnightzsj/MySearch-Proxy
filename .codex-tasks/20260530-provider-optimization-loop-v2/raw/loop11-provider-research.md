## Loop 11 Provider Capability Delta

- Date: 2026-07-17
- Decision: no new provider capability research is required beyond the Loop 10 refresh.

## Delta Check

- Tavily, Firecrawl, and Exa contracts were live-smoked in Loop 10 and no new endpoint or schema failure appeared during Loop 11 Inspect.
- Current `chenyme/grok2api` source still registers inference under `/v1`, with `POST /v1/responses` as the primary Responses API and `POST /v1/chat/completions` plus `POST /v1/messages` retained as compatibility routes.
- The active MySearch Social target remains `<grok2api-root>/v1/responses`; grok2api v3 requires an explicit `g2a_` client key for inference.
- Loop 11 findings concern benchmark evidence integrity and console delivery, not a newly changed provider contract.
