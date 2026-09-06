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
#   GATEWAY_SMOKE=0      skip the external Gateway-to-Worker test (default: 1)
#   CF_ACCESS_*_SERVICE  macOS Keychain service names for the smoke test
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TOFU_DIR="${REPO_ROOT}/tofu/10-proxmox-talos"
KUBECONFIG_PATH="${REPO_ROOT}/_out/kubeconfig"
AI_WORKER_REPO="${AI_WORKER_REPO:-${REPO_ROOT}/../ai-business-worker}"
GITOPS_REVISION="${GITOPS_REVISION:-$(git -C "${REPO_ROOT}" branch --show-current)}"
PBS_BACKUP="${PBS_BACKUP:-1}"
GATEWAY_SMOKE="${GATEWAY_SMOKE:-1}"
TAILSCALE_WORKER_FQDN="${TAILSCALE_WORKER_FQDN:-ai-worker-k8s.tailb6c7d.ts.net}"
CF_ACCESS_CLIENT_ID_SERVICE="${CF_ACCESS_CLIENT_ID_SERVICE:-dev.craftz.ai-business-gateway.cloudflare-access-client-id}"
CF_ACCESS_CLIENT_SECRET_SERVICE="${CF_ACCESS_CLIENT_SECRET_SERVICE:-dev.craftz.ai-business-gateway.cloudflare-access-client-secret}"
EXPECTED_VMIDS=(1001 1002 1003 1101 1102 1103)
EXPECTED_NAMES=(k8s-1 k8s-2 k8s-3 k8s-worker-1 k8s-worker-2 k8s-worker-3)
PVE_HOSTS=(172.16.10.11 172.16.10.12 172.16.10.13)
PVE_VMIDS=("1001 1101" "1002 1102" "1003 1103")
EXECUTE=0
CONFIRMED=0
RESUME_AFTER_BACKUP=0
RESTORE_ONLY=0

info() { printf '[INFO] %s\n' "$*"; }
ok() { printf '[OK]   %s\n' "$*"; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
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

  kubectl --kubeconfig "${KUBECONFIG_PATH}" -n ai-worker get deploy ai-business-worker \
    -o jsonpath='{.spec.template.spec.containers[0].image}' \
    >"${BACKUP_DIR}/worker-image.txt"
  cp "${TOFU_DIR}/terraform.tfstate" "${BACKUP_DIR}/terraform.tfstate.encrypted"
  chmod 600 "${BACKUP_DIR}/terraform.tfstate.encrypted" "${BACKUP_DIR}/worker-image.txt"
  (cd "${BACKUP_DIR}" && shasum -a 256 *.age terraform.tfstate.encrypted worker-image.txt >SHA256SUMS)
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
  ok "Existing encrypted recovery set verified"
}

