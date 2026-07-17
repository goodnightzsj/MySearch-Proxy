## Loop 10 Provider Contract Refresh

- Date: 2026-07-17
- Primary source: https://github.com/chenyme/grok2api
- Main branch inspected at commit `6aa160f`; deployed container revision is `e05aada`.

## Current grok2api v3 Contract

- Public inference routes include `GET /v1/models`, `POST /v1/responses`, `POST /v1/chat/completions`, and `POST /v1/messages`.
- Inference routes are grouped below `/v1` and protected by client-key authentication.
- Client keys are created in the v3 administrator UI/API and use the `g2a_` prefix.
- Administrator routes moved to `/api/admin/v1/*` and use an administrator login/session contract. They no longer expose the v2 `app.api_key` inheritance contract.
- The deployed account currently exposes `grok-4.20-0309`, `grok-4.3`, and `grok-4.5`; live `x_search` probes verified `grok-4.20-0309` and `grok-4.3`.

## Decision

- Keep `SOCIAL_GATEWAY_UPSTREAM_BASE_URL=<root>/v1` and `SOCIAL_GATEWAY_UPSTREAM_RESPONSES_PATH=/responses`.
- Require an explicit v3 `g2a_` key for inference.
- Retain v2 admin auto-inheritance only as a legacy compatibility path and reject HTML/non-object responses explicitly.
- Do not add implicit v3 administrator login or store administrator credentials in the Social gateway.
