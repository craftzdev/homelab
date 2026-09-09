# Tailscale Kubernetes Operator

The Operator provides Tailnet-only HTTPS ingress for the AI Worker and Argo CD,
plus an egress proxy back to the Business Gateway. Argo CD is available only at
`https://argocd.<tailnet>.ts.net` and uses the dedicated `tag:argocd` identity.
OAuth credentials are created with
`Devices Core`, `Auth Keys`, and `Services` read/write scopes and the
`tag:k8s-operator` tag. Credentials are installed directly as a Kubernetes
Secret and are not stored in Git.

Cilium runs with `socketLB.hostNamespaceOnly=true`, allowing the Operator's
proxy Pods to apply their own DNAT rules. `DNSConfig` resolves only configured
Tailnet ingress/egress names inside the cluster; CoreDNS forwards the
`tailb6c7d.ts.net` zone to the nameserver address reported in
`dnsconfig.status.nameserver.ip`.
