# Harbor

Private OCI registry managed by Argo CD with the upstream Harbor Helm chart.

- Tailnet URL: `https://harbor.tailb6c7d.ts.net`
- Storage: Longhorn retain volumes; 50 GiB for image layers, online-expandable
- Scanner: Trivy
- Metrics: Prometheus `ServiceMonitor`
- Credentials: macOS Keychain -> `scripts/bootstrap-cluster-secrets.sh`

The existing registry at `172.16.40.200:5000` is intentionally retained until
image migration, client cutover, and Harbor recovery verification are complete.
