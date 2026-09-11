# Business Gateway

Phase 1 の最小Gateway実装。PostgreSQLをSystem of Recordとし、Job登録、型付きWorker APIへのdispatch、状態取得、Worker Event受付を提供する。

## Listener

- Public API: `127.0.0.1:8080`
- Worker callback: `127.0.0.1:8081`

Public APIはcloudflared、callback APIはTailscale Serveからのみproxyする。

The public listener intentionally remains on loopback. The deployed Cloudflare
Tunnel exposes it at `https://gateway.craftz.dev`; the application Bearer token
and Cloudflare Access Service Auth are active. The Gateway validates the Access
JWT again at the application boundary. Do not bind port 8080 to a LAN address
as a workaround.

## Grok Bot MCP

The public process also exposes a stateless Streamable HTTP MCP endpoint:

```text
https://gateway.craftz.dev/mcp
```

It is an adapter over the existing Gateway job model; the REST API, PostgreSQL
state machine, Worker dispatch, and callback contract remain authoritative.
The MCP surface intentionally exposes only these tools:

- `submit_job`
- `get_job`
- `wait_for_job`
- `get_review_url`
- `submit_business_idea`
- `get_project`
- `request_production_approval`

`submit_job` accepts only the Agent/Worker-backed actions `product.plan`,
`code.build`, `code.fix`, `test.run`, `qa.review`, `analytics.read`,
`growth.plan`, `browser.research`, and `stripe.read`.
Only `research` and `preview` environments are available. Production,
deployment, publishing, and other irreversible actions are not exposed through
MCP.

Production approval is deliberately absent from MCP. It requires the independent
`X-Human-Approval-Token` header in addition to Cloudflare Access and the Gateway
Bearer token.

A pending production request can be withdrawn with
`POST /v1/approvals/{approval_id}/cancel`. Cancellation restores the state
derived from the latest QA verdict and records an audit event.

Preview-only validation data is accepted without a production release when the
analytics payload contains `"scope": "preview_validation"`. It is stored as
validation evidence, moves the project to `VALIDATION_MEASURING`, and must not
be presented as real-user production analytics.

Every MCP request must pass both authentication layers:

```text
CF-Access-Client-Id: <Cloudflare Access service-token client ID>
CF-Access-Client-Secret: <Cloudflare Access service-token client secret>
Authorization: Bearer <Gateway API token>
```

Cloudflare first authenticates the service token and injects
`Cf-Access-Jwt-Assertion`. The Gateway then verifies that JWT and independently
verifies its own Bearer token. Do not put any of these values in a Bot prompt,
repository file, shared skill, or connector URL.

## Local start

```bash
cp .env.example .env
# .envの3値を安全なランダム値へ変更する
docker compose up -d --build
```

## Health

```bash
curl --fail http://127.0.0.1:8080/health
curl --fail http://127.0.0.1:8080/ready
curl --fail http://127.0.0.1:8081/health
```

The authenticated job and callback smoke test requires the Cloudflare Service
Token at runtime when Access validation is enabled. Do not save those two values
in the Gateway `.env` file:

```bash
sudo --preserve-env=CF_ACCESS_CLIENT_ID,CF_ACCESS_CLIENT_SECRET \
  /opt/ai-business-gateway/scripts/smoke-test.sh
```

The MCP discovery and end-to-end job test can be run from an environment that
already has the three credentials in memory:

```bash
python scripts/mcp-smoke-test.py
```

The deployed callback listener is available inside the Tailnet at:

```text
https://ai-gateway-01.tailb6c7d.ts.net
```

Infrastructure policy tracked in this repository:

- `../infra/cloudflared/config.yml`
- `../infra/tailscale/policy.hujson`

Deployment evidence and outstanding production gates are recorded in
`docs/ai-business-platform/deployment-2026-09-06.md`.
