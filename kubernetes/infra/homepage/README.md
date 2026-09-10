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

The PBS card is link-only. Do not reuse the existing backup-write token for a
dashboard. A future PBS widget must receive a separate Audit-only identity.