remove_stale_tailnet_cluster_devices() {
  local oauth_json client_id client_secret token_json access_token devices_json
  local device_ids device_count device_id delete_code tag selector maximum

  info "Removing previous Tailnet identities before recreation"
  oauth_json="$(age -d -i "${AGE_KEY_FILE}" \
    "${BACKUP_DIR}/tailscale-oauth.secret.json.age")"
  client_id="$(jq -er '.data.client_id | @base64d' <<<"${oauth_json}")"
  client_secret="$(jq -er '.data.client_secret | @base64d' <<<"${oauth_json}")"

  for tag in tag:ai-worker-trusted tag:k8s-operator; do
    token_json="$(curl -fsS -u "${client_id}:${client_secret}" \
      --data-urlencode 'grant_type=client_credentials' \
      --data-urlencode 'scope=devices:core' \
      --data-urlencode "tags=${tag}" \
      https://api.tailscale.com/api/v2/oauth/token)"
    access_token="$(jq -er '.access_token' <<<"${token_json}")"
    devices_json="$(curl -fsS -H "Authorization: Bearer ${access_token}" \
      https://api.tailscale.com/api/v2/tailnet/-/devices)"
    if [[ "${tag}" == tag:ai-worker-trusted ]]; then
      selector='(.name == $worker_fqdn or .hostname == "ai-worker-ai-gateway-egress")'
      maximum=2
    else
      selector='(.hostname == "tailscale-operator")'
      maximum=1
    fi
    device_ids="$(jq -r --arg worker_fqdn "${TAILSCALE_WORKER_FQDN}" \
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

restore_secret() {
  local input=$1
  age -d -i "${AGE_KEY_FILE}" "${input}" \
    | kubectl --kubeconfig "${KUBECONFIG_PATH}" apply -f - >/dev/null
}

restore_platform() {
  export KUBECONFIG="${KUBECONFIG_PATH}"
  info "Waiting for the new kube-apiserver VIP"
  for _ in $(seq 1 60); do
    if kubectl version -o json >/dev/null 2>&1; then
      break
    fi
    sleep 5
  done
  kubectl version -o json >/dev/null 2>&1 \
    || die "kube-apiserver did not become ready within five minutes"
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

  info "Bootstrapping Argo CD at revision ${GITOPS_REVISION}"
  GITOPS_REVISION="${GITOPS_REVISION}" "${SCRIPT_DIR}/bootstrap-argocd.sh"
  "${SCRIPT_DIR}/reconcile-cluster-platform.sh"
  kubectl -n tailscale rollout status deployment/operator --timeout=10m
  kubectl wait --for=jsonpath='{.status.conditions[?(@.type=="ProxyClassReady")].status}'=True \
    proxyclass/restricted-userspace proxyclass/kernel-egress --timeout=5m
  "${SCRIPT_DIR}/configure-tailscale-dns.sh"
  kubectl -n longhorn-system rollout status daemonset/longhorn-manager --timeout=15m
  "${SCRIPT_DIR}/reconcile-longhorn-worker-plane.sh"

  info "Restoring the internal registry"
  kubectl -n image-registry rollout status deployment/registry --timeout=15m
  age -d -i "${AGE_KEY_FILE}" "${REGISTRY_DATA_BACKUP}" \
    | kubectl -n image-registry exec -i deploy/registry -- tar -C /var/lib/registry -xf -
  kubectl -n image-registry rollout restart deployment/registry
  kubectl -n image-registry rollout status deployment/registry --timeout=10m

  info "Restoring AI Worker secrets and persistent data"
  kubectl apply -f "${AI_WORKER_REPO}/deploy/kubernetes/namespace.yaml" >/dev/null
  restore_secret "${BACKUP_DIR}/ai-worker-runtime.secret.json.age"
  restore_secret "${BACKUP_DIR}/ai-worker-codex-auth.secret.json.age"
  kubectl apply -f "${AI_WORKER_REPO}/deploy/kubernetes/config.yaml" >/dev/null
  kubectl apply -f "${AI_WORKER_REPO}/deploy/kubernetes/pvc.yaml" >/dev/null
  kubectl wait -n ai-worker --for=jsonpath='{.status.phase}'=Bound \
    pvc/ai-business-worker-data --timeout=10m

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
  kubectl apply -k "${AI_WORKER_REPO}/deploy/kubernetes"
  kubectl -n ai-worker rollout status deployment/ai-business-worker --timeout=15m
  kubectl -n ai-worker exec deployment/ai-business-worker -c worker -- \
    python -c "import socket; socket.getaddrinfo('ai-gateway-01.${TAILSCALE_WORKER_FQDN#*.}', 443)" \
    >/dev/null
  kubectl -n tailscale wait --for=condition=Ready pod \
    -l tailscale.com/parent-resource=ai-business-worker --timeout=10m
  kubectl -n ai-worker wait \
    --for=jsonpath='{.status.loadBalancer.ingress[0].hostname}'="${TAILSCALE_WORKER_FQDN}" \
    ingress/ai-business-worker --timeout=10m
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
  kubectl -n argocd get applications -o json | jq -e '
    all(.items[];
      .status.sync.status == "Synced" and .status.health.status == "Healthy")
  ' >/dev/null || {
    kubectl -n argocd get applications >&2
    die "an Argo CD Application is not Synced/Healthy"
  }
  kubectl get pods -A -o json | jq -e '
    all(.items[];
      .metadata.deletionTimestamp != null or
      .status.phase == "Succeeded" or
      (.status.phase == "Running" and
        all(.status.containerStatuses // []; .ready == true)))
  ' >/dev/null || {
    kubectl get pods -A >&2
    die "a cluster Pod is not healthy"
  }
  curl -fsS --retry 12 --retry-all-errors --retry-delay 5 \
    "https://${TAILSCALE_WORKER_FQDN}/health" >/dev/null
  ok "Six-node cluster, GitOps platform, and Tailnet Worker health verified"
}

verify_gateway_worker_path() {
  [[ "${GATEWAY_SMOKE}" == 1 ]] || {
    info "Gateway-to-Worker smoke test skipped"
    return
  }

  local client_id client_secret
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
  printf '%s\n%s\n' "${client_id}" "${client_secret}" \
    | ssh -o BatchMode=yes -o ConnectTimeout=10 craftz@172.16.40.30 '
        IFS= read -r CF_ACCESS_CLIENT_ID
        IFS= read -r CF_ACCESS_CLIENT_SECRET
        export CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET
        sudo -n --preserve-env=CF_ACCESS_CLIENT_ID,CF_ACCESS_CLIENT_SECRET \
          /opt/ai-business-gateway/scripts/smoke-test.sh
      '
  unset client_id client_secret
  ok "Cloudflare Access, Gateway dispatch, Worker execution, and callback verified"
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
