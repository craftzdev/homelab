#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
KUBECONFIG_PATH="${1:-${REPO_ROOT}/_out/kubeconfig}"
ANALYTICS_NAMESPACE=analytics
LOGGING_NAMESPACE=logging

[[ -s "${KUBECONFIG_PATH}" ]] || {
  echo "kubeconfig not found: ${KUBECONFIG_PATH}" >&2
  exit 1
}

read_keychain() {
  security find-generic-password -s "$1" -w
}

ensure_generated_keychain_secret() {
  local service=$1 value
  value="$(read_keychain "${service}" 2>/dev/null || true)"
  if [[ -z "${value}" ]]; then
    value="$(openssl rand -hex 32)"
    security add-generic-password -U -a craftz -s "${service}" -w "${value}" >/dev/null
  fi
  printf '%s' "${value}"
}

kubectl --kubeconfig "${KUBECONFIG_PATH}" create namespace "${ANALYTICS_NAMESPACE}" \
  --dry-run=client -o yaml | kubectl --kubeconfig "${KUBECONFIG_PATH}" apply -f - >/dev/null

db_password="$(ensure_generated_keychain_secret dev.craftz.umami.db-password)"
app_secret="$(ensure_generated_keychain_secret dev.craftz.umami.app-secret)"
ensure_generated_keychain_secret dev.craftz.umami.admin-password >/dev/null
minio_secret="$(ensure_generated_keychain_secret dev.craftz.umami.minio-secret-key)"
minio_access=umami-cnpg
database_url="postgresql://umami:${db_password}@umami-postgres-rw.analytics.svc.cluster.local:5432/umami?sslmode=verify-full&sslrootcert=/etc/umami/postgres-ca/ca.crt"

kubectl --kubeconfig "${KUBECONFIG_PATH}" -n "${ANALYTICS_NAMESPACE}" \
  create secret generic umami-db-owner \
  --type=kubernetes.io/basic-auth \
  --from-literal=username=umami \
  --from-literal=password="${db_password}" \
  --dry-run=client -o yaml \
  | kubectl --kubeconfig "${KUBECONFIG_PATH}" apply -f - >/dev/null

kubectl --kubeconfig "${KUBECONFIG_PATH}" -n "${ANALYTICS_NAMESPACE}" \
  create secret generic umami-runtime \
  --from-literal=DATABASE_URL="${database_url}" \
  --from-literal=APP_SECRET="${app_secret}" \
  --dry-run=client -o yaml \
  | kubectl --kubeconfig "${KUBECONFIG_PATH}" apply -f - >/dev/null

for namespace in "${ANALYTICS_NAMESPACE}" "${LOGGING_NAMESPACE}"; do
  kubectl --kubeconfig "${KUBECONFIG_PATH}" -n "${namespace}" \
    create secret generic umami-s3-credentials \
    --from-literal=ACCESS_KEY_ID="${minio_access}" \
    --from-literal=ACCESS_SECRET_KEY="${minio_secret}" \
    --dry-run=client -o yaml \
    | kubectl --kubeconfig "${KUBECONFIG_PATH}" apply -f - >/dev/null
done

unset db_password app_secret minio_secret database_url
echo "Umami runtime, database, administrator, and backup credentials are reconciled."
