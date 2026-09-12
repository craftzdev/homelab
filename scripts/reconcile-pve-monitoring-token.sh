#!/usr/bin/env bash
# Create and reconcile the PBS observer's own read-only Proxmox identity.
#
# The observer used to share Homepage's token. Sharing one credential couples
# their rotation and makes the PVE task log unable to tell the two apart, so it
# gets its own identity here, mirroring what the PBS side already does with
# grafana@pbs!monitor.
#
# Values are validated before intentional client-side expansion in SSH command
# strings. Proxmox's CLI is available only on the remote host.
# shellcheck disable=SC2029
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PVE_HOST="${PVE_HOST:-172.16.10.11}"
PVE_USER="${PVE_USER:-grafana@pve}"
PVE_TOKEN_ID="${PVE_TOKEN_ID:-monitor}"
PVE_TOKEN_FULL="${PVE_USER}!${PVE_TOKEN_ID}"
PVE_STORAGE="${PVE_STORAGE:-pbs-gateway}"
PVE_NODE="${PVE_NODE:-sv-proxmox-01}"
KEYCHAIN_SERVICE="${OBSERVER_PVE_KEYCHAIN_SERVICE:-dev.craftz.homelab.grafana-pve-token}"
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
[[ "${PVE_STORAGE}" =~ ^[A-Za-z][A-Za-z0-9._-]+$ ]] \
  || die "PVE_STORAGE is invalid"
[[ "${PVE_NODE}" =~ ^[A-Za-z][A-Za-z0-9.-]+$ ]] \
  || die "PVE_NODE is invalid"

ssh_options=(-o BatchMode=yes -o ConnectTimeout=10)
users_json="$(ssh "${ssh_options[@]}" root@"${PVE_HOST}" \
  'pveum user list --output-format json')"
if ! jq -e --arg user "${PVE_USER}" \
    '.[] | select(.userid == $user)' <<<"${users_json}" >/dev/null; then
  info "Creating passwordless Proxmox service user ${PVE_USER}"
  ssh "${ssh_options[@]}" root@"${PVE_HOST}" \
    "pveum user add '${PVE_USER}' --comment 'Grafana PBS observer, read-only' --enable 1"
fi

# Narrower than Homepage's PVEAuditor on / with propagate: this identity reads
# only the backup schedule, the node task lists and one storage's status.
#
#   /                        Sys.Audit for GET /cluster/backup. propagate 0, so
#                            it grants nothing below the root object itself.
#   /nodes                   Sys.Audit for the vzdump task lists.
#   /storage/<storage>       Datastore.Audit for the PBS storage status.
#
# PVEAuditor is read-only in every case; no write, restore or prune privilege
# is granted anywhere.
grant_acl() {
  local path="$1" propagate="$2" principal="$3" kind="$4"
  ssh "${ssh_options[@]}" root@"${PVE_HOST}" \
    "pveum acl modify '${path}' --${kind} '${principal}' --roles PVEAuditor --propagate ${propagate}"
}

grant_acl / 0 "${PVE_USER}" users
grant_acl /nodes 1 "${PVE_USER}" users
grant_acl "/storage/${PVE_STORAGE}" 1 "${PVE_USER}" users

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
  # A token whose secret is not in Keychain cannot be recovered: Proxmox shows
  # it once. Fail instead of silently minting a second one.
  [[ "${token_exists}" == false ]] || die \
    "Proxmox token exists but its non-retrievable secret is absent from Keychain"
  info "Creating privilege-separated Proxmox API token"
  token_json="$(ssh "${ssh_options[@]}" root@"${PVE_HOST}" \
    "pveum user token add '${PVE_USER}' '${PVE_TOKEN_ID}' --privsep 1 --comment 'Grafana PBS observer, read-only' --output-format json")"
  pve_token_secret="$(jq -er '.value' <<<"${token_json}")"
  security add-generic-password -U \
    -s "${KEYCHAIN_SERVICE}" -a "${PVE_TOKEN_FULL}" \
    -w "${pve_token_secret}" >/dev/null
  unset token_json
fi

# The privilege-separated token needs the same ACLs as its user.
grant_acl / 0 "${PVE_TOKEN_FULL}" tokens
grant_acl /nodes 1 "${PVE_TOKEN_FULL}" tokens
grant_acl "/storage/${PVE_STORAGE}" 1 "${PVE_TOKEN_FULL}" tokens

# Verify the exact credential against every endpoint the exporter calls, so a
# too-narrow ACL surfaces here and not as a silent source failure in Grafana.
probe() {
  curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --cacert "${REPO_ROOT}/kubernetes/infra/homepage/pve-root-ca.pem" \
    -H "Authorization: PVEAPIToken=${PVE_TOKEN_FULL}=${pve_token_secret}" \
    "https://${PVE_HOST}:8006/api2/json$1"
}
while read -r description path; do
  http_code="$(probe "${path}")"
  [[ "${http_code}" == 200 ]] || die \
    "Proxmox token cannot read ${description} (HTTP ${http_code}). The ACL is too narrow for the exporter."
done <<EOF
backup-schedule /cluster/backup
node-tasks /nodes/${PVE_NODE}/tasks?typefilter=vzdump&limit=1
storage-status /nodes/${PVE_NODE}/storage/${PVE_STORAGE}/status
EOF

kubectl_args=(--kubeconfig "${KUBECONFIG_PATH}" --request-timeout=120s)
[[ -n "${KUBE_SERVER}" ]] && kubectl_args+=(--server="${KUBE_SERVER}")
kubectl "${kubectl_args[@]}" create namespace portal --dry-run=client -o yaml \
  | kubectl "${kubectl_args[@]}" apply -f - >/dev/null
# A Secret of its own: reconcile-pbs-monitoring-token.py owns
# pbs-observer-credentials, and applying a full Secret replaces its data.
kubectl "${kubectl_args[@]}" -n portal create secret generic pbs-observer-pve-credentials \
  --from-literal=PVE_TOKEN_ID="${PVE_TOKEN_FULL}" \
  --from-literal=PVE_TOKEN_SECRET="${pve_token_secret}" \
  --dry-run=client -o yaml \
  | kubectl "${kubectl_args[@]}" apply -f - >/dev/null

unset pve_token_secret users_json tokens_json
ok "PBS observer's Proxmox identity is read-only, verified, and present in Kubernetes"
