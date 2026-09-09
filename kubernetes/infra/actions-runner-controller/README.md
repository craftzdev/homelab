# Kubernetes GitHub Actions runner

Actions Runner Controller (ARC) creates an ephemeral runner Pod for trusted
push and manual workflows in `craftzdev/homelab`. Pull requests continue to
use `ubuntu-latest`, so code from a fork never executes inside the home
network.

The scale set is named `homelab-runner`, has zero idle runners, and allows at
most one concurrent job. Runner Pods receive no Kubernetes ServiceAccount
token, run without privilege escalation, and are restricted to public HTTPS
egress. RFC1918, Tailnet, and link-local destinations are denied.

ARC authenticates with the pre-created `arc-github-app` Secret in the
`arc-runners` namespace. It must contain `github_app_id`,
`github_app_installation_id`, and `github_app_private_key`. Never commit these
values. The GitHub App needs repository `Administration: read and write` and
`Metadata: read-only`; install it only on `craftzdev/homelab`.

The current installation uses GitHub App `craftz-homelab-arc-runner` (App ID
`4890688`, installation ID `160444144`). Its private key is also stored in the
admin Mac login Keychain under service
`dev.craftz.homelab.arc-github-app-private-key`; the downloaded PEM must not be
kept in `Downloads`. Cluster rebuild recovery additionally preserves the live
Secret as an age-encrypted recovery artifact.

Check the installation with:

```bash
kubectl -n arc-systems get deploy,pods
kubectl -n arc-runners get autoscalingrunnersets,ephemeralrunnersets,pods
```
