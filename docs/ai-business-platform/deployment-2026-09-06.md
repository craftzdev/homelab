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
| Cloudflare hostname | `https://gateway.craftz.dev` | Tunnel and DNS active; public health/API verified |
| Cloudflare Tunnel | `ai-business-gateway` / `1cb360c1-26be-4f23-b3d3-728689073a04` | Connected over four QUIC sessions |
| Tailscale policy | Gateway tag + deny-by-default Grants | Active |

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
  `gateway.craftz.dev` reached the loopback-only public API. Public health,
  HTTP 401 without a token, and authenticated Job creation were verified.
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

1. Cloudflare Access Service Auth and Gateway-side Access JWT validation are
   not implemented yet. The public Job API still requires its independent
   Bearer token, but completing Edge authentication is required before issuing
   credentials to Grok Bot.
2. Proxmox Backup Server is reachable through the existing Tailscale node, but
   no PBS storage or scheduled backup is registered in Proxmox. The temporary
   local backup is not a replacement for PBS.
3. Ceph remains `HEALTH_WARN`: BlueStore slow-operation indications are now
   reported for `osd.0` and `osd.2`. Data placement is clean, but the device and
   I/O path warning must be investigated before production load is increased.
4. The Mac Studio Worker API and its executors are a separate deployment step;
   this deployment validates the Proxmox Gateway and callback ingress only.
