#!/usr/bin/env bash
# Rebuild all six Talos Kubernetes VMs and the complete GitOps platform.
#
# Default execution is a read-only destroy-plan audit. Destruction requires the
# exact confirmation flag below. The hard-coded VMID allowlist deliberately
# excludes Business Gateway VM 1200.
#
#   ./scripts/rebuild-talos-cluster.sh
#   ./scripts/rebuild-talos-cluster.sh --execute --confirm-destroy-six-k8s-vms
#   BACKUP_DIR=... ./scripts/rebuild-talos-cluster.sh --restore-only
#
# Environment:
#   PBS_BACKUP=0          skip optional whole-VM PBS snapshots (default: 1)
#   GITOPS_REVISION=...   Git revision used by Argo CD (default: current branch)
#   AI_WORKER_REPO=...    ai-business-worker checkout
#   BACKUP_DIR=...        existing recovery set when using --resume-after-backup
#   REGISTRY_DATA_BACKUP=... override registry archive for recovery
#   WORKER_DATA_BACKUP=...   override Worker archive for recovery
#   TAILSCALE_WORKER_FQDN=... canonical Worker MagicDNS name
#   TAILSCALE_ARGOCD_FQDN=... canonical Argo CD MagicDNS name
#   TAILSCALE_GRAFANA_FQDN=... canonical Grafana MagicDNS name
#   TAILSCALE_PORTAL_FQDN=... canonical Homepage MagicDNS name
#   GATEWAY_SMOKE=0      skip the external Gateway-to-Worker test (default: 1)
#   CF_ACCESS_*_SERVICE  macOS Keychain service names for the smoke test
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TOFU_DIR="${REPO_ROOT}/tofu/10-proxmox-talos"
KUBECONFIG_PATH="${REPO_ROOT}/_out/kubeconfig"
REBUILD_KUBECONFIG_PATH="${REPO_ROOT}/_out/rebuild-kubeconfig"
AI_WORKER_REPO="${AI_WORKER_REPO:-${REPO_ROOT}/../ai-business-worker}"
GITOPS_REVISION="${GITOPS_REVISION:-$(git -C "${REPO_ROOT}" branch --show-current)}"
PBS_BACKUP="${PBS_BACKUP:-1}"
GATEWAY_SMOKE="${GATEWAY_SMOKE:-1}"
TAILSCALE_WORKER_FQDN="${TAILSCALE_WORKER_FQDN:-ai-worker-cluster.tailb6c7d.ts.net}"
TAILSCALE_ARGOCD_FQDN="${TAILSCALE_ARGOCD_FQDN:-argocd.tailb6c7d.ts.net}"
TAILSCALE_GRAFANA_FQDN="${TAILSCALE_GRAFANA_FQDN:-grafana.tailb6c7d.ts.net}"
TAILSCALE_PORTAL_FQDN="${TAILSCALE_PORTAL_FQDN:-portal.tailb6c7d.ts.net}"
CF_ACCESS_CLIENT_ID_SERVICE="${CF_ACCESS_CLIENT_ID_SERVICE:-dev.craftz.ai-business-gateway.cloudflare-access-client-id}"
CF_ACCESS_CLIENT_SECRET_SERVICE="${CF_ACCESS_CLIENT_SECRET_SERVICE:-dev.craftz.ai-business-gateway.cloudflare-access-client-secret}"
EXPECTED_VMIDS=(1001 1002 1003 1101 1102 1103)
PVE_HOSTS=(172.16.10.11 172.16.10.12 172.16.10.13)
PVE_VMIDS=("1001 1101" "1002 1102" "1003 1103")
EXECUTE=0
CONFIRMED=0
RESUME_AFTER_BACKUP=0
RESTORE_ONLY=0
TAILNET_RESTORE_TAINTED=0

info() { printf '[INFO] %s\n' "$*"; }
ok() { printf '[OK]   %s\n' "$*"; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute) EXECUTE=1 ;;
    --confirm-destroy-six-k8s-vms) CONFIRMED=1 ;;
    --resume-after-backup) RESUME_AFTER_BACKUP=1 ;;
    --restore-only) RESTORE_ONLY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

for tool in tofu jq kubectl talosctl helm age age-keygen security ssh git curl; do
  command -v "${tool}" >/dev/null || die "required command not found: ${tool}"
done
[[ -d "${AI_WORKER_REPO}/deploy/kubernetes" ]] \
  || die "AI Worker repository not found: ${AI_WORKER_REPO}"
[[ "${PBS_BACKUP}" == 0 || "${PBS_BACKUP}" == 1 ]] \
  || die "PBS_BACKUP must be 0 or 1"
[[ "${GATEWAY_SMOKE}" == 0 || "${GATEWAY_SMOKE}" == 1 ]] \
  || die "GATEWAY_SMOKE must be 0 or 1"

export TF_VAR_state_encryption_passphrase
export TF_VAR_proxmox_api_token
TF_VAR_state_encryption_passphrase="$(security find-generic-password \
  -s dev.craftz.homelab.tofu-state -a talos-k8s -w)"
TF_VAR_proxmox_api_token="$(security find-generic-password \
  -s dev.craftz.proxmox.tofu-token -a 'tofu@pve!provider' -w)"
[[ -n "${TF_VAR_state_encryption_passphrase}" ]] || die "state passphrase is empty"
[[ -n "${TF_VAR_proxmox_api_token}" ]] || die "Proxmox API token is empty"

AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-${HOME}/.config/sops/age/keys.txt}"
[[ -s "${AGE_KEY_FILE}" ]] || die "SOPS age key not found: ${AGE_KEY_FILE}"
AGE_RECIPIENT="$(age-keygen -y "${AGE_KEY_FILE}")"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="${BACKUP_DIR:-${REPO_ROOT}/_out/rebuild-backups/${timestamp}}"
REGISTRY_DATA_BACKUP="${REGISTRY_DATA_BACKUP:-${BACKUP_DIR}/registry-data.tar.age}"
WORKER_DATA_BACKUP="${WORKER_DATA_BACKUP:-${BACKUP_DIR}/ai-worker-data.tar.age}"
mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"
DESTROY_PLAN="${BACKUP_DIR}/destroy.tfplan"
DESTROY_GUARD="${BACKUP_DIR}/destroy-plan-guard.json"

