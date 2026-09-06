# AI Business Platform Gateway deployment report

実施日: 2026-09-06 (JST)

## Secure rebuild update

The original Gateway deployment below has been superseded by the Talos/local-ZFS
rebuild performed on the same day.

| Item | Current state |
|---|---|
| Ceph | Decommissioned on all three Proxmox nodes after PBS backup and CephFS archive verification |
| Proxmox storage | `local-zfs` online on `sv-proxmox-01` through `sv-proxmox-03` |
| Gateway VM | VM `1200` running from `local-zfs` on `sv-proxmox-01` |
| Gateway durability | PBS backups at `172.16.10.51`; ZFS replication jobs `1200-0` and `1200-1` to nodes 2 and 3 every five minutes |
| Gateway HA verification | Online migration to node 2 and back completed; both API surfaces remained healthy afterward |
| Talos | `1.13.9`, three dedicated control-plane nodes plus three dedicated workers, all Ready |
| Kubernetes | `1.34.3`, API VIP `172.16.40.10` |
| CNI | Cilium `1.20.1`, kube-proxy replacement, WireGuard, L2 LoadBalancer, Hubble healthy |
| Storage | Longhorn `1.12.1`; all five persistent volumes are healthy and replicas are restricted to the dedicated workers |
| K8s Worker | Running on the dedicated worker plane in namespace `ai-worker`; restricted non-root Pod, Codex CLI and API health verified |
| Gateway dispatcher | Deployed; typed Worker endpoint mapping and Bearer authentication verified against an isolated Worker |
| Tailnet cutover | Tailscale Operator ingress/egress and bidirectional HTTPS verified |
| Mac Studio Worker | Removed after Kubernetes cutover; launchd, account/group, home, plist, Serve, and Worker Grants absent |
| Internal registry | TLS registry at `172.16.40.200:5000`, 20 GiB Longhorn 3-replica PVC |
| Codex end-to-end | Gateway job `9b828a64-1f96-4261-af02-10f3c29c7160` succeeded with tests and signed review artifacts |
| Post-migration smoke test | Gateway job `054f75e6-50d2-4bc1-a837-687574f72c21` reached `SUCCEEDED` through Cloudflare Access, Gateway, Tailnet HTTPS, and the Kubernetes Worker |

## Final one-command rebuild validation (2026-09-07 JST)

The production rebuild command completed successfully from commit `531adae`:

```bash
./scripts/rebuild-talos-cluster.sh \
  --execute \
  --confirm-destroy-six-k8s-vms
```

For the final repeat test, the already verified PBS snapshots and encrypted
application recovery set were reused with `--resume-after-backup`. The recovery
set was `_out/rebuild-backups/20260906T120841Z`. The command independently
audited its destroy plan before applying it and restricted deletion to VMIDs
`1001`, `1002`, `1003`, `1101`, `1102`, and `1103`. Gateway VM `1200` remained
running throughout.

| Verification | Result |
|---|---|
| Proxmox VM placement | One control plane and one worker running on each of `sv-proxmox-01` through `sv-proxmox-03` |
| Talos / Kubernetes | Six nodes Ready; three control-plane and three worker nodes |
| etcd | Three voting members, no learners |
| API stability | All three direct kube-apiserver endpoints and all nodes Ready for 12 consecutive five-second samples |
| GitOps | All 12 Argo CD Applications `Synced/Healthy` |
| Storage | Five Longhorn volumes `attached/healthy`; replica nodes restricted to the three workers |
| Infrastructure drift | `tofu plan -detailed-exitcode` returned `0` (`No changes`) |
| Tailnet continuity | Worker ingress and Gateway egress device IDs matched the encrypted pre-rebuild state |
| TLS continuity | Cached Tailnet certificate state restored before proxy startup; no ACME issuance or rate-limit event in the new proxy logs |
| Worker health | `https://ai-worker-cluster.tailb6c7d.ts.net/health` returned healthy with Codex Builder enabled |
| End-to-end | Gateway job `054f75e6-50d2-4bc1-a837-687574f72c21` reached `SUCCEEDED` |

Cold-start image downloads from external registries were slow and temporarily
increased local-ZFS I/O latency. The rebuild therefore uses dependency gates
and bounded convergence windows for Cilium, Longhorn CSI, every Pod, and the
final authenticated smoke test. After image convergence, all three API servers
remained continuously Ready for the final stability sample.

## Six-VM destructive rebuild validation

The complete Talos cluster was destroyed and recreated from the OpenTofu state
on 2026-09-06. The guarded destroy plan contained exactly VMIDs `1001`, `1002`,
`1003`, `1101`, `1102`, and `1103`; Gateway VM `1200` was explicitly excluded.
Before deletion, all six VMs were snapshotted to `pbs-gateway` and an encrypted
application recovery set was written to
`_out/rebuild-backups/20260906T024154Z`.

All six replacement nodes reached `Ready` with three tainted control-plane
nodes and three dedicated workers. Registry and Worker data were restored to
new Longhorn volumes; each volume has one healthy replica on every worker and
none on the control plane. Tailscale Operator ingress/egress identities were
recreated, CoreDNS forwarding for `tailb6c7d.ts.net` was restored, and
`https://ai-worker-cluster.tailb6c7d.ts.net/health` returned healthy.

The post-rebuild end-to-end test created Gateway job
`ad231cde-5ff6-434b-9bbb-5e5de067f573`. It passed Cloudflare Access rejection,
Gateway Bearer authentication, idempotency and conflict checks, Tailnet Worker
dispatch, Worker callback delivery, duplicate event handling, and ended in
`SUCCEEDED`.

The additional PBS snapshot containing the rebuilt Gateway and dispatcher is
`pbs-gateway:backup/vm/1200/2026-09-05T19:33:25Z`.

## Original Gateway deployment state (before secure rebuild)

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
| Mac Studio Worker | `business-worker:business-worker` launchd daemon / `127.0.0.1:8080` | Codex Builder active |
| Worker Tailnet HTTPS | `https://macstudio.tailb6c7d.ts.net` | Tailscale Serve active |
| Worker source | Private `craftzdev/ai-business-worker` / commit `0189afb` | Eight tests passing and pushed |

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
- A live `code.build` job was sent from the Gateway VM through Tailscale HTTPS
  to the Mac Studio. Codex added a `multiply` function and unittest coverage in
  an isolated clone, changed only the two requested files, and all four tests
  passed. The Worker delivered `accepted`, `started`, and `completed` events;
  Gateway PostgreSQL reached `SUCCEEDED` with the same Worker Job ID.
- The live verification used Gateway Job
  `7ae6d29d-7649-4d21-98cd-d432db49da21`, Worker Job
  `wjob_0ee583cd9a964017acaaeffb5d360c2f`, and Codex Thread
  `01a07293-d963-7e92-b55a-b1d29d697730`. The Tailnet-only signed review page
  returned HTTP 200 and displayed the summary, changed files, tests, and artifacts.
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

## Historical open production gates (resolved or superseded)

1. PBS backup for VM 1200 was completed before Ceph decommissioning; recurring
   ZFS replication now targets both remaining Proxmox nodes.
2. Ceph was decommissioned and replaced by per-node `local-zfs`; Kubernetes PVs
   use Longhorn three-replica storage.
3. Codex Builder is active in the restricted Kubernetes Worker boundary with
   fixed test commands, time/output limits, and review artifacts. Playwright and
   Data executors, durable callback outbox/retry, and automatic log secret
   redaction remain implementation work.
