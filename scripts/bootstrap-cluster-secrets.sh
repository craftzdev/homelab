#!/usr/bin/env bash
# Materialize non-Git cluster secrets from the local macOS Keychain.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
KUBECONFIG_PATH="${KUBECONFIG:-${REPO_ROOT}/_out/kubeconfig}"
GRAFANA_KEYCHAIN_SERVICE="${GRAFANA_KEYCHAIN_SERVICE:-dev.craftz.homelab.grafana-admin}"
GRAFANA_KEYCHAIN_ACCOUNT="${GRAFANA_KEYCHAIN_ACCOUNT:-admin}"
MINIO_KEYCHAIN_SERVICE="${MINIO_KEYCHAIN_SERVICE:-dev.craftz.homelab.minio-root}"
MINIO_KEYCHAIN_ACCOUNT="${MINIO_KEYCHAIN_ACCOUNT:-minio-root}"
LOKI_S3_KEYCHAIN_SERVICE="${LOKI_S3_KEYCHAIN_SERVICE:-dev.craftz.homelab.loki-s3}"
LOKI_S3_KEYCHAIN_ACCOUNT="${LOKI_S3_KEYCHAIN_ACCOUNT:-loki}"
WORKER_REPO_KEYCHAIN_SERVICE="${WORKER_REPO_KEYCHAIN_SERVICE:-dev.craftz.homelab.argocd-ai-worker-deploy-key}"
WORKER_REPO_KEYCHAIN_ACCOUNT="${WORKER_REPO_KEYCHAIN_ACCOUNT:-craftzdev/ai-business-worker}"
WORKER_REPO_URL="${WORKER_REPO_URL:-git@github.com:craftzdev/ai-business-worker.git}"
HOMEPAGE_PVE_KEYCHAIN_SERVICE="${HOMEPAGE_PVE_KEYCHAIN_SERVICE:-dev.craftz.homelab.homepage-proxmox-token}"
HOMEPAGE_PVE_KEYCHAIN_ACCOUNT="${HOMEPAGE_PVE_KEYCHAIN_ACCOUNT:-homepage@pve!homepage}"