cleanup() {
  if [[ "${TAILNET_RESTORE_TAINTED}" == 1 && -s "${KUBECONFIG_PATH}" ]]; then
    local node
    for node in k8s-worker-1 k8s-worker-2 k8s-worker-3; do
      kubectl --kubeconfig "${KUBECONFIG_PATH}" taint node "${node}" \
        homelab.craftz.dev/tailnet-state-restore:NoSchedule- \
        >/dev/null 2>&1 || true
    done
  fi
  unset TF_VAR_state_encryption_passphrase TF_VAR_proxmox_api_token AGE_RECIPIENT
}
trap cleanup EXIT

verify_gitops_revision() {
  local current_branch remote_head
  current_branch="$(git -C "${REPO_ROOT}" branch --show-current)"
  if [[ "${GITOPS_REVISION}" == "${current_branch}" ]]; then
    git -C "${REPO_ROOT}" diff --quiet \
      || die "tracked files are modified; commit them before rebuilding"
    git -C "${REPO_ROOT}" diff --cached --quiet \
      || die "staged files are not committed; commit them before rebuilding"
    remote_head="$(git -C "${REPO_ROOT}" ls-remote origin \
      "refs/heads/${GITOPS_REVISION}" | awk 'NR == 1 {print $1}')"
    [[ -n "${remote_head}" ]] \
      || die "GitOps branch does not exist on origin: ${GITOPS_REVISION}"
    [[ "${remote_head}" == "$(git -C "${REPO_ROOT}" rev-parse HEAD)" ]] \
      || die "local HEAD is not pushed to origin/${GITOPS_REVISION}"
  fi
  ok "GitOps revision is available remotely: ${GITOPS_REVISION}"
}

plan_and_guard() {
  info "Creating an encrypted destroy plan"
  (
    cd "${TOFU_DIR}"
    # Destroy plans may contain machine credentials. Keep the full plan
    # encrypted and persist only the minimum non-secret fields used by guards.
    tofu plan -destroy -out="${DESTROY_PLAN}" >/dev/null
    tofu show -json "${DESTROY_PLAN}" | jq '{resource_changes: [
      .resource_changes[] | {
        address,
        type,
        actions: .change.actions,
        vm_id: .change.before.vm_id,
        name: .change.before.name
      }
    ]}' >"${DESTROY_GUARD}"
  )
  chmod 600 "${DESTROY_PLAN}" "${DESTROY_GUARD}"

  planned_vmids="$(jq -r '
    .resource_changes[]
    | select(.type == "proxmox_virtual_environment_vm")
    | select(.actions | index("delete"))
    | .vm_id' "${DESTROY_GUARD}" | sort -n | tr '\n' ' ' | sed 's/ $//')"
  [[ "${planned_vmids}" == "${EXPECTED_VMIDS[*]}" ]] || {
    printf 'expected VMIDs: %s\nplanned VMIDs:  %s\n' \
      "${EXPECTED_VMIDS[*]}" "${planned_vmids}" >&2
    die "destroy plan escaped the six-VM allowlist"
  }
  if jq -e '.resource_changes[] | select(
      .type == "proxmox_virtual_environment_vm" and
      (.actions | index("delete")) and
      .vm_id == 1200)' "${DESTROY_GUARD}" >/dev/null; then
    die "Gateway VM 1200 appeared in the destroy plan"
  fi
  ok "Destroy plan is restricted to VMIDs ${EXPECTED_VMIDS[*]} (Gateway 1200 excluded)"
}

encrypt_secret() {
  local namespace=$1 name=$2 output=$3
  kubectl --kubeconfig "${KUBECONFIG_PATH}" -n "${namespace}" get secret "${name}" -o json \
    | jq '{apiVersion:"v1",kind:"Secret",metadata:{name:.metadata.name,namespace:.metadata.namespace},type,data}' \
    | age -r "${AGE_RECIPIENT}" -o "${output}"
  chmod 600 "${output}"
}

backup_tailnet_proxy_state() {
  local parent_type=$1 parent_namespace=$2 parent_name=$3 output=$4
  local secrets_json count
  secrets_json="$(kubectl --kubeconfig "${KUBECONFIG_PATH}" -n tailscale \
    get secrets \
    -l "tailscale.com/managed=true,tailscale.com/parent-resource-type=${parent_type},tailscale.com/parent-resource-ns=${parent_namespace},tailscale.com/parent-resource=${parent_name}" \
    -o json)"
  count="$(jq '.items | length' <<<"${secrets_json}")"
  [[ "${count}" == 1 ]] \
    || die "expected one Tailnet state Secret for ${parent_type}/${parent_namespace}/${parent_name}, found ${count}"

  jq '.items[0] | {
      apiVersion: "v1",
      kind: "Secret",
      type,
      metadata: {
        labels: {
          "tailscale.com/parent-resource-type": .metadata.labels["tailscale.com/parent-resource-type"],
          "tailscale.com/parent-resource-ns": .metadata.labels["tailscale.com/parent-resource-ns"],
          "tailscale.com/parent-resource": .metadata.labels["tailscale.com/parent-resource"]
        }
      },
      data
    }' <<<"${secrets_json}" | age -r "${AGE_RECIPIENT}" -o "${output}"
  chmod 600 "${output}"
}

verify_encrypted_tar() {
  local archive=$1
  age -d -i "${AGE_KEY_FILE}" "${archive}" | tar -tf - >/dev/null
}

backup_directory() {
  local namespace=$1 workload=$2 container=$3 source_dir=$4 output=$5
  local attempt temporary
  temporary="${output}.tmp"
  for attempt in 1 2 3; do
    rm -f "${temporary}"
    if kubectl --kubeconfig "${KUBECONFIG_PATH}" -n "${namespace}" \
        exec "${workload}" -c "${container}" -- \
        tar -C "${source_dir}" --exclude=./lost+found -cf - . \
        | age -r "${AGE_RECIPIENT}" -o "${temporary}" \
      && verify_encrypted_tar "${temporary}"; then
      mv "${temporary}" "${output}"
      chmod 600 "${output}"
      return
    fi
    info "Archive validation failed for ${namespace}/${workload}; retry ${attempt}/3"
  done
  rm -f "${temporary}"
  die "could not create a valid archive for ${namespace}/${workload}"
}

