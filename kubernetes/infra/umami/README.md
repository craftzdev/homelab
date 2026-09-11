# Umami analytics

Umami v3 runs in the `analytics` namespace and is available only to Tailnet
administrators at `https://umami.tailb6c7d.ts.net`.

- The application image is pinned to the v3.3.1 multi-architecture digest.
- CloudNativePG operates three PostgreSQL 17 instances across the worker nodes.
- Each database instance uses a retained, single-replica Longhorn volume. Native
  PostgreSQL streaming replication supplies database redundancy without ninefold
  block-level replication.
- Barman Cloud archives WAL continuously and creates a daily base backup in the
  `umami-postgres` bucket on the existing MinIO service. The stable Barman
  server name is `umami-postgres-v1`; increment it only when intentionally
  starting a new backup lineage. Retention is 30 days.
- Runtime credentials stay in macOS Keychain and Kubernetes Secrets, never Git.

Reconcile secrets before the first Argo CD sync:

```bash
ai-business-platform/infra/scripts/bootstrap-umami-secrets.sh
```

Retrieve the administrator password after bootstrap:

```bash
security find-generic-password -s dev.craftz.umami.admin-password -w
```
