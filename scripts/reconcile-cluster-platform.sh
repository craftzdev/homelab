#!/usr/bin/env bash
# Reconcile the complete GitOps platform in dependency order and fail closed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
KUBECONFIG_PATH="${KUBECONFIG:-${REPO_ROOT}/_out/kubeconfig}"
APPLICATION_TIMEOUT_SECONDS="${APPLICATION_TIMEOUT_SECONDS:-1800}"
DEFER_AI_WORKER="${DEFER_AI_WORKER:-0}"

info() { printf '[INFO] %s\n' "$*"; }
ok() { printf '[OK]   %s\n' "$*"; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

for tool in kubectl jq; do
  command -v "${tool}" >/dev/null || die "required command not found: ${tool}"
done
[[ -s "${KUBECONFIG_PATH}" ]] || die "kubeconfig not found: ${KUBECONFIG_PATH}"
[[ "${APPLICATION_TIMEOUT_SECONDS}" =~ ^[0-9]+$ ]] \
  || die "APPLICATION_TIMEOUT_SECONDS must be an integer"
[[ "${DEFER_AI_WORKER}" == 0 || "${DEFER_AI_WORKER}" == 1 ]] \
  || die "DEFER_AI_WORKER must be 0 or 1"

export KUBECONFIG="${KUBECONFIG_PATH}"

application_diagnostics() {
  local name=$1
  kubectl --request-timeout=15s -n argocd get application "${name}" -o json 2>/dev/null \
    | jq '{sync:.status.sync.status,health:.status.health.status,
      conditions:(.status.conditions // []),operation:.status.operationState.phase}' >&2 \
    || true
}

wait_for_application() {
  local name=$1 deadline next_refresh sync health status_json
  deadline=$((SECONDS + APPLICATION_TIMEOUT_SECONDS))
  next_refresh=$((SECONDS + 60))
  info "Waiting for Argo CD Application ${name}"
  while (( SECONDS < deadline )); do
    if status_json="$(kubectl --request-timeout=15s -n argocd \
        get application "${name}" -o json 2>/dev/null)"; then
      sync="$(jq -r '.status.sync.status // ""' <<<"${status_json}")"
      health="$(jq -r '.status.health.status // ""' <<<"${status_json}")"
      if [[ "${sync}" == Synced && "${health}" == Healthy ]]; then
        ok "${name} is Synced/Healthy"
        return
      fi
    fi
    # A heavily loaded first bootstrap can leave an otherwise successful
    # Application with stale Degraded/Progressing health. Periodic hard refresh
    # makes the wait converge without operator intervention.
    if (( SECONDS >= next_refresh )); then
      kubectl --request-timeout=15s -n argocd annotate application "${name}" \
        argocd.argoproj.io/refresh=hard --overwrite >/dev/null 2>&1 || true
      next_refresh=$((SECONDS + 60))
    fi
    sleep 5
  done
  application_diagnostics "${name}"
  die "Application ${name} did not become Synced/Healthy"
}

resume_application() {
  local name=$1
  # bootstrap-argocd.sh pauses feature-branch Applications so their initial
  # automated syncs cannot stampede etcd. Removing the annotation is idempotent
  # and harmless for main/root-managed Applications where it is absent.
  kubectl --request-timeout=15s -n argocd annotate application "${name}" \
    argocd.argoproj.io/skip-reconcile- >/dev/null 2>&1 || true
  refresh_application "${name}"
}

refresh_application() {
  local name=$1
  for _ in $(seq 1 12); do
    if kubectl --request-timeout=15s -n argocd annotate application "${name}" \
        argocd.argoproj.io/refresh=hard --overwrite >/dev/null 2>&1; then
      return
    fi
    sleep 5
  done
  die "could not request an Argo CD refresh for ${name}"
}

verify_ksops_runtime() {
  info "Verifying KSOPS and the age key inside repo-server"
  for _ in $(seq 1 60); do
    # shellcheck disable=SC2016 # The remote shell expands this variable.
    if kubectl --request-timeout=15s -n argocd \
        exec deploy/argocd-repo-server -c repo-server -- \
        sh -c 'command -v ksops >/dev/null && test -s "$SOPS_AGE_KEY_FILE"' \
        >/dev/null 2>&1; then
      ok "KSOPS and the age key are available at runtime"
      return
    fi
    sleep 5
  done
  die "KSOPS runtime verification failed after kubelet certificate approval"
}

wait_for_longhorn_csi() {
  local deadline ready name
  local workers=(k8s-worker-1 k8s-worker-2 k8s-worker-3)
  local deployments=(
    longhorn-driver-deployer
    csi-attacher
    csi-provisioner
    csi-resizer
    csi-snapshotter
  )
  local daemonsets=(longhorn-manager longhorn-csi-plugin)

  deadline=$((SECONDS + APPLICATION_TIMEOUT_SECONDS))
  info "Waiting for Longhorn CSI provisioning to become available"
  while (( SECONDS < deadline )); do
    ready=true

    kubectl --request-timeout=15s get csidriver driver.longhorn.io \
      >/dev/null 2>&1 || ready=false
    for name in "${workers[@]}"; do
      kubectl --request-timeout=15s wait --for=condition=Ready \
        "node/${name}" --timeout=5s >/dev/null 2>&1 || ready=false
    done
    for name in "${deployments[@]}"; do
      kubectl --request-timeout=15s -n longhorn-system rollout status \
        "deployment/${name}" --timeout=5s >/dev/null 2>&1 || ready=false
    done
    for name in "${daemonsets[@]}"; do
      kubectl --request-timeout=15s -n longhorn-system rollout status \
        "daemonset/${name}" --timeout=5s >/dev/null 2>&1 || ready=false
    done

    if [[ "${ready}" == true ]]; then
      ok "Longhorn CSI provisioning is available on the worker plane"
      return
    fi
    sleep 5
  done

  kubectl --request-timeout=15s -n longhorn-system get \
    deployments,daemonsets,pods >&2 || true
  kubectl --request-timeout=15s get csidriver driver.longhorn.io >&2 || true
  die "Longhorn CSI provisioning did not become available"
}

# This CiliumNetworkPolicy belonged to the pre-Longhorn policy model. Argo CD
# cannot prune it because it is no longer in the desired manifest set.
kubectl --request-timeout=15s -n longhorn-system delete ciliumnetworkpolicy default-deny \
  --ignore-not-found --wait=true >/dev/null 2>&1 || true

# Foundation first. Kubelet serving certificates are required for reliable
# container status/log/exec operations during the rest of bootstrap. Longhorn's
# Application can report Healthy before the CSI resources it generates exist,
# so storage receives an additional explicit readiness gate before any PVC user.
for app in gateway-api-crds cilium kubelet-serving-cert-approver \
  snapshot-controller longhorn; do
  resume_application "${app}"
  wait_for_application "${app}"
done

verify_ksops_runtime
wait_for_longhorn_csi

# Monitoring installs the ServiceMonitor CRD required by cert-manager and Trivy,
# so those applications are deliberately checked later.
for app in image-registry tailscale-operator security-config monitoring; do
  resume_application "${app}"
  wait_for_application "${app}"
done

# Harbor's upstream chart uses Helm `lookup` to retain its internal database
# credential. Argo CD deliberately renders without cluster lookup, so bootstrap
# the chart first, then inject the Keychain-backed client value into the one
# generated Secret key before enforcing application health.
resume_application harbor
for _ in $(seq 1 60); do
  kubectl --request-timeout=15s -n harbor get secret harbor-core \
    >/dev/null 2>&1 && break
  sleep 5
done
kubectl --request-timeout=15s -n harbor get secret harbor-core >/dev/null 2>&1 \
  || die "Harbor did not render its runtime Secrets"
"${SCRIPT_DIR}/bootstrap-cluster-secrets.sh"
wait_for_application harbor

for app in cert-manager trivy-operator network-policies; do
  resume_application "${app}"
  wait_for_application "${app}"
done

for app in arc-controller arc-runners; do
  resume_application "${app}"
  wait_for_application "${app}"
done

# Application workloads are reconciled only after their storage, networking,
# private repository credential, and runtime dependencies are available. A
# disaster restore defers this gate until its out-of-band Secrets and PVC data
# have been restored.
if [[ "${DEFER_AI_WORKER}" == 0 ]]; then
  resume_application ai-business-worker
  wait_for_application ai-business-worker
else
  info "AI Business Worker reconciliation deferred for data restore"
fi

for retired_app in gateway cloudflared velero; do
  if kubectl --request-timeout=15s -n argocd \
      get application "${retired_app}" >/dev/null 2>&1; then
    die "retired Application still exists: ${retired_app}"
  fi
done

ok "The complete GitOps platform is Synced/Healthy"
