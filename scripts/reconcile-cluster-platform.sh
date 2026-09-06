#!/usr/bin/env bash
# Reconcile the complete GitOps platform in dependency order and fail closed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
KUBECONFIG_PATH="${KUBECONFIG:-${REPO_ROOT}/_out/kubeconfig}"
APPLICATION_TIMEOUT_SECONDS="${APPLICATION_TIMEOUT_SECONDS:-1800}"

readonly EXPECTED_APPLICATIONS=(
  network-policies
  gateway-api-crds
  cilium
  snapshot-controller
  longhorn
  image-registry
  tailscale-operator
  security-config
  kubelet-serving-cert-approver
  monitoring
  cert-manager
  trivy-operator
)

info() { printf '[INFO] %s\n' "$*"; }
ok() { printf '[OK]   %s\n' "$*"; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

for tool in kubectl jq; do
  command -v "${tool}" >/dev/null || die "required command not found: ${tool}"
done
[[ -s "${KUBECONFIG_PATH}" ]] || die "kubeconfig not found: ${KUBECONFIG_PATH}"
[[ "${APPLICATION_TIMEOUT_SECONDS}" =~ ^[0-9]+$ ]] \
  || die "APPLICATION_TIMEOUT_SECONDS must be an integer"

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

# This CiliumNetworkPolicy belonged to the pre-Longhorn policy model. Argo CD
# cannot prune it because it is no longer in the desired manifest set.
kubectl --request-timeout=15s -n longhorn-system delete ciliumnetworkpolicy default-deny \
  --ignore-not-found --wait=true >/dev/null 2>&1 || true

# Foundation first. Monitoring installs the ServiceMonitor CRD required by
# cert-manager and Trivy, so those applications are deliberately checked later.
for app in gateway-api-crds cilium snapshot-controller longhorn \
  image-registry tailscale-operator security-config \
  kubelet-serving-cert-approver monitoring; do
  resume_application "${app}"
  wait_for_application "${app}"
done

# kubectl exec requires a trusted kubelet serving certificate. Verify the
# repo-server runtime only after the certificate approver Application is healthy.
verify_ksops_runtime

for app in cert-manager trivy-operator network-policies; do
  resume_application "${app}"
  wait_for_application "${app}"
done

for retired_app in gateway cloudflared velero; do
  if kubectl --request-timeout=15s -n argocd \
      get application "${retired_app}" >/dev/null 2>&1; then
    die "retired Application still exists: ${retired_app}"
  fi
done

ok "The complete GitOps platform is Synced/Healthy"
