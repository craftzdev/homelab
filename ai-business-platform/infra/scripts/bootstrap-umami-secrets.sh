#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
KUBECONFIG_PATH="${1:-${REPO_ROOT}/_out/kubeconfig}"

# Secret は argv に載せず標準入力から渡す（lib/secrets.sh の apply_secret）。
# shellcheck source=scripts/lib/secrets.sh
source "${REPO_ROOT}/scripts/lib/secrets.sh"
KUBECTL_SECRET_ARGS=(--kubeconfig "${KUBECONFIG_PATH}")
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

apply_secret "${ANALYTICS_NAMESPACE}" umami-db-owner kubernetes.io/basic-auth \
  "username=umami" \
  "password=${db_password}"

apply_secret "${ANALYTICS_NAMESPACE}" umami-runtime "" \
  "DATABASE_URL=${database_url}" \
  "APP_SECRET=${app_secret}"

for namespace in "${ANALYTICS_NAMESPACE}" "${LOGGING_NAMESPACE}"; do
  apply_secret "${namespace}" umami-s3-credentials "" \
    "ACCESS_KEY_ID=${minio_access}" \
    "ACCESS_SECRET_KEY=${minio_secret}"
done

unset db_password app_secret minio_secret database_url
echo "Umami runtime, database, administrator, and backup credentials are reconciled."
