# Tailscale Kubernetes Operator

The Operator provides Tailnet-only HTTPS ingress for the AI Worker, Argo CD,
Grafana, and Hubble UI, plus an egress proxy back to the Business Gateway. The
management UIs are available at `https://argocd.<tailnet>.ts.net`,
`https://grafana.<tailnet>.ts.net`, and `https://hubble.<tailnet>.ts.net`. All
of them use the existing admin-only `tag:argocd` policy, so Tailnet Grants must
allow only administrators to reach `tag:argocd` over TCP 443.

Hubble UI is deployed by the Cilium chart with `hubble.ui.ingress.enabled=false`
so `hubble-ingress.yaml` stays the single publication path. Its backing Service
remains ClusterIP, and `kubectl -n kube-system port-forward svc/hubble-ui
12000:80` remains the fallback when the Tailnet is unavailable.
OAuth credentials are created with
`Devices Core`, `Auth Keys`, and `Services` read/write scopes and the
`tag:k8s-operator` tag. Credentials are installed directly as a Kubernetes
Secret and are not stored in Git.

Cilium runs with `socketLB.hostNamespaceOnly=true`, allowing the Operator's
proxy Pods to apply their own DNAT rules. `DNSConfig` resolves only configured
Tailnet ingress/egress names inside the cluster; CoreDNS forwards the
`tailb6c7d.ts.net` zone to the nameserver address reported in
`dnsconfig.status.nameserver.ip`.
