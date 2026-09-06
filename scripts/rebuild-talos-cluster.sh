#!/usr/bin/env bash
# Rebuild all six Talos Kubernetes VMs from OpenTofu and restore the AI Worker.
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
#   TAILSCALE_WORKER_FQDN=... canonical Worker MagicDNS name
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TOFU_DIR="${REPO_ROOT}/tofu/10-proxmox-talos"
KUBECONFIG_PATH="${REPO_ROOT}/_out/kubeconfig"
AI_WORKER_REPO="${AI_WORKER_REPO:-${REPO_ROOT}/../ai-business-worker}"
GITOPS_REVISION="${GITOPS_REVISION:-$(git -C "${REPO_ROOT}" branch --show-current)}"
PBS_BACKUP="${PBS_BACKUP:-1}"
TAILSCALE_WORKER_FQDN="${TAILSCALE_WORKER_FQDN:-ai-worker-k8s.tailb6c7d.ts.net}"
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
mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"
DESTROY_PLAN="${BACKUP_DIR}/destroy.tfplan"
DESTROY_GUARD="${BACKUP_DIR}/destroy-plan-guard.json"

cleanup() {
  unset TF_VAR_state_encryption_passphrase TF_VAR_proxmox_api_token AGE_RECIPIENT
}
trap cleanup EXIT

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