info() { printf '[INFO] %s\n' "$*"; }
ok() { printf '[OK]   %s\n' "$*"; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

for tool in kubectl security openssl; do
  command -v "${tool}" >/dev/null || die "required command not found: ${tool}"
done
[[ -s "${KUBECONFIG_PATH}" ]] || die "kubeconfig not found: ${KUBECONFIG_PATH}"

export KUBECONFIG="${KUBECONFIG_PATH}"
kubectl version -o json >/dev/null 2>&1 || die "cluster is not reachable"

if grafana_password="$(security find-generic-password \
    -s "${GRAFANA_KEYCHAIN_SERVICE}" -a "${GRAFANA_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)"; then
  info "Using the existing Grafana credential from macOS Keychain"
else
  info "Creating the initial Grafana credential in macOS Keychain"
  grafana_password="$(openssl rand -base64 32)"
  security add-generic-password -U \
    -s "${GRAFANA_KEYCHAIN_SERVICE}" \
    -a "${GRAFANA_KEYCHAIN_ACCOUNT}" \
    -w "${grafana_password}" >/dev/null
fi
[[ -n "${grafana_password}" ]] || die "Grafana password is empty"

for namespace in monitoring logging logging-audit cert-manager security \
  image-registry tailscale arc-systems arc-runners argocd portal; do
  kubectl create namespace "${namespace}" --dry-run=client -o yaml \
    | kubectl apply -f - >/dev/null
done

# The generated password is base64 text, so it is safe to place in stringData.
# It is intentionally never printed or persisted to a temporary file.
kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: grafana-admin
  namespace: monitoring
type: Opaque
stringData:
  admin-user: admin
  admin-password: "${grafana_password}"
EOF

unset grafana_password

# MinIO root and Loki's dedicated S3 credential are independent. Loki never
# receives the MinIO administrator password. Values are generated only once and
# kept in the macOS Keychain so a complete cluster rebuild produces the same
# application credentials without committing them to Git.
if minio_root_password="$(security find-generic-password \
    -s "${MINIO_KEYCHAIN_SERVICE}" -a "${MINIO_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)"; then
  info "Using the existing MinIO root credential from macOS Keychain"
else
  info "Creating the initial MinIO root credential in macOS Keychain"
  minio_root_password="$(openssl rand -base64 32)"
  security add-generic-password -U \
    -s "${MINIO_KEYCHAIN_SERVICE}" \
    -a "${MINIO_KEYCHAIN_ACCOUNT}" \
    -w "${minio_root_password}" >/dev/null
fi
[[ -n "${minio_root_password}" ]] || die "MinIO root password is empty"

if loki_s3_secret="$(security find-generic-password \
    -s "${LOKI_S3_KEYCHAIN_SERVICE}" -a "${LOKI_S3_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)"; then
  info "Using the existing Loki S3 credential from macOS Keychain"
else
  info "Creating the initial Loki S3 credential in macOS Keychain"
  loki_s3_secret="$(openssl rand -base64 32)"
  security add-generic-password -U \
    -s "${LOKI_S3_KEYCHAIN_SERVICE}" \
    -a "${LOKI_S3_KEYCHAIN_ACCOUNT}" \
    -w "${loki_s3_secret}" >/dev/null
fi
[[ -n "${loki_s3_secret}" ]] || die "Loki S3 secret is empty"

kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: minio-root-credentials
  namespace: logging
type: Opaque
stringData:
  root-user: minio-root
  root-password: "${minio_root_password}"
---
apiVersion: v1
kind: Secret
metadata:
  name: loki-s3-credentials
  namespace: logging
type: Opaque
stringData:
  AWS_ACCESS_KEY_ID: loki
  AWS_SECRET_ACCESS_KEY: "${loki_s3_secret}"
EOF

unset minio_root_password loki_s3_secret

# Homepage gets a Proxmox API token with PVEAuditor only. Token creation and
# ACL reconciliation are handled by reconcile-homepage-proxmox-token.sh; this
# bootstrap step only copies the existing secret from Keychain into Kubernetes.
if homepage_pve_token="$(security find-generic-password \
    -s "${HOMEPAGE_PVE_KEYCHAIN_SERVICE}" \
    -a "${HOMEPAGE_PVE_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)"; then
  [[ -n "${homepage_pve_token}" ]] || die "Homepage Proxmox token is empty"
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: homepage-integrations
  namespace: portal
type: Opaque
stringData:
  HOMEPAGE_VAR_PROXMOX_USERNAME: "${HOMEPAGE_PVE_KEYCHAIN_ACCOUNT}"
  HOMEPAGE_VAR_PROXMOX_TOKEN: "${homepage_pve_token}"
EOF
  unset homepage_pve_token
  ok "Homepage Proxmox credential is present"
else
  info "Homepage Proxmox token is absent from Keychain; run reconcile-homepage-proxmox-token.sh"
fi

# The private Worker repository uses a repository-scoped, read-only GitHub
# Deploy Key. The Keychain value is base64 so multiline OpenSSH key material
# survives an exact round trip through the `security` CLI.
if worker_repo_key_b64="$(security find-generic-password \
    -s "${WORKER_REPO_KEYCHAIN_SERVICE}" \
    -a "${WORKER_REPO_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)"; then
  worker_repo_key="$(printf '%s' "${worker_repo_key_b64}" | openssl base64 -d -A)"
  [[ "${worker_repo_key}" == '-----BEGIN OPENSSH PRIVATE KEY-----'* ]] \
    || die "Worker repository Deploy Key in Keychain is invalid"
  indented_worker_repo_key="$(printf '%s\n' "${worker_repo_key}" | sed 's/^/    /')"
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: ai-business-worker-repository
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
type: Opaque
stringData:
  type: git
  url: ${WORKER_REPO_URL}
  sshPrivateKey: |
${indented_worker_repo_key}
EOF
  unset worker_repo_key_b64 worker_repo_key indented_worker_repo_key
  ok "Argo CD Worker repository credential is present"
else
  info "Worker repository Deploy Key is absent from Keychain; recovery restore must provide it"
fi

ok "Cluster bootstrap secrets are present"
