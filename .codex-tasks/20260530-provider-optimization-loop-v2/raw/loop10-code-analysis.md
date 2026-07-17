## Loop 10 Code Analysis

- Date: 2026-07-17
- Surface: runtime, provider compatibility, docs, tests
- Baseline: deployed MySearch image `7ec7a62-local`; deployed grok2api image revision `e05aada`

## Findings

1. The deployed Social/X primary model was `grok-4.20-fast`, but grok2api v3 `GET /v1/models` no longer exposed that model. Every Social/X request first failed and then retried with `grok-4.20-0309-non-reasoning`.
2. grok2api v3 still registers inference at `POST /v1/responses`; the effective MySearch target remains `<grok2api-root>/v1/responses`. The breaking contract change is authentication: v3 requires a `g2a_` client key created by the administrator instead of inheriting the v2 `app.api_key`.
3. The v2 admin fallback paths can return the grok2api v3 frontend SPA with HTTP 200. Both Social gateway implementations converted non-object responses to `{}` and could report `admin_connected=true` with empty statistics.
4. The standalone Social gateway created an `asyncio.Lock` at module import. Under Python 3.9, importing it after an isolated event loop was closed raised `RuntimeError: There is no current event loop`.
5. The first post-deploy 41-row comparison exposed a second version-extraction regression: Python's official downloads page says "development versions of Python 3.15", but the negative marker list only recognized `development branch`. The extractor promoted prerelease `3.15` over stable `3.14.6`.

## Provider Smoke Results

- Tavily: live and healthy.
- Firecrawl: endpoint healthy and ordinary search returns results. A strict `include_domains=[docs.python.org]` query returned no Firecrawl result and correctly fell back to Tavily; this is an existing provider-result behavior, not an endpoint failure.
- Exa: direct provider search returned official Python documentation results.
- X/Social: `grok-4.20-0309` and `grok-4.3` both returned HTTP 200 through `POST /v1/responses` with `x_search`.

## Severity And Outcome

- The invalid default primary model, false admin health signal, and prerelease version promotion are actionable runtime issues.
- Loop 10 is not a clean loop even after the fixes pass validation.
