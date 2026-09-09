#!/usr/bin/env bash
# Materialize non-Git cluster secrets from the local macOS Keychain.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
KUBECONFIG_PATH="${KUBECONFIG:-${REPO_ROOT}/_out/kubeconfig}"
GRAFANA_KEYCHAIN_SERVICE="${GRAFANA_KEYCHAIN_SERVICE:-dev.craftz.homelab.grafana-admin}"
GRAFANA_KEYCHAIN_ACCOUNT="${GRAFANA_KEYCHAIN_ACCOUNT:-admin}"

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

for namespace in monitoring cert-manager security image-registry tailscale \
  arc-systems arc-runners; do
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
ok "Cluster bootstrap secrets are present"