backup_cluster_data() {
  [[ -s "${KUBECONFIG_PATH}" ]] || die "current kubeconfig not found"
  kubectl --kubeconfig "${KUBECONFIG_PATH}" get nodes >/dev/null
  info "Creating encrypted application-level backups"

  backup_directory ai-worker deploy/ai-business-worker worker /data \
    "${BACKUP_DIR}/ai-worker-data.tar.age"
  backup_directory image-registry deploy/registry registry /var/lib/registry \
    "${BACKUP_DIR}/registry-data.tar.age"

  encrypt_secret ai-worker ai-business-worker-runtime \
    "${BACKUP_DIR}/ai-worker-runtime.secret.json.age"
  encrypt_secret ai-worker ai-business-worker-codex-auth \
    "${BACKUP_DIR}/ai-worker-codex-auth.secret.json.age"
  encrypt_secret image-registry registry-tls \
    "${BACKUP_DIR}/registry-tls.secret.json.age"
  encrypt_secret tailscale operator-oauth \
    "${BACKUP_DIR}/tailscale-oauth.secret.json.age"
  encrypt_secret arc-runners arc-github-app \
    "${BACKUP_DIR}/arc-github-app.secret.json.age"
  encrypt_secret argocd ai-business-worker-repository \
    "${BACKUP_DIR}/ai-worker-repository.secret.json.age"
  encrypt_secret tailscale operator \
    "${BACKUP_DIR}/tailscale-operator-state.secret.json.age"
  backup_tailnet_proxy_state ingress ai-worker ai-business-worker \
    "${BACKUP_DIR}/tailscale-worker-state.secret.json.age"
  backup_tailnet_proxy_state svc ai-worker ai-gateway-egress \
    "${BACKUP_DIR}/tailscale-gateway-egress-state.secret.json.age"
  backup_tailnet_proxy_state ingress argocd argocd \
    "${BACKUP_DIR}/tailscale-argocd-state.secret.json.age"
  backup_tailnet_proxy_state ingress monitoring grafana \
    "${BACKUP_DIR}/tailscale-grafana-state.secret.json.age"
  backup_tailnet_proxy_state ingress portal homepage \
    "${BACKUP_DIR}/tailscale-portal-state.secret.json.age"

  kubectl --kubeconfig "${KUBECONFIG_PATH}" -n ai-worker get deploy ai-business-worker \
    -o jsonpath='{.spec.template.spec.containers[0].image}' \
    >"${BACKUP_DIR}/worker-image.txt"
  cp "${TOFU_DIR}/terraform.tfstate" "${BACKUP_DIR}/terraform.tfstate.encrypted"
  chmod 600 "${BACKUP_DIR}/terraform.tfstate.encrypted" "${BACKUP_DIR}/worker-image.txt"
  (cd "${BACKUP_DIR}" && shasum -a 256 ./*.age terraform.tfstate.encrypted worker-image.txt >SHA256SUMS)
  ok "Encrypted recovery set created at ${BACKUP_DIR}"
}

backup_pbs() {
  [[ "${PBS_BACKUP}" == 1 ]] || { info "PBS snapshots skipped"; return; }
  info "Creating six independent PBS snapshots"
  local index host vmid
  for index in 0 1 2; do
    host="${PVE_HOSTS[$index]}"
    for vmid in ${PVE_VMIDS[$index]}; do
      ssh -o BatchMode=yes -o ConnectTimeout=10 root@"${host}" \
        "vzdump ${vmid} --storage pbs-gateway --mode snapshot --compress zstd" \
        >"${BACKUP_DIR}/pbs-vm-${vmid}.log" 2>&1
      ok "PBS snapshot completed: VM ${vmid} on ${host}"
    done
  done
}

verify_recovery_set() {
  required_recovery_files=(
    ai-worker-data.tar.age registry-data.tar.age
    ai-worker-runtime.secret.json.age ai-worker-codex-auth.secret.json.age
    registry-tls.secret.json.age tailscale-oauth.secret.json.age
    arc-github-app.secret.json.age
    terraform.tfstate.encrypted worker-image.txt SHA256SUMS
  )
  for recovery_file in "${required_recovery_files[@]}"; do
    [[ -s "${BACKUP_DIR}/${recovery_file}" ]] \
      || die "recovery set is incomplete: ${recovery_file}"
  done
  (cd "${BACKUP_DIR}" && shasum -a 256 -c SHA256SUMS)
  [[ -s "${WORKER_DATA_BACKUP}" ]] \
    || die "Worker data archive not found: ${WORKER_DATA_BACKUP}"
  [[ -s "${REGISTRY_DATA_BACKUP}" ]] \
    || die "registry data archive not found: ${REGISTRY_DATA_BACKUP}"
  verify_encrypted_tar "${WORKER_DATA_BACKUP}" \
    || die "Worker data archive is not a complete tar stream"
  verify_encrypted_tar "${REGISTRY_DATA_BACKUP}" \
    || die "registry data archive is not a complete tar stream"
  local tailnet_state_count=0 tailnet_state_file
  for tailnet_state_file in \
    tailscale-operator-state.secret.json.age \
    tailscale-worker-state.secret.json.age \
    tailscale-gateway-egress-state.secret.json.age \
    tailscale-argocd-state.secret.json.age \
    tailscale-grafana-state.secret.json.age \
    tailscale-portal-state.secret.json.age; do
    [[ -s "${BACKUP_DIR}/${tailnet_state_file}" ]] && tailnet_state_count=$((tailnet_state_count + 1))
  done
  # Older recovery sets can predate individual management UI ingresses.
  # Current recovery sets contain the Operator, two Worker proxies, and all
  # three management UIs (six state files in total).
  [[ "${tailnet_state_count}" == 0 || "${tailnet_state_count}" == 3 \
      || "${tailnet_state_count}" == 4 || "${tailnet_state_count}" == 5 \
      || "${tailnet_state_count}" == 6 ]] \
    || die "Tailnet state backup is incomplete"
  if [[ "${tailnet_state_count}" == 3 ]]; then
    for tailnet_state_file in \
      tailscale-operator-state.secret.json.age \
      tailscale-worker-state.secret.json.age \
      tailscale-gateway-egress-state.secret.json.age; do
      [[ -s "${BACKUP_DIR}/${tailnet_state_file}" ]] \
        || die "legacy Tailnet state backup is incomplete: ${tailnet_state_file}"
    done
  fi
  ok "Existing encrypted recovery set verified"
}

remove_stale_tailnet_cluster_devices() {
  local oauth_json client_id client_secret token_json access_token devices_json
  local device_ids device_count device_id delete_code tag selector maximum
  local -a cleanup_tags

  if [[ -s "${BACKUP_DIR}/tailscale-worker-state.secret.json.age" \
      && -s "${BACKUP_DIR}/tailscale-argocd-state.secret.json.age" \
      && -s "${BACKUP_DIR}/tailscale-grafana-state.secret.json.age" \
      && -s "${BACKUP_DIR}/tailscale-portal-state.secret.json.age" ]]; then
    info "Preserving Tailnet identities and TLS certificate cache for recreation"
    return
  fi

  if [[ -s "${BACKUP_DIR}/tailscale-worker-state.secret.json.age" ]]; then
    # Legacy recovery sets preserve the Worker and Operator but predate the
    # Argo CD proxy. Remove only a later stale Argo CD device so the canonical
    # hostname can be reclaimed without affecting the preserved identities.
    cleanup_tags=(tag:argocd)
    info "Preserving legacy Tailnet states and removing any stale Argo CD identity"
  else
    cleanup_tags=(tag:ai-worker-trusted tag:k8s-operator tag:argocd)
    info "Removing previous Tailnet identities before recreation (legacy recovery set)"
  fi

  oauth_json="$(age -d -i "${AGE_KEY_FILE}" \
    "${BACKUP_DIR}/tailscale-oauth.secret.json.age")"
  client_id="$(jq -er '.data.client_id | @base64d' <<<"${oauth_json}")"
  client_secret="$(jq -er '.data.client_secret | @base64d' <<<"${oauth_json}")"

  for tag in "${cleanup_tags[@]}"; do
    token_json="$(curl -fsS -u "${client_id}:${client_secret}" \
      --data-urlencode 'grant_type=client_credentials' \
      --data-urlencode 'scope=devices:core' \
      --data-urlencode "tags=${tag}" \
      https://api.tailscale.com/api/v2/oauth/token)"
    access_token="$(jq -er '.access_token' <<<"${token_json}")"
    devices_json="$(curl -fsS -H "Authorization: Bearer ${access_token}" \
      https://api.tailscale.com/api/v2/tailnet/-/devices)"
    if [[ "${tag}" == tag:ai-worker-trusted ]]; then
      # shellcheck disable=SC2016 # jq, not the shell, expands $worker_fqdn.
      selector='(.name == $worker_fqdn or .hostname == "ai-worker-ai-gateway-egress")'
      maximum=2
    elif [[ "${tag}" == tag:argocd ]]; then
      # Management UIs intentionally share this admin-only tag. Remove
      # only identities absent from the selected recovery set.
      selector='false'
      maximum=0
      if [[ ! -s "${BACKUP_DIR}/tailscale-argocd-state.secret.json.age" ]]; then
        selector="${selector} or (.name == \$argocd_fqdn or .hostname == \"argocd\")"
        maximum=$((maximum + 1))
      fi
      if [[ ! -s "${BACKUP_DIR}/tailscale-grafana-state.secret.json.age" ]]; then
        selector="${selector} or (.name == \$grafana_fqdn or .hostname == \"grafana\")"
        maximum=$((maximum + 1))
      fi
      if [[ ! -s "${BACKUP_DIR}/tailscale-portal-state.secret.json.age" ]]; then
        selector="${selector} or (.name == \$portal_fqdn or .hostname == \"portal\")"
        maximum=$((maximum + 1))
      fi
    else
      selector='(.hostname == "tailscale-operator")'
      maximum=1
    fi
    device_ids="$(jq -r --arg worker_fqdn "${TAILSCALE_WORKER_FQDN}" \
      --arg argocd_fqdn "${TAILSCALE_ARGOCD_FQDN}" \
      --arg grafana_fqdn "${TAILSCALE_GRAFANA_FQDN}" \
      --arg portal_fqdn "${TAILSCALE_PORTAL_FQDN}" \
      --arg tag "${tag}" "[.devices[]
        | select(${selector})
        | select((.tags // []) | index(\$tag))
        | .id][]" <<<"${devices_json}")"
    device_count="$(sed '/^$/d' <<<"${device_ids}" | wc -l | tr -d ' ')"
    [[ "${device_count}" -le "${maximum}" ]] \
      || die "too many Tailnet devices matched ${tag}; refusing cleanup"
    while IFS= read -r device_id; do
      [[ -n "${device_id}" ]] || continue
      delete_code="$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE \
        -H "Authorization: Bearer ${access_token}" \
        "https://api.tailscale.com/api/v2/device/${device_id}")"
      [[ "${delete_code}" == 200 || "${delete_code}" == 204 ]] \
        || die "Tailnet device cleanup failed with HTTP ${delete_code}"
    done <<<"${device_ids}"
    info "Removed ${device_count} previous ${tag} device(s)"
  done
  ok "Previous Tailnet cluster identities removed"
  unset oauth_json client_id client_secret token_json access_token devices_json
}

delete_tailnet_device_id() {
  local device_id=$1 tag=$2 oauth_json client_id client_secret token_json access_token delete_code
  [[ -n "${device_id}" ]] || return
  oauth_json="$(age -d -i "${AGE_KEY_FILE}" \
    "${BACKUP_DIR}/tailscale-oauth.secret.json.age")"
  client_id="$(jq -er '.data.client_id | @base64d' <<<"${oauth_json}")"
  client_secret="$(jq -er '.data.client_secret | @base64d' <<<"${oauth_json}")"
  token_json="$(curl -fsS -u "${client_id}:${client_secret}" \
    --data-urlencode 'grant_type=client_credentials' \
    --data-urlencode 'scope=devices:core' \
    --data-urlencode "tags=${tag}" \
    https://api.tailscale.com/api/v2/oauth/token)"
  access_token="$(jq -er '.access_token' <<<"${token_json}")"
  delete_code="$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE \
    -H "Authorization: Bearer ${access_token}" \
    "https://api.tailscale.com/api/v2/device/${device_id}")"
  [[ "${delete_code}" == 200 || "${delete_code}" == 204 ]] \
    || die "transient Tailnet device cleanup failed with HTTP ${delete_code}"
  unset oauth_json client_id client_secret token_json access_token
}

restore_tailnet_proxy_state() {
  local input=$1 parent_type=$2 parent_namespace=$3 parent_name=$4 device_tag=$5
  local selector secrets_json count secret_name attempt
  local old_json old_device_id new_device_id identity_patch
  [[ -s "${input}" ]] || {
    info "Tailnet state backup is absent; a new identity and certificate will be issued"
    return
  }

  selector="tailscale.com/managed=true,tailscale.com/parent-resource-type=${parent_type},tailscale.com/parent-resource-ns=${parent_namespace},tailscale.com/parent-resource=${parent_name}"
  for attempt in $(seq 1 120); do
    secrets_json="$(kubectl --kubeconfig "${KUBECONFIG_PATH}" --request-timeout=15s \
      -n tailscale get secrets -l "${selector}" -o json 2>/dev/null || true)"
    if [[ -n "${secrets_json}" ]]; then
      count="$(jq -er '.items | length' <<<"${secrets_json}" 2>/dev/null || echo 0)"
    else
      count=0
    fi
    [[ "${count}" == 1 ]] && break
    if (( attempt % 12 == 0 )); then
      info "Still waiting for the generated Tailnet state Secret for ${parent_type}/${parent_namespace}/${parent_name} (found ${count})"
    fi
    sleep 5
  done
  [[ "${count:-0}" == 1 ]] \
    || die "generated Tailnet state Secret was not created for ${parent_type}/${parent_namespace}/${parent_name}"

  secret_name="$(jq -r '.items[0].metadata.name' <<<"${secrets_json}")"
  new_device_id="$(jq -r '.items[0].data.device_id // "" | @base64d' <<<"${secrets_json}")"
  old_json="$(age -d -i "${AGE_KEY_FILE}" "${input}")"
  old_device_id="$(jq -r '.data.device_id // "" | @base64d' <<<"${old_json}")"

  # The generated cap/serve data contains the new Pod UID and ClusterIP, so only
  # identity, profile, ACME account, and cached certificate material is restored.
  identity_patch="$(jq -c '{data: (.data | with_entries(select(
      .key == "_current-profile" or
      .key == "_machinekey" or
      .key == "_profiles" or
      (.key | startswith("profile-")) or
      .key == "acme-account.key.pem" or
      (.key | startswith("cert-")) or
      (.key | endswith(".crt")) or
      (.key | endswith(".key")) or
      (.key | endswith(".pem"))
    )))}' <<<"${old_json}")"
  [[ "$(jq '.data | length' <<<"${identity_patch}")" -gt 0 ]] \
    || die "Tailnet state backup contains no restorable identity data"

  if [[ -n "${new_device_id}" && "${new_device_id}" != "${old_device_id}" ]]; then
    delete_tailnet_device_id "${new_device_id}" "${device_tag}"
  fi
  kubectl -n tailscale patch secret "${secret_name}" --type=merge \
    -p "${identity_patch}" >/dev/null

  ok "Restored Tailnet identity state for ${parent_type}/${parent_namespace}/${parent_name}"
}

taint_tailnet_proxy_workers() {
  local node
  for node in k8s-worker-1 k8s-worker-2 k8s-worker-3; do
    kubectl taint node "${node}" \
      homelab.craftz.dev/tailnet-state-restore=true:NoSchedule --overwrite
  done
  TAILNET_RESTORE_TAINTED=1
}

untaint_tailnet_proxy_workers() {
  local node
  for node in k8s-worker-1 k8s-worker-2 k8s-worker-3; do
    kubectl taint node "${node}" \
      homelab.craftz.dev/tailnet-state-restore:NoSchedule-
  done
  TAILNET_RESTORE_TAINTED=0
}

restart_tailnet_proxy() {
  local parent_type=$1 parent_namespace=$2 parent_name=$3 selector statefulsets_json statefulset_name
  selector="tailscale.com/managed=true,tailscale.com/parent-resource-type=${parent_type},tailscale.com/parent-resource-ns=${parent_namespace},tailscale.com/parent-resource=${parent_name}"
  statefulsets_json="$(kubectl -n tailscale get statefulsets -l "${selector}" -o json)"
  [[ "$(jq '.items | length' <<<"${statefulsets_json}")" == 1 ]] \
    || die "expected one Tailnet StatefulSet for ${parent_type}/${parent_namespace}/${parent_name}"
  statefulset_name="$(jq -r '.items[0].metadata.name' <<<"${statefulsets_json}")"
  kubectl -n tailscale rollout restart "statefulset/${statefulset_name}" >/dev/null
  kubectl -n tailscale rollout status "statefulset/${statefulset_name}" --timeout=10m
}

restore_secret() {
  local input=$1
  age -d -i "${AGE_KEY_FILE}" "${input}" \
    | kubectl --kubeconfig "${KUBECONFIG_PATH}" apply -f - >/dev/null
}

wait_for_control_plane_stability() {
  local deadline stable_checks=0 members
  deadline=$((SECONDS + 600))
  export TALOSCONFIG="${REPO_ROOT}/_out/talosconfig"
  info "Waiting for a stable three-voter etcd control plane"

  while (( SECONDS < deadline )); do
    members="$(talosctl --nodes 172.16.40.11 etcd members 2>/dev/null || true)"
    if awk 'NR > 1 && $NF == "false" { voters++ } END { exit !(voters == 3) }' \
        <<<"${members}" \
      && [[ "$(kubectl --request-timeout=10s get --raw=/readyz 2>/dev/null || true)" == ok ]]; then
      stable_checks=$((stable_checks + 1))
      if (( stable_checks >= 6 )); then
        ok "etcd has three voters and the API remained ready for 30 seconds"
        return
      fi
    else
      stable_checks=0
    fi
    sleep 5
  done

  printf '%s\n' "${members}" >&2
  die "the three-voter control plane did not stabilize within ten minutes"
}

pin_rebuild_api_endpoint() {
  local source_config="${REPO_ROOT}/_out/kubeconfig"
  local context cluster endpoint attempt
  [[ -s "${source_config}" ]] || die "generated kubeconfig not found: ${source_config}"

  # The Kubernetes VIP uses L2 ownership. Across a Tailscale subnet router an
  # ownership change can retain a stale ARP entry for several minutes, even
  # though every apiserver is healthy. Use one direct control-plane endpoint
  # for this finite rebuild transaction; the generated VIP kubeconfig remains
  # untouched for normal cluster administration.
  cp "${source_config}" "${REBUILD_KUBECONFIG_PATH}"
  chmod 600 "${REBUILD_KUBECONFIG_PATH}"
  context="$(kubectl --kubeconfig "${REBUILD_KUBECONFIG_PATH}" config current-context)"
  cluster="$(kubectl --kubeconfig "${REBUILD_KUBECONFIG_PATH}" config view -o json \
    | jq -r --arg context "${context}" \
      '.contexts[] | select(.name == $context) | .context.cluster')"
  [[ -n "${cluster}" && "${cluster}" != null ]] \
    || die "could not resolve the active kubeconfig cluster"

  for attempt in $(seq 1 60); do
    for endpoint in 172.16.40.13 172.16.40.12 172.16.40.11; do
      if [[ "$(kubectl --kubeconfig "${source_config}" \
          --server="https://${endpoint}:6443" --request-timeout=10s \
          get --raw=/readyz 2>/dev/null || true)" == ok ]]; then
        kubectl --kubeconfig "${REBUILD_KUBECONFIG_PATH}" config set-cluster \
          "${cluster}" --server="https://${endpoint}:6443" >/dev/null
        KUBECONFIG_PATH="${REBUILD_KUBECONFIG_PATH}"
        ok "Rebuild API traffic pinned to healthy control plane ${endpoint}"
        return
      fi
    done
    sleep 5
  done
  die "no direct control-plane API endpoint became ready within five minutes"
}

wait_for_all_pods() {
  local deadline stable_checks=0 pods_json
  deadline=$((SECONDS + 1200))
  info "Waiting for every cluster Pod container to become ready"

  while (( SECONDS < deadline )); do
    if pods_json="$(kubectl --request-timeout=15s get pods -A -o json 2>/dev/null)" \
      && jq -e '
        all(.items[];
          .metadata.deletionTimestamp != null or
          .status.phase == "Succeeded" or
          (.status.phase == "Failed" and
            .status.reason == "Terminated" and
            (.status.message // "" | contains("imminent node shutdown"))) or
          (.status.phase == "Running" and
            (.status.containerStatuses // [] | length) > 0 and
            all((.status.containerStatuses // [])[]; .ready == true)))
      ' <<<"${pods_json}" >/dev/null; then
      stable_checks=$((stable_checks + 1))
      if (( stable_checks >= 3 )); then
        ok "Every Pod container remained ready for 15 seconds"
        return
      fi
    else
      stable_checks=0
    fi
    sleep 5
  done

  kubectl --request-timeout=15s get pods -A >&2 || true
  die "cluster Pods did not converge within twenty minutes"
}

restore_platform() {
  pin_rebuild_api_endpoint
  export KUBECONFIG="${KUBECONFIG_PATH}"
  info "Waiting for the pinned kube-apiserver endpoint"
  for _ in $(seq 1 60); do
    if kubectl version -o json >/dev/null 2>&1; then
      break
    fi
    sleep 5
  done
  kubectl version -o json >/dev/null 2>&1 \
    || die "kube-apiserver did not become ready within five minutes"
  wait_for_control_plane_stability
  info "Bootstrapping Cilium and the dedicated worker plane"
  "${SCRIPT_DIR}/bootstrap-cluster.sh"

  local node
  for node in k8s-1 k8s-2 k8s-3; do
    kubectl taint node "${node}" node-role.kubernetes.io/control-plane=:NoSchedule --overwrite
  done
  for node in k8s-worker-1 k8s-worker-2 k8s-worker-3; do
    kubectl label node "${node}" homelab.craftz.dev/workload-plane=true \
      node.longhorn.io/create-default-disk=true --overwrite
  done

  "${SCRIPT_DIR}/bootstrap-cluster-secrets.sh"
  restore_secret "${BACKUP_DIR}/registry-tls.secret.json.age"
  restore_secret "${BACKUP_DIR}/tailscale-oauth.secret.json.age"
  restore_secret "${BACKUP_DIR}/arc-github-app.secret.json.age"
  if [[ -s "${BACKUP_DIR}/ai-worker-repository.secret.json.age" ]]; then
    restore_secret "${BACKUP_DIR}/ai-worker-repository.secret.json.age"
  fi
  if [[ -s "${BACKUP_DIR}/tailscale-operator-state.secret.json.age" ]]; then
    restore_secret "${BACKUP_DIR}/tailscale-operator-state.secret.json.age"
  fi

  info "Bootstrapping Argo CD at revision ${GITOPS_REVISION}"
  GITOPS_REVISION="${GITOPS_REVISION}" "${SCRIPT_DIR}/bootstrap-argocd.sh"
  # Worker data and runtime Secrets are restored below. Reconcile all
  # dependencies now and enforce Worker GitOps convergence after that restore.
  DEFER_AI_WORKER=1 "${SCRIPT_DIR}/reconcile-cluster-platform.sh"
  kubectl -n tailscale rollout status deployment/operator --timeout=10m
  kubectl wait --for=jsonpath='{.status.conditions[?(@.type=="ProxyClassReady")].status}'=True \
    proxyclass/restricted-userspace proxyclass/kernel-egress --timeout=5m
  if [[ -s "${BACKUP_DIR}/tailscale-argocd-state.secret.json.age" ]]; then
    # GitOps creates the generated state Secret first. Replace its temporary
    # identity with the encrypted pre-rebuild state, then restart the proxy to
    # retain the canonical MagicDNS name and cached TLS material.
    restore_tailnet_proxy_state \
      "${BACKUP_DIR}/tailscale-argocd-state.secret.json.age" \
      ingress argocd argocd tag:argocd
    restart_tailnet_proxy ingress argocd argocd
  fi
  if [[ -s "${BACKUP_DIR}/tailscale-grafana-state.secret.json.age" ]]; then
    restore_tailnet_proxy_state \
      "${BACKUP_DIR}/tailscale-grafana-state.secret.json.age" \
      ingress monitoring grafana tag:argocd
    restart_tailnet_proxy ingress monitoring grafana
  fi
  if [[ -s "${BACKUP_DIR}/tailscale-portal-state.secret.json.age" ]]; then
    restore_tailnet_proxy_state \
      "${BACKUP_DIR}/tailscale-portal-state.secret.json.age" \
      ingress portal homepage tag:argocd
    restart_tailnet_proxy ingress portal homepage
  fi
  kubectl -n argocd wait \
    --for=jsonpath='{.status.loadBalancer.ingress[0].hostname}'="${TAILSCALE_ARGOCD_FQDN}" \
    ingress/argocd --timeout=10m
  kubectl -n monitoring wait \
    --for=jsonpath='{.status.loadBalancer.ingress[0].hostname}'="${TAILSCALE_GRAFANA_FQDN}" \
    ingress/grafana --timeout=10m
  kubectl -n portal wait \
    --for=jsonpath='{.status.loadBalancer.ingress[0].hostname}'="${TAILSCALE_PORTAL_FQDN}" \
    ingress/homepage --timeout=10m
  "${SCRIPT_DIR}/configure-tailscale-dns.sh"
  kubectl -n longhorn-system rollout status daemonset/longhorn-manager --timeout=15m
  "${SCRIPT_DIR}/reconcile-longhorn-worker-plane.sh"

  info "Restoring the internal registry"
  kubectl -n image-registry rollout status deployment/registry --timeout=15m
  age -d -i "${AGE_KEY_FILE}" "${REGISTRY_DATA_BACKUP}" \
    | kubectl -n image-registry exec -i deploy/registry -- tar -C /var/lib/registry -xf -
  # Distribution reads repository metadata from the filesystem for each API
  # request, so a restart is unnecessary. Restarting a one-replica RWO
  # deployment can move the Pod to another worker and needlessly wait for a
  # Longhorn detach/attach cycle during an otherwise idempotent restore.

  info "Restoring AI Worker secrets and persistent data"
  kubectl apply -f "${AI_WORKER_REPO}/deploy/kubernetes/namespace.yaml" >/dev/null
  restore_secret "${BACKUP_DIR}/ai-worker-runtime.secret.json.age"
  restore_secret "${BACKUP_DIR}/ai-worker-codex-auth.secret.json.age"
  kubectl apply -f "${AI_WORKER_REPO}/deploy/kubernetes/config.yaml" >/dev/null
  kubectl apply -f "${AI_WORKER_REPO}/deploy/kubernetes/pvc.yaml" >/dev/null
  kubectl wait -n ai-worker --for=jsonpath='{.status.phase}'=Bound \
    pvc/ai-business-worker-data --timeout=10m

  # A resumed restore may already have a Worker using this ReadWriteOnce PVC.
  # Stop it before mounting the volume in the restore Pod, and remove a restore
  # Pod left by an interrupted attempt.
  if kubectl -n ai-worker get deployment ai-business-worker >/dev/null 2>&1; then
    kubectl -n ai-worker scale deployment ai-business-worker --replicas=0 >/dev/null
    kubectl -n ai-worker rollout status deployment/ai-business-worker --timeout=5m
  fi
  kubectl -n ai-worker delete pod rebuild-data-restore \
    --ignore-not-found --wait=true --timeout=5m >/dev/null

  worker_image="$(cat "${BACKUP_DIR}/worker-image.txt")"
  cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: rebuild-data-restore
  namespace: ai-worker
spec:
  restartPolicy: Never
  nodeSelector:
    homelab.craftz.dev/workload-plane: "true"
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: restore
      image: ${worker_image}
      command: ["/bin/sh", "-c", "sleep 3600"]
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
        readOnlyRootFilesystem: true
      volumeMounts:
        - name: data
          mountPath: /data
        - name: tmp
          mountPath: /tmp
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: ai-business-worker-data
    - name: tmp
      emptyDir: {}
EOF
  kubectl wait -n ai-worker --for=condition=Ready pod/rebuild-data-restore --timeout=10m
  age -d -i "${AGE_KEY_FILE}" "${WORKER_DATA_BACKUP}" \
    | kubectl -n ai-worker exec -i rebuild-data-restore -- \
      sh -c 'mkdir -p /tmp/restore && tar -C /tmp/restore --exclude=./lost+found -xf - && cp -R /tmp/restore/. /data/'
  kubectl -n ai-worker exec rebuild-data-restore -- \
    sh -c 'test -f /data/worker.db && test -d /data/jobs'
  kubectl -n ai-worker delete pod rebuild-data-restore --wait=true >/dev/null

  # Prevent newly generated Tailscale proxy Pods from starting with an empty
  # StateStore and requesting another ACME certificate. The operator may create
  # the Secret/StatefulSet while workers are NoSchedule; identity state is then
  # injected before the first proxy process starts.
  if [[ -s "${BACKUP_DIR}/tailscale-worker-state.secret.json.age" ]]; then
    taint_tailnet_proxy_workers
  fi
  kubectl apply -k "${AI_WORKER_REPO}/deploy/kubernetes"

  if [[ -s "${BACKUP_DIR}/tailscale-worker-state.secret.json.age" ]]; then
    restore_tailnet_proxy_state \
      "${BACKUP_DIR}/tailscale-worker-state.secret.json.age" \
      ingress ai-worker ai-business-worker tag:ai-worker-trusted
    restore_tailnet_proxy_state \
      "${BACKUP_DIR}/tailscale-gateway-egress-state.secret.json.age" \
      svc ai-worker ai-gateway-egress tag:ai-worker-trusted
    untaint_tailnet_proxy_workers
    restart_tailnet_proxy ingress ai-worker ai-business-worker
    restart_tailnet_proxy svc ai-worker ai-gateway-egress
  fi

  kubectl -n ai-worker rollout status deployment/ai-business-worker --timeout=15m
  kubectl -n tailscale wait --for=condition=Ready pod \
    -l tailscale.com/parent-resource=ai-business-worker --timeout=10m
  kubectl -n ai-worker wait \
    --for=jsonpath='{.status.loadBalancer.ingress[0].hostname}'="${TAILSCALE_WORKER_FQDN}" \
    ingress/ai-business-worker --timeout=10m

  # The bootstrap apply above only stages the PVC/data recovery. Argo CD is the
  # sole steady-state manager and must adopt every Worker resource without drift.
  "${SCRIPT_DIR}/reconcile-cluster-platform.sh"

  # DNSConfig records are populated asynchronously after both proxy Pods are
  # ready. Avoid caching a transient NXDOMAIN as a restore failure.
  local resolved=0
  for _ in $(seq 1 24); do
    if kubectl -n ai-worker exec deployment/ai-business-worker -c worker -- \
        python -c "import socket; socket.getaddrinfo('ai-gateway-01.${TAILSCALE_WORKER_FQDN#*.}', 443)" \
        >/dev/null 2>&1; then
      resolved=1
      break
    fi
    sleep 5
  done
  [[ "${resolved}" == 1 ]] || die "Worker could not resolve the Gateway Tailnet name"
}

verify_rebuild() {
  export KUBECONFIG="${KUBECONFIG_PATH}"
  info "Verifying cluster invariants"
  kubectl wait --for=condition=Ready nodes --all --timeout=5m
  [[ "$(kubectl get nodes --no-headers | wc -l | tr -d ' ')" == 6 ]] \
    || die "cluster does not contain exactly six nodes"
  for node in k8s-1 k8s-2 k8s-3; do
    kubectl get node "${node}" -o json | jq -e \
      '.spec.taints[] | select(.key=="node-role.kubernetes.io/control-plane" and .effect=="NoSchedule")' \
      >/dev/null || die "control-plane taint missing on ${node}"
  done
  for node in k8s-worker-1 k8s-worker-2 k8s-worker-3; do
    [[ "$(kubectl get node "${node}" -o jsonpath='{.metadata.labels.homelab\.craftz\.dev/workload-plane}')" == true ]] \
      || die "worker label missing on ${node}"
  done
  kubectl -n longhorn-system get volumes.longhorn.io -o json | jq -e \
    'all(.items[]; .status.robustness == "healthy")' >/dev/null \
    || die "a Longhorn volume is not healthy"
  if kubectl -n longhorn-system get replicas.longhorn.io -o json | jq -e \
      '.items[] | select(.spec.nodeID | test("^k8s-[123]$"))' >/dev/null; then
    die "a Longhorn replica is scheduled on the control plane"
  fi
  wait_for_all_pods
  # An early API interruption can leave a stale Progressing health value even
  # after all workloads recover. Force one final dependency-aware comparison
  # only after every Pod is ready, then enforce the fail-closed invariant.
  "${SCRIPT_DIR}/reconcile-cluster-platform.sh"
  kubectl -n argocd get applications -o json | jq -e '
    all(.items[];
      .status.sync.status == "Synced" and .status.health.status == "Healthy")
  ' >/dev/null || {
    kubectl -n argocd get applications >&2
    die "an Argo CD Application is not Synced/Healthy"
  }
  curl -fsS --retry 12 --retry-all-errors --retry-delay 5 \
    "https://${TAILSCALE_WORKER_FQDN}/health" >/dev/null
  curl -fsS --retry 12 --retry-all-errors --retry-delay 5 \
    "https://${TAILSCALE_ARGOCD_FQDN}/" >/dev/null
  curl -fsS --retry 12 --retry-all-errors --retry-delay 5 \
    "https://${TAILSCALE_GRAFANA_FQDN}/api/health" >/dev/null
  curl -fsS --retry 12 --retry-all-errors --retry-delay 5 \
    "https://${TAILSCALE_PORTAL_FQDN}/api/healthcheck" >/dev/null
  ok "Six-node cluster, GitOps platform, Tailnet Worker, and management UIs verified"
}

verify_gateway_worker_path() {
  [[ "${GATEWAY_SMOKE}" == 1 ]] || {
    info "Gateway-to-Worker smoke test skipped"
    return
  }

  local client_id client_secret attempt
  info "Running the authenticated Cloudflare/Gateway/Worker smoke test"

  client_id="$(security find-generic-password \
    -s "${CF_ACCESS_CLIENT_ID_SERVICE}" -a craftz -w)"
  client_secret="$(security find-generic-password \
    -s "${CF_ACCESS_CLIENT_SECRET_SERVICE}" -a craftz -w)"
  [[ -n "${client_id}" && -n "${client_secret}" ]] \
    || die "Cloudflare Access service token is empty"

  # Pass the short-lived in-memory values over SSH stdin, not command-line
  # arguments or files. The Gateway smoke script loads its application tokens
  # locally and verifies the complete callback lifecycle.
  for attempt in $(seq 1 6); do
    if printf '%s\n%s\n' "${client_id}" "${client_secret}" \
        | ssh -o BatchMode=yes -o ConnectTimeout=10 craftz@172.16.40.30 '
            IFS= read -r CF_ACCESS_CLIENT_ID
            IFS= read -r CF_ACCESS_CLIENT_SECRET
            export CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET
            sudo -n --preserve-env=CF_ACCESS_CLIENT_ID,CF_ACCESS_CLIENT_SECRET \
              /opt/ai-business-gateway/scripts/smoke-test.sh
          '; then
      unset client_id client_secret
      ok "Cloudflare Access, Gateway dispatch, Worker execution, and callback verified"
      return
    fi
    info "Gateway smoke attempt ${attempt}/6 failed; retrying in 10 seconds"
    sleep 10
  done
  unset client_id client_secret
  die "authenticated Cloudflare/Gateway/Worker smoke test failed after six attempts"
}

if [[ "${RESTORE_ONLY}" == 1 ]]; then
  verify_gitops_revision
  verify_recovery_set
  restore_platform
  verify_rebuild
  verify_gateway_worker_path
  printf '\nRestore completed successfully.\nRecovery set: %s\nGitOps revision: %s\n' \
    "${BACKUP_DIR}" "${GITOPS_REVISION}"
  exit 0
fi

verify_gitops_revision
plan_and_guard
if [[ "${EXECUTE}" != 1 ]]; then
  info "Read-only audit complete. Re-run with both destructive flags to rebuild."
  exit 0
fi
[[ "${CONFIRMED}" == 1 ]] \
  || die "--execute also requires --confirm-destroy-six-k8s-vms"

if [[ "${RESUME_AFTER_BACKUP}" == 1 ]]; then
  verify_recovery_set
else
  backup_cluster_data
  backup_pbs
fi

info "Destroying the six allowlisted Kubernetes VMs using the audited plan"
(cd "${TOFU_DIR}" && tofu apply -auto-approve "${DESTROY_PLAN}")
ok "Six Kubernetes VMs destroyed"
remove_stale_tailnet_cluster_devices

info "Recreating all OpenTofu and Talos resources"
(cd "${TOFU_DIR}" && tofu apply -auto-approve)
restore_platform
verify_rebuild
verify_gateway_worker_path

printf '\nRebuild completed successfully.\nRecovery set: %s\nGitOps revision: %s\n' \
  "${BACKUP_DIR}" "${GITOPS_REVISION}"
