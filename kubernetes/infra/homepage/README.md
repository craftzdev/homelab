# Homepage operations portal

Homepage is exposed only inside the Tailnet at:

```text
https://portal.tailb6c7d.ts.net
```

The Ingress uses the same `tag:argocd` identity as the existing administrative
UIs, so the Tailnet Grant remains the authentication boundary. Homepage's MCP
endpoint and Kubernetes ServiceAccount token are disabled.

The Proxmox widget uses the passwordless, privilege-separated
`homepage@pve!homepage` API token. Both the user and token receive only the
built-in `PVEAuditor` role. Create or reconcile it with:

```bash
./scripts/reconcile-homepage-proxmox-token.sh
```

The token secret is kept in macOS Keychain under service
`dev.craftz.homelab.homepage-proxmox-token` and materialized as the
`portal/homepage-integrations` Secret. It is never stored in Git.

The Homepage PBS card is link-only. The Grafana observer uses its own PBS
Audit token; it never reuses the existing backup-write token.

The `pbs-exporter` workload supplies Grafana with PBS datastore usage and
Proxmox backup-job status through its own Proxmox identity,
`grafana@pve!monitor`; it does not share Homepage's token, so the two rotate
independently and the PVE task log can tell them apart. Both the user and its
privilege-separated token hold `PVEAuditor` on `/` without propagation (the
backup schedule), on `/nodes` (the vzdump task lists) and on
`/storage/pbs-gateway` (that storage's status) — read-only in every case, and
narrower than Homepage's site-wide `PVEAuditor`. Reconcile it with:

```bash
./scripts/reconcile-pve-monitoring-token.sh
```

The secret is stored in macOS Keychain as `dev.craftz.homelab.grafana-pve-token`
(account `grafana@pve!monitor`) and Kubernetes
`portal/pbs-observer-pve-credentials`. The script verifies the credential
against all three endpoints the exporter calls before writing the Secret, so a
too-narrow ACL fails there instead of appearing later as a source failure.

Direct PBS host, snapshots, verification and GC data use `grafana@pbs!monitor`.
Both the PBS user and privilege-separated token have `Audit` on `/system` and
`DatastoreAudit` on `/datastore/gateway-backup`. No write, restore, prune or
verification execution privileges are granted. Reconcile the saved token with:

```bash
python3 scripts/reconcile-pbs-monitoring-token.py
```

The secret is stored in macOS Keychain as `dev.craftz.homelab.grafana-pbs-token`
(account `grafana@pbs!monitor`) and Kubernetes `portal/pbs-observer-credentials`.
Existing tokens are reused; a missing Keychain copy does not trigger rotation.
The script requires SSH key access to `root@172.16.10.51`; initial setup uses
`scripts/setup-pbs-ssh-key.sh`. That script registers the key with
`no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc`, and
rewrites an already-registered entry that lacks those options; pass
`MANAGEMENT_CIDR=<address>` to also pin the source address. Key login was
tested independently after setup; password authentication was not disabled.

The separate observer policy permits only `172.16.10.11:8006` and
`172.16.10.51:8007` egress. PBS TLS uses the public `pbs-ca.pem` certificate
retrieved through trusted SSH, with full CA and hostname verification.
The pod maps `pbs.home.arpa` to `172.16.10.51` via `hostAliases` because the
certificate contains the hostname, not the management IP. Nothing resolves
names at runtime, including the HTTP server's bind path: `http.server` would
call `getfqdn()` between `bind()` and `listen()`, which stalls without DNS and
lets the liveness probe kill a container whose port is bound but not yet
listening, so `Server.server_bind` skips that lookup. Refresh this public
certificate through trusted SSH if PBS rotates it. Other namespaces, including
AI workloads, receive neither these credentials nor a PBS network exception.
Only Prometheus may scrape its ClusterIP metrics endpoint
(plus node-origin health probes). No Kubernetes API token is mounted.

The existing PVE root CA lacks the `keyUsage` extension. The Python 3.13 client
disables only the new `VERIFY_X509_STRICT` compatibility flag; `CERT_REQUIRED`,
hostname validation and the explicitly mounted PVE CA remain enforced.

The observer is reconciled with this Kustomization. Its Python standard-library
collector polls every 60 seconds; Prometheus scrapes every 30 seconds. Source
failures remove the affected values and report failure, rather than retaining
an old healthy value. A payload missing an expected field is handled the same
way: only that source drops out with `pbs_observer_source_success 0`, while the
other sources, the collection timestamp and the scrape itself are unaffected.
The dashboard also checks exporter availability and a 180-second freshness
limit. `/healthz` only checks the HTTP process; source
health is represented by `pbs_observer_source_success`.

Direct PBS storage metrics describe `gateway-backup`; PVE storage and schedules
are specific to `pbs-gateway`.
Node task panels are the last completed **vzdump** job on each Proxmox node,
regardless of datastore, from the most recent 100 backup tasks per node.
24-hour failure counts cover that bounded history. Task success is not a
snapshot-integrity or restore test. Direct PBS snapshots cover the datastore's
root namespace only. Missing or unknown verification states are unverified,
not successful. VM freshness measures the newest stored snapshot, not whether
the whole job succeeded. The dashboard's 48/72-hour colors are advisory and
are not a per-VM backup SLA. Zero enabled verification schedules is shown as
unconfigured. GC results do not imply snapshot verification or restoreability.
No backup payload, file list, owner or task log is exported.

Check `up{job="pbs-observer"}`, `pbs_observer_collection_success`, and
`pbs_observer_source_success` (all 1) and
`time() - pbs_observer_collection_timestamp_seconds` (<180).
To remove the observer, remove the resource and code generator under
`pbs-exporter`, its ServiceMonitor, and its dashboard ConfigMap, then delete
the `grafana@pve` and `grafana@pbs` identities and their two Secrets. Homepage's
own Proxmox credential is separate and stays untouched.
