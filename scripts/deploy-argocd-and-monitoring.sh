#!/usr/bin/env bash
set -Eeuo pipefail

# Deploy ArgoCD and kube-prometheus-stack (Prometheus + Grafana) via Helm
# Requirements:
# - kubectl configured to point to the target cluster
# - helm installed and able to reach the internet (for chart repos)
#
# Customizable via environment variables:
#   ARGOCD_NAMESPACE (default: argocd)
#   ARGOCD_RELEASE   (default: argocd)
#   MONITORING_NAMESPACE (default: monitoring)
#   MONITORING_RELEASE   (default: monitoring)
#   HELM_TIMEOUT (default: 10m)
#
# Usage:
#   ./scripts/deploy-argocd-and-monitoring.sh

ARGOCD_NAMESPACE="${ARGOCD_NAMESPACE:-argocd}"
ARGOCD_RELEASE="${ARGOCD_RELEASE:-argocd}"
MONITORING_NAMESPACE="${MONITORING_NAMESPACE:-monitoring}"
MONITORING_RELEASE="${MONITORING_RELEASE:-monitoring}"
HELM_TIMEOUT="${HELM_TIMEOUT:-10m}"

log() {
  echo "[deploy] $*" >&2
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: Required command '$1' not found in PATH" >&2
    exit 1
  fi
}

log "Checking required commands..."
require_cmd kubectl
require_cmd helm

log "Checking kubectl context and cluster reachability..."
if ! kubectl config current-context >/dev/null 2>&1; then
  echo "ERROR: kubectl is not configured with a current context" >&2
  exit 1
fi
if ! kubectl get nodes -o name >/dev/null 2>&1; then
  echo "ERROR: Unable to reach Kubernetes cluster (kubectl get nodes failed)" >&2
  exit 1
fi

log "Ensuring namespaces exist: ${ARGOCD_NAMESPACE}, ${MONITORING_NAMESPACE}"
kubectl get ns "$ARGOCD_NAMESPACE" >/dev/null 2>&1 || kubectl create ns "$ARGOCD_NAMESPACE"
kubectl get ns "$MONITORING_NAMESPACE" >/dev/null 2>&1 || kubectl create ns "$MONITORING_NAMESPACE"

log "Setting up Helm repositories..."
# Add Argo Helm repository
if ! helm repo list | awk '{print $1}' | grep -qx "argo"; then
  helm repo add argo https://argoproj.github.io/argo-helm
fi
# Add Prometheus Community repository
if ! helm repo list | awk '{print $1}' | grep -qx "prometheus-community"; then
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
fi
helm repo update

# --- ArgoCD ---
log "Deploying ArgoCD (release=${ARGOCD_RELEASE}, ns=${ARGOCD_NAMESPACE})"
if helm -n "$ARGOCD_NAMESPACE" list --filter "^${ARGOCD_RELEASE}$" -q | grep -q "^${ARGOCD_RELEASE}$"; then
  log "Release exists; upgrading ArgoCD..."
  helm upgrade "$ARGOCD_RELEASE" argo/argo-cd \
    --namespace "$ARGOCD_NAMESPACE" \
    --wait --timeout "$HELM_TIMEOUT" \
    --set server.service.type="ClusterIP"
else
  log "Release not found; installing ArgoCD..."
  helm install "$ARGOCD_RELEASE" argo/argo-cd \
    --namespace "$ARGOCD_NAMESPACE" \
    --wait --timeout "$HELM_TIMEOUT" \
    --set server.service.type="ClusterIP"
fi

log "Waiting for ArgoCD server rollout..."
kubectl -n "$ARGOCD_NAMESPACE" rollout status deploy/"${ARGOCD_RELEASE}"-server --timeout=5m || true

# Try to show initial admin password (if secret exists)
log "Fetching ArgoCD initial admin password (if present)..."
if kubectl -n "$ARGOCD_NAMESPACE" get secret argocd-initial-admin-secret >/dev/null 2>&1; then
  echo "ArgoCD initial admin password: $(kubectl -n "$ARGOCD_NAMESPACE" get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d)" >&2
else
  echo "(argocd-initial-admin-secret not found; password may already be configured)" >&2
fi

# --- kube-prometheus-stack (Prometheus + Grafana) ---
log "Deploying kube-prometheus-stack (release=${MONITORING_RELEASE}, ns=${MONITORING_NAMESPACE})"
if helm -n "$MONITORING_NAMESPACE" list --filter "^${MONITORING_RELEASE}$" -q | grep -q "^${MONITORING_RELEASE}$"; then
  log "Release exists; upgrading kube-prometheus-stack..."
  helm upgrade "$MONITORING_RELEASE" prometheus-community/kube-prometheus-stack \
    --namespace "$MONITORING_NAMESPACE" \
    --wait --timeout "$HELM_TIMEOUT"
else
  log "Release not found; installing kube-prometheus-stack..."
  helm install "$MONITORING_RELEASE" prometheus-community/kube-prometheus-stack \
    --namespace "$MONITORING_NAMESPACE" \
    --wait --timeout "$HELM_TIMEOUT"
fi

log "Waiting for Grafana rollout..."
kubectl -n "$MONITORING_NAMESPACE" rollout status deploy/"${MONITORING_RELEASE}"-grafana --timeout=5m || true

log "Attempting to fetch Grafana admin password (if set in chart secret)..."
if kubectl -n "$MONITORING_NAMESPACE" get secret "${MONITORING_RELEASE}"-grafana >/dev/null 2>&1; then
  if kubectl -n "$MONITORING_NAMESPACE" get secret "${MONITORING_RELEASE}"-grafana -o jsonpath="{.data.admin-password}" >/dev/null 2>&1; then
    echo "Grafana admin password: $(kubectl -n "$MONITORING_NAMESPACE" get secret "${MONITORING_RELEASE}"-grafana -o jsonpath="{.data.admin-password}" | base64 -d)" >&2
  else
    echo "(Grafana admin password not found in secret; default may apply or a custom secret is used)" >&2
  fi
else
  echo "(Grafana secret '${MONITORING_RELEASE}-grafana' not found; ensure chart is deployed successfully)" >&2
fi

cat >&2 <<EOF

Access tips:
- ArgoCD UI:
    kubectl -n ${ARGOCD_NAMESPACE} port-forward svc/${ARGOCD_RELEASE}-server 8080:443
  Then open http://localhost:8080 and login with 'admin' and the password shown above (or configured).

- Grafana UI:
    kubectl -n ${MONITORING_NAMESPACE} port-forward svc/${MONITORING_RELEASE}-grafana 3000:80
  Then open http://localhost:3000 and login with 'admin' and the password shown above (or the default).

Security note: Change default/initial credentials promptly in production environments.
EOF

log "Done. ArgoCD and Monitoring stack are deployed (or upgraded)."