#!/usr/bin/env bash
# Create and reconcile Homepage's read-only Proxmox API identity.
# Values are validated before intentional client-side expansion in SSH command
# strings. Proxmox's CLI is available only on the remote host.
# shellcheck disable=SC2029
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PVE_HOST="${PVE_HOST:-172.16.10.11}"
PVE_USER="${PVE_USER:-homepage@pve}"
PVE_TOKEN_ID="${PVE_TOKEN_ID:-homepage}"
PVE_TOKEN_FULL="${PVE_USER}!${PVE_TOKEN_ID}"
KEYCHAIN_SERVICE="${HOMEPAGE_PVE_KEYCHAIN_SERVICE:-dev.craftz.homelab.homepage-proxmox-token}"
KUBECONFIG_PATH="${KUBECONFIG:-${REPO_ROOT}/_out/kubeconfig}"
KUBE_SERVER="${KUBE_SERVER:-https://172.16.40.13:6443}"

info() { printf '[INFO] %s\n' "$*"; }
ok() { printf '[OK]   %s\n' "$*"; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

for tool in curl jq kubectl security ssh; do
  command -v "${tool}" >/dev/null || die "required command not found: ${tool}"
done
[[ -s "${KUBECONFIG_PATH}" ]] || die "kubeconfig not found: ${KUBECONFIG_PATH}"
[[ "${PVE_HOST}" =~ ^[0-9]+(\.[0-9]+){3}$ ]] \
  || die "PVE_HOST must be an IPv4 address"
[[ "${PVE_USER}" =~ ^[A-Za-z][A-Za-z0-9._-]*@pve$ ]] \
  || die "PVE_USER is invalid"
[[ "${PVE_TOKEN_ID}" =~ ^[A-Za-z][A-Za-z0-9._-]+$ ]] \
  || die "PVE_TOKEN_ID is invalid"

ssh_options=(-o BatchMode=yes -o ConnectTimeout=10)
users_json="$(ssh "${ssh_options[@]}" root@"${PVE_HOST}" \
  'pveum user list --output-format json')"
if ! jq -e --arg user "${PVE_USER}" \
    '.[] | select(.userid == $user)' <<<"${users_json}" >/dev/null; then
  info "Creating passwordless Proxmox service user ${PVE_USER}"
  ssh "${ssh_options[@]}" root@"${PVE_HOST}" \
    "pveum user add '${PVE_USER}' --comment 'Homepage read-only dashboard' --enable 1"
fi

# Both the user and the privilege-separated token need the read-only ACL.
ssh "${ssh_options[@]}" root@"${PVE_HOST}" \
  "pveum acl modify / --users '${PVE_USER}' --roles PVEAuditor --propagate 1"

tokens_json="$(ssh "${ssh_options[@]}" root@"${PVE_HOST}" \
  "pveum user token list '${PVE_USER}' --output-format json")"
token_exists=false
if jq -e --arg tokenid "${PVE_TOKEN_ID}" \
    '.[] | select(.tokenid == $tokenid)' <<<"${tokens_json}" >/dev/null; then
  token_exists=true
fi

if pve_token_secret="$(security find-generic-password \
    -s "${KEYCHAIN_SERVICE}" -a "${PVE_TOKEN_FULL}" -w 2>/dev/null)"; then
  [[ -n "${pve_token_secret}" ]] || die "Keychain token is empty"
  [[ "${token_exists}" == true ]] \
    || die "Keychain contains a token that no longer exists in Proxmox"
  info "Using the existing Proxmox token from macOS Keychain"
else
  [[ "${token_exists}" == false ]] || die \
    "Proxmox token exists but its non-retrievable secret is absent from Keychain"
  info "Creating privilege-separated Proxmox API token"
  token_json="$(ssh "${ssh_options[@]}" root@"${PVE_HOST}" \
    "pveum user token add '${PVE_USER}' '${PVE_TOKEN_ID}' --privsep 1 --comment 'Homepage read-only dashboard' --output-format json")"
  pve_token_secret="$(jq -er '.value' <<<"${token_json}")"
  security add-generic-password -U \
    -s "${KEYCHAIN_SERVICE}" -a "${PVE_TOKEN_FULL}" \
    -w "${pve_token_secret}" >/dev/null
  unset token_json
fi

ssh "${ssh_options[@]}" root@"${PVE_HOST}" \
  "pveum acl modify / --tokens '${PVE_TOKEN_FULL}' --roles PVEAuditor --propagate 1"

# Test the exact credential against Proxmox before publishing it to the Pod.
http_code="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  --cacert "${REPO_ROOT}/kubernetes/infra/homepage/pve-root-ca.pem" \
  -H "Authorization: PVEAPIToken=${PVE_TOKEN_FULL}=${pve_token_secret}" \
  "https://${PVE_HOST}:8006/api2/json/cluster/resources")"
[[ "${http_code}" == 200 ]] || die "Proxmox token verification failed (HTTP ${http_code})"

kubectl_args=(--kubeconfig "${KUBECONFIG_PATH}" --request-timeout=120s)
[[ -n "${KUBE_SERVER}" ]] && kubectl_args+=(--server="${KUBE_SERVER}")
kubectl "${kubectl_args[@]}" create namespace portal --dry-run=client -o yaml \
  | kubectl "${kubectl_args[@]}" apply -f - >/dev/null
kubectl "${kubectl_args[@]}" -n portal create secret generic homepage-integrations \
  --from-literal=HOMEPAGE_VAR_PROXMOX_USERNAME="${PVE_TOKEN_FULL}" \
  --from-literal=HOMEPAGE_VAR_PROXMOX_TOKEN="${pve_token_secret}" \
  --dry-run=client -o yaml \
  | kubectl "${kubectl_args[@]}" apply -f - >/dev/null

unset pve_token_secret users_json tokens_json
ok "Homepage Proxmox identity is read-only, verified, and present in Kubernetes"
