# Business Gateway

Phase 1 の最小Gateway実装。PostgreSQLをSystem of Recordとし、Job登録、状態取得、Worker Event受付を提供する。

## Listener

- Public API: `127.0.0.1:8080`
- Worker callback: `127.0.0.1:8081`

Public APIはcloudflared、callback APIはTailscale Serveからのみproxyする。

The public listener intentionally remains on loopback. The deployed Cloudflare
Tunnel exposes it at `https://gateway.craftz.dev`; the application Bearer token
is active, while Cloudflare Access and Gateway-side Access JWT validation remain
production gates. Do not bind port 8080 to a LAN address as a workaround.

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

On the Gateway VM, the authenticated job and callback smoke test can be rerun
without printing credentials:

```bash
sudo /opt/ai-business-gateway/scripts/smoke-test.sh
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