backup_cluster_data() {
  [[ -s "${KUBECONFIG_PATH}" ]] || die "current kubeconfig not found"
  kubectl --kubeconfig "${KUBECONFIG_PATH}" get nodes >/dev/null
  info "Creating encrypted application-level backups"

  kubectl --kubeconfig "${KUBECONFIG_PATH}" -n ai-worker \
    exec deploy/ai-business-worker -c worker -- \
      tar -C /data --exclude=./lost+found -cf - . \
    | age -r "${AGE_RECIPIENT}" -o "${BACKUP_DIR}/ai-worker-data.tar.age"
  kubectl --kubeconfig "${KUBECONFIG_PATH}" -n image-registry \
    exec deploy/registry -- tar -C /var/lib/registry -cf - . \
    | age -r "${AGE_RECIPIENT}" -o "${BACKUP_DIR}/registry-data.tar.age"
  chmod 600 "${BACKUP_DIR}"/*.age

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
  ok "Existing encrypted recovery set verified"
}

remove_stale_tailnet_worker() {
  local oauth_json client_id client_secret token_json access_token devices_json
  local device_ids device_count device_id delete_code

  info "Removing the previous Tailnet Worker identity before recreation"
  oauth_json="$(age -d -i "${AGE_KEY_FILE}" \
    "${BACKUP_DIR}/tailscale-oauth.secret.json.age")"
  client_id="$(jq -er '.data.client_id | @base64d' <<<"${oauth_json}")"
  client_secret="$(jq -er '.data.client_secret | @base64d' <<<"${oauth_json}")"
  token_json="$(curl -fsS -u "${client_id}:${client_secret}" \
    --data-urlencode 'grant_type=client_credentials' \
    --data-urlencode 'scope=devices:core' \
    --data-urlencode 'tags=tag:ai-worker-trusted' \
    https://api.tailscale.com/api/v2/oauth/token)"
  access_token="$(jq -er '.access_token' <<<"${token_json}")"
  devices_json="$(curl -fsS -H "Authorization: Bearer ${access_token}" \
    https://api.tailscale.com/api/v2/tailnet/-/devices)"
  device_ids="$(jq -r --arg fqdn "${TAILSCALE_WORKER_FQDN}" '
    [.devices[]
      | select(.name == $fqdn)
      | select((.tags // []) | index("tag:ai-worker-trusted"))
      | .id][]' <<<"${devices_json}")"
  device_count="$(sed '/^$/d' <<<"${device_ids}" | wc -l | tr -d ' ')"
  [[ "${device_count}" -le 1 ]] \
    || die "multiple canonical Tailnet Worker devices matched; refusing cleanup"

  if [[ "${device_count}" == 1 ]]; then
    device_id="${device_ids}"
    delete_code="$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE \
      -H "Authorization: Bearer ${access_token}" \
      "https://api.tailscale.com/api/v2/device/${device_id}")"
    [[ "${delete_code}" == 200 || "${delete_code}" == 204 ]] \
      || die "Tailnet device cleanup failed with HTTP ${delete_code}"
    ok "Previous Tailnet Worker identity removed"
  else
    info "No previous canonical Tailnet Worker identity found"
  fi
  unset oauth_json client_id client_secret token_json access_token devices_json
}

restore_secret() {
  local input=$1
  age -d -i "${AGE_KEY_FILE}" "${input}" \
    | kubectl --kubeconfig "${KUBECONFIG_PATH}" apply -f - >/dev/null
}

wait_for_application() {
  local name=$1
  kubectl --kubeconfig "${KUBECONFIG_PATH}" -n argocd wait \
    --for=jsonpath='{.status.health.status}'=Healthy "application/${name}" \
    --timeout=20m
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

  kubectl create namespace image-registry --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl create namespace tailscale --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  restore_secret "${BACKUP_DIR}/registry-tls.secret.json.age"
  restore_secret "${BACKUP_DIR}/tailscale-oauth.secret.json.age"

  info "Bootstrapping Argo CD at revision ${GITOPS_REVISION}"
  GITOPS_REVISION="${GITOPS_REVISION}" "${SCRIPT_DIR}/bootstrap-argocd.sh"
  wait_for_application snapshot-controller
  wait_for_application longhorn
  wait_for_application image-registry
  wait_for_application tailscale-operator
  kubectl -n tailscale rollout status deployment/operator --timeout=10m
  kubectl wait --for=jsonpath='{.status.conditions[?(@.type=="ProxyClassReady")].status}'=True \
    proxyclass/restricted-userspace proxyclass/kernel-egress --timeout=5m
  kubectl -n longhorn-system rollout status daemonset/longhorn-manager --timeout=15m
  "${SCRIPT_DIR}/reconcile-longhorn-worker-plane.sh"

  info "Restoring the internal registry"
  kubectl -n image-registry rollout status deployment/registry --timeout=15m
  age -d -i "${AGE_KEY_FILE}" "${BACKUP_DIR}/registry-data.tar.age" \
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
  age -d -i "${AGE_KEY_FILE}" "${BACKUP_DIR}/ai-worker-data.tar.age" \
    | kubectl -n ai-worker exec -i rebuild-data-restore -- \
      sh -c 'mkdir -p /tmp/restore && tar -C /tmp/restore --exclude=./lost+found -xf - && cp -R /tmp/restore/. /data/'
  kubectl -n ai-worker exec rebuild-data-restore -- \
    sh -c 'test -f /data/worker.db && test -d /data/jobs'
  kubectl -n ai-worker delete pod rebuild-data-restore --wait=true >/dev/null
  kubectl apply -k "${AI_WORKER_REPO}/deploy/kubernetes"
  kubectl -n ai-worker rollout status deployment/ai-business-worker --timeout=15m
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
  curl -fsS --retry 12 --retry-all-errors --retry-delay 5 \
    "https://${TAILSCALE_WORKER_FQDN}/health" >/dev/null
  ok "Six-node cluster and Tailnet Worker health verified"
}

if [[ "${RESTORE_ONLY}" == 1 ]]; then
  verify_recovery_set
  restore_platform
  verify_rebuild
  printf '\nRestore completed successfully.\nRecovery set: %s\nGitOps revision: %s\n' \
    "${BACKUP_DIR}" "${GITOPS_REVISION}"
  exit 0
fi

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
remove_stale_tailnet_worker

info "Recreating all OpenTofu and Talos resources"
(cd "${TOFU_DIR}" && tofu apply -auto-approve)
restore_platform
verify_rebuild

printf '\nRebuild completed successfully.\nRecovery set: %s\nGitOps revision: %s\n' \
  "${BACKUP_DIR}" "${GITOPS_REVISION}"
