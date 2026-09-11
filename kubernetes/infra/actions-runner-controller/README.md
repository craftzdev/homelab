# Kubernetes GitHub Actions runner

Actions Runner Controller (ARC) creates ephemeral runner Pods. The
`homelab-runner` scale set validates `craftzdev/homelab` without a container
daemon. Two repository-scoped scale sets build application images:

- `ai-agent-builder` for `craftzdev/ai-business-agent`
- `ai-worker-builder` for `craftzdev/ai-business-worker`
- `ai-control-plane-builder` for `craftzdev/ai-business-control-plane`

The builder Pods use ARC's ephemeral Docker-in-Docker mode and disappear after
each job. Cilium permits them to reach public HTTPS plus
`harbor.tailb6c7d.ts.net:443`; access to the Kubernetes API and home networks
remains blocked.

Every scale set has zero idle runners and allows at most one concurrent job.
Runner Pods receive no Kubernetes ServiceAccount token.

ARC authenticates with the pre-created `arc-github-app` Secret in the
`arc-runners` namespace. It must contain `github_app_id`,
`github_app_installation_id`, and `github_app_private_key`. Never commit these
values. The GitHub App needs repository `Administration: read and write` and
`Metadata: read-only`. Every scale set is bound to one exact repository URL.

The current installation uses GitHub App `craftz-homelab-arc-runner` (App ID
`4890688`, installation ID `160444144`). Its private key is also stored as
base64 in the admin Mac login Keychain under service
`dev.craftz.homelab.arc-github-app-private-key`; the downloaded PEM must not be
kept in `Downloads`. Cluster rebuild recovery additionally preserves the live
Secret as an age-encrypted recovery artifact.

Check the installation with:

```bash
kubectl -n arc-systems get deploy,pods
kubectl -n arc-runners get autoscalingrunnersets,ephemeralrunnersets,pods
```
