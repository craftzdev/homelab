# Business Gateway

Phase 1 の最小Gateway実装。PostgreSQLをSystem of Recordとし、Job登録、型付きWorker APIへのdispatch、状態取得、Worker Event受付を提供する。

## Listener

- Public API: `127.0.0.1:8080`
- Worker callback: `127.0.0.1:8081`
- Internal workflow API: `127.0.0.1:8082`

Public APIはcloudflared、callback APIとinternal workflow APIはTailscale Serveからのみ
proxyする。internal surfaceは Workflow Controller 専用で、`CONTROLLER_API_TOKEN` を
持つ主体だけが `/internal/v1/...` を使える。Grok とブラウザが通る public ingress には
出さない。

Gateway VM 上の Tailscale Serve は次の2経路である。callback は root、internal は
`/internal` 配下に置く（同じ 443 なので、Tailnet ACL は `tcp:443` のままで足りる）。

```
sudo tailscale serve --bg http://127.0.0.1:8081                              # /
sudo tailscale serve --bg --set-path=/internal http://127.0.0.1:8082/internal  # /internal/v1/...
```

cluster 側の Workflow Controller には secret `ai-business-workflow-controller` で
`gateway-internal-url=https://ai-gateway-01.<tailnet>.ts.net` と、VM の `.env` と同じ
`controller-api-token` を渡す（`scripts/bootstrap-cluster-secrets.sh` が macOS Keychain
から材料化する）。

The public listener intentionally remains on loopback. The deployed Cloudflare
Tunnel exposes it at `https://gateway.craftz.dev`; the application Bearer token
and Cloudflare Access Service Auth are active. The Gateway validates the Access
JWT again at the application boundary. Do not bind port 8080 to a LAN address
as a workaround.

## Tests

```sh
docker compose -f compose.test.yaml run --rm tests     # this repository
docker compose -f compose.e2e.yaml run --rm e2e        # with the Agent's own code
```

The second one mounts the sibling `ai-business-agent` checkout and runs its real
Workflow Controller and its real dispatch service against this Gateway: one
request goes from start to human acceptance, and every handoff the Controller
builds is admitted by the Gateway and then validated by the Agent's registry and
JSON schemas before delivery. What these three exchange on that path therefore
fails here rather than in the cluster. It walks one path only: other actions,
other field sizes and other refusals are covered — where they are covered — by
each repository's own tests. It skips when that checkout is not present.

What it does not establish: the Worker is a double, so nothing here proves Codex
execution, the Worker's own durability, or the Control Plane's read model. The
Worker's and the Control Plane's own suites cover their logic — the Worker's
executor tests drive a stand-in `codex` program. **No test, and no run recorded in
this repository, has exercised the real Codex CLI through this Workflow.** The
manual run in `docs/task-ledger.md` used a Worker double as well. What that leaves
unproven is the agent execution itself; everything around it — admission,
progression, verification binding, delivery, cancellation — is covered.

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
- `get_production_approval`
- `list_capabilities`
- `list_workflows`
- `create_task`
- `get_task`
- `list_tasks`
- `request_task_action`
- `add_task_instruction`

Task tools share the Job state machine: every accepted job belongs to a Task, and
`submit_job` now returns `task_id` alongside `job_id`. Configuration editing,
Harness permissions, Worker capability changes and human-only decisions are not
exposed. See [Task 台帳](docs/task-ledger.md) for the implemented scope and the
guarantees that are explicitly not in place yet.

`submit_job` accepts only the Agent/Worker-backed actions `product.plan`,
`code.build`, `code.fix`, `test.run`, `qa.review`, `analytics.read`,
`growth.plan`, `browser.research`, and `stripe.read`.
Only `research` and `preview` environments are available. Production,
deployment, publishing, and other irreversible actions are not exposed through
MCP.

Production approval is deliberately absent from MCP. It requires the independent
`X-Human-Approval-Token` header in addition to Cloudflare Access and the Gateway
Bearer token.

Approval is bound to a registered immutable release candidate, a server-side
human identity, and an expiration time. See [bound approval API](docs/approvals.md)
for the migration, request bodies, and deployment checklist. Legacy unbound
approvals cannot be used after migration. `get_production_approval` is read-only;
it allows either Grok Bot or Hermes to recover approval state without chat memory.

A pending or granted-but-unused production request can be withdrawn with
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

## Video generation

REST `/v1/jobs` and MCP `submit_job` accept `video.generate` using a fixed,
bounded FastH3 workflow. See [API usage and operational limits](docs/video-generation.md).
