#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
KUBECONFIG_PATH="${1:-${REPO_ROOT}/_out/kubeconfig}"
DEPLOY_KEY_PATH="${CONTROL_PLANE_DEPLOY_KEY_PATH:-/Users/craftz/.ssh/argocd-ai-business-control-plane}"
NAMESPACE=ai-control-plane
RUNTIME_SECRET=ai-business-control-plane-runtime

[[ -s "${KUBECONFIG_PATH}" ]] || {
  echo "kubeconfig not found: ${KUBECONFIG_PATH}" >&2
  exit 1
}

[[ -s "${DEPLOY_KEY_PATH}" ]] || {
  echo "control plane deploy key not found: ${DEPLOY_KEY_PATH}" >&2
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

kubectl --kubeconfig "${KUBECONFIG_PATH}" create namespace "${NAMESPACE}" \
  --dry-run=client -o yaml | kubectl --kubeconfig "${KUBECONFIG_PATH}" apply -f - >/dev/null

admin_token="$(ensure_generated_keychain_secret dev.craftz.ai-business-control-plane.admin-token)"
session_secret="$(ensure_generated_keychain_secret dev.craftz.ai-business-control-plane.session-secret)"
gateway_token="$(read_keychain dev.craftz.ai-business-gateway.gateway-api-token)"
cf_client_id="$(read_keychain dev.craftz.ai-business-gateway.cloudflare-access-client-id)"
cf_client_secret="$(read_keychain dev.craftz.ai-business-gateway.cloudflare-access-client-secret)"

kubectl --kubeconfig "${KUBECONFIG_PATH}" -n "${NAMESPACE}" \
  create secret generic "${RUNTIME_SECRET}" \
  --from-literal=admin-token="${admin_token}" \
  --from-literal=session-secret="${session_secret}" \
  --from-literal=gateway-api-token="${gateway_token}" \
  --from-literal=cf-access-client-id="${cf_client_id}" \
  --from-literal=cf-access-client-secret="${cf_client_secret}" \
  --dry-run=client -o yaml \
  | kubectl --kubeconfig "${KUBECONFIG_PATH}" apply -f - >/dev/null

kubectl --kubeconfig "${KUBECONFIG_PATH}" -n ai-worker get secret harbor-pull -o json \
  | jq 'del(.metadata.creationTimestamp,.metadata.resourceVersion,.metadata.uid,.metadata.ownerReferences,.metadata.managedFields) | .metadata.namespace="ai-control-plane"' \
  | kubectl --kubeconfig "${KUBECONFIG_PATH}" apply -f - >/dev/null

deploy_key="$(<"${DEPLOY_KEY_PATH}")"

kubectl --kubeconfig "${KUBECONFIG_PATH}" -n argocd \
  create secret generic ai-business-control-plane-repository \
  --from-literal=type=git \
  --from-literal=url=git@github.com:craftzdev/ai-business-control-plane.git \
  --from-literal=project=ai-business \
  --from-literal=sshPrivateKey="${deploy_key}" \
  --dry-run=client -o yaml \
  | kubectl --kubeconfig "${KUBECONFIG_PATH}" apply -f - >/dev/null
kubectl --kubeconfig "${KUBECONFIG_PATH}" -n argocd label secret \
  ai-business-control-plane-repository \
  argocd.argoproj.io/secret-type=repository --overwrite >/dev/null

unset admin_token session_secret gateway_token cf_client_id cf_client_secret
unset deploy_key
echo "Control Plane runtime and repository credentials are reconciled."
