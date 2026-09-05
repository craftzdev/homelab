# AI Business Platform Gateway deployment report

実施日: 2026-09-06 (JST)

## Deployed state

| Item | Value | State |
|---|---|---|
| Proxmox VM | `1200` / `ai-gateway-01` | Running on `sv-proxmox-01` |
| Compute | 2 vCPU / 4 GiB RAM / 64 GiB disk | Applied |
| Network | VLAN 40 / `172.16.40.30/24` | Applied |
| Storage | `cephrdb_vm` | Applied |
| Guest | Ubuntu 24.04 / QEMU Guest Agent | Running |
| Gateway | FastAPI public and callback surfaces | Running |
| Database | PostgreSQL 17 | Healthy |
| Host firewall | deny incoming; management SSH/Tailscale ingress; management/Ceph/workload VLAN egress denied | Active |
| HA resource | `vm:1200` | Started |
| HA rule | `ai-gateway-placement` | In use |
| Tailscale node | `ai-gateway-01` / `100.104.73.43` | Connected |
| Tailscale HTTPS | `https://ai-gateway-01.tailb6c7d.ts.net` | Callback surface healthy |
| Cloudflare hostname | `https://gateway.craftz.dev` | Tunnel, Access Service Auth, and Gateway JWT validation active |
| Cloudflare Tunnel | `ai-business-gateway` / `1cb360c1-26be-4f23-b3d3-728689073a04` | Connected over four QUIC sessions |
| Cloudflare Access app | `AI Business Gateway - Grok Service` / `63028f65-73a8-4319-a12e-96897bd23ac4` | Service token policy active |
| Tailscale policy | Gateway tag + deny-by-default Grants | Active |
| Mac Studio Worker | `business-worker` launchd daemon / `127.0.0.1:8080` | Running in self-test-only mode |
| Worker Tailnet HTTPS | `https://macstudio.tailb6c7d.ts.net` | Tailscale Serve active |
| Worker source | Local `ai-business-worker` repository / commit `ca4cbc1` | Six API tests passing; private GitHub publication pending |

The HA node preference is `sv-proxmox-01:3`, `sv-proxmox-02:2`, and
`sv-proxmox-03:1`, with strict placement, `max_restart=1`,
`max_relocate=1`, and `failback=0`.

## Verification completed

- Proxmox quorum and all three HA agents are healthy.
- All three Ceph OSDs are up/in and all 129 placement groups are
  `active+clean`.
- A live migration from `sv-proxmox-01` to `sv-proxmox-02` and back completed
  successfully. Observed migration downtime was 76 ms and 65 ms.
- Guest reboot restored Docker, PostgreSQL, both API surfaces, UFW, and
  Tailscale Serve automatically.
- The Gateway rejected an unauthenticated request with HTTP 401 and hid the
  callback route from the public listener with HTTP 404.
- Job creation, idempotent replay, conflicting idempotency key rejection,
  callback authentication, duplicate event handling, monotonic sequence
  enforcement, and final `SUCCEEDED` state were verified against the deployed
  PostgreSQL instance.
- Tailscale issued a valid HTTPS certificate and the Mac Studio reached the
  callback health endpoint over the Tailnet.
- The dedicated Cloudflare Tunnel registered four QUIC connections and
  `gateway.craftz.dev` reached the loopback-only public API. Cloudflare Access
  rejects requests without a Service Token with HTTP 403. A valid Service
  Token reaches the Gateway, but Job creation still returns HTTP 401 without
  the independent Gateway Bearer token. Supplying both credentials returns
  HTTP 202.
- The Gateway validates the `Cf-Access-Jwt-Assertion` signature, issuer,
  audience, expiry, and issued-at claims. Direct-origin requests with a missing
  or forged assertion returned HTTP 401.
- The Mac Studio Worker runs as the login-disabled `business-worker` user under
  launchd. Its API token is required, SQLite persists jobs, and the public
  listener remains bound to loopback behind Tailscale Serve.
- A real self-test job was sent from the Gateway VM through Tailscale HTTPS to
  the Mac Studio. The Worker returned HTTP 202, completed successfully, and
  delivered `accepted`, `started`, and `completed` events back to the Gateway.
  PostgreSQL reached `SUCCEEDED` with event sequence 3 and the same Worker Job ID.
- The Gateway is tagged `tag:ai-gateway`. Tailnet policy allows only HTTPS
  between it and the current Mac Studio worker host (or future worker tags).
  A Tailnet SSH connection to the Gateway was rejected while HTTPS remained
  available.
- The Gateway host firewall rejected new connections to the Proxmox management,
  Ceph public, and Ceph cluster networks. Management SSH into the Gateway, DNS,
  Cloudflare egress, and Tailscale HTTPS continued to work.
- A snapshot backup was created on Proxmox local storage. It was restored to
  isolated VM `1299`; the guest agent, containers, API readiness, and restored
  PostgreSQL rows were checked. The temporary restore VM and its disks were
  then removed.

## Open production gates

1. Proxmox Backup Server is reachable through the existing Tailscale node, but
   no PBS storage or scheduled backup is registered in Proxmox. The temporary
   local backup is not a replacement for PBS.
2. Ceph remains `HEALTH_WARN`: BlueStore slow-operation indications are now
   reported for `osd.0` and `osd.2`. Data placement is clean, but the device and
   I/O path warning must be investigated before production load is increased.
3. The Worker is intentionally limited to `operation=self_test`. Codex,
   Playwright, project test, and data executors; process isolation; callback
   outbox/retry; and the Gateway scheduler remain implementation work.
4. The new Worker repository exists and is committed locally. It should be
   published as the private `craftzdev/ai-business-worker` repository after
   GitHub authentication is configured on this Mac.
