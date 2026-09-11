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

## Renovate

`.github/workflows/renovate.yaml` runs Renovate on `homelab-runner` every
Saturday at 10:00 JST, inside the `after 9am and before 6pm on saturday` window
that `renovate.json` declares. It is a weekly few-minute job, so it uses the
existing ephemeral runner instead of a resident Pod or a CronJob and consumes no
GitHub-hosted minutes.

`renovatebot/github-action` is deliberately not used: it starts a Docker
container, and this scale set has no container daemon. The workflow installs a
version-pinned Renovate npm package instead. Both the Renovate and Node.js pins
live in the workflow's `env:` block behind `# renovate:` comments, and a regex
manager in `renovate.json` keeps them updated like any other dependency.

Renovate authenticates as its own GitHub App, `homelab-renovate` — separate
from the ARC Controller App, whose private key is never reused. A `GITHUB_TOKEN`
would not work here: pull requests it creates do not start the `validate`
workflow, which is most of the value of having Renovate at all. The workflow
mints a short-lived installation token at run time and never stores one.

Register the App's credentials as repository Actions secrets:

```text
RENOVATE_APP_ID
RENOVATE_APP_PRIVATE_KEY
```

Repository permissions, kept to the minimum Renovate needs:

| Permission | Level | Why |
| --- | --- | --- |
| Metadata | Read-only | Required for every App |
| Contents | Read and write | Push update branches |
| Pull requests | Read and write | Open and update PRs |
| Issues | Read and write | Dependency Dashboard |
| Workflows | Read and write | Update pinned Action versions |
| Commit statuses / Checks | Read-only | Read existing CI results |

Nothing is auto-merged, including vulnerability fixes: Renovate opens the PR and
the existing CI plus a human approval decide. `concurrency` lets a running
Renovate finish rather than cancelling it, because an interrupted run leaves
half-created branches behind.

Validate configuration changes before pushing them — this needs no credentials:

```bash
npx --package renovate@<pinned version> renovate-config-validator
npx --package renovate@<pinned version> renovate --platform=local --dry-run=extract
```

The local extract only sees files Git tracks, so `git add` a new manifest before
checking that a manager picks it up. `renovate.json` has no key for comments;
put explanations in the top-level `description` or in a rule's `description`, or
Renovate opens a config-warning issue.

Check the installation with:

```bash
kubectl -n arc-systems get deploy,pods
kubectl -n arc-runners get autoscalingrunnersets,ephemeralrunnersets,pods
```
