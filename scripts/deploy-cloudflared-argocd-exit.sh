#!/usr/bin/env bash
set -Eeuo pipefail

# Deploy a cloudflared (Cloudflare Tunnel) exit for ArgoCD
# This runs cloudflared as a connector inside the cluster and forwards
# HTTPS traffic at the FQDN to the ArgoCD service inside the cluster.
#
# Requirements:
# - kubectl configured to point to the target cluster
# - You have created a Named Tunnel in Cloudflare Zero Trust dashboard
#   and obtained a one-time Connector Token for that tunnel
# - A Kubernetes Secret containing the token exists (or you allow this
#   script to create it via env CLOUDFLARED_TOKEN)
# - In Cloudflare Zero Trust > Tunnels, configure a Public Hostname
#   for your FQDN that points to your origin (e.g. http://argocd-server.argocd:80)
# - Cloudflare Access application/policy for the FQDN is configured if you
#   want SSO/Zero Trust protection
#
# Customizable via environment variables:
#   CLOUDFLARED_NAMESPACE (default: cloudflared-tunnel-exits)
#   FQDN                 (default: argocd.craftz.dev)
#   BACKEND_HOSTPORT     (default: argocd-server.argocd:80)
#   BACKEND_SCHEME       (default: http)   # http or https (used only for guidance text)
#   TUNNEL_TOKEN_SECRET  (default: cloudflared-argocd-token)
#   TUNNEL_TOKEN_KEY     (default: token)
#   CLOUDFLARED_IMAGE    (default: cloudflare/cloudflared:latest)
#   APPLY_TIMEOUT        (default: 5m)
#
# Usage:
#   # Create the secret with connector token (recommended)
#   kubectl -n cloudflared-tunnel-exits create secret generic cloudflared-argocd-token \
#     --from-literal=token='PASTE_YOUR_CONNECTOR_TOKEN_HERE'
#
#   # Deploy
#   ./scripts/deploy-cloudflared-argocd-exit.sh
#
#   # Note: Public Hostname mapping must be configured in Cloudflare UI for ${FQDN}
#   # Example: Type=HTTP, URL=${BACKEND_SCHEME}://${BACKEND_HOSTPORT}

NAMESPACE="${CLOUDFLARED_NAMESPACE:-cloudflared-tunnel-exits}"
FQDN="${FQDN:-argocd.craftz.dev}"
BACKEND_HOSTPORT="${BACKEND_HOSTPORT:-argocd-server.argocd:80}"
BACKEND_SCHEME="${BACKEND_SCHEME:-http}"
SECRET_NAME="${TUNNEL_TOKEN_SECRET:-cloudflared-argocd-token}"
SECRET_KEY="${TUNNEL_TOKEN_KEY:-token}"
CLOUDFLARED_IMAGE="${CLOUDFLARED_IMAGE:-cloudflare/cloudflared:latest}"
APPLY_TIMEOUT="${APPLY_TIMEOUT:-5m}"

log() {
  echo "[cloudflared-argocd] $*" >&2
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: Required command '$1' not found in PATH" >&2
    exit 1
  fi
}

require_cmd kubectl

# Ensure namespace
if ! kubectl get ns "$NAMESPACE" >/dev/null 2>&1; then
  log "Creating namespace: $NAMESPACE"
  kubectl create ns "$NAMESPACE"
else
  log "Namespace exists: $NAMESPACE"
fi

# Optionally create secret from env CLOUDFLARED_TOKEN if provided and secret absent
if ! kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" >/dev/null 2>&1; then
  if [[ -n "${CLOUDFLARED_TOKEN:-}" ]]; then
    log "Creating Secret $SECRET_NAME from env CLOUDFLARED_TOKEN"
    kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" \
      --from-literal="${SECRET_KEY}=${CLOUDFLARED_TOKEN}"
  else
    log "Kubernetes Secret '$SECRET_NAME' not found in namespace '$NAMESPACE'."
    log "Please create it with your Cloudflare Tunnel connector token, e.g.:"
    echo "kubectl -n $NAMESPACE create secret generic $SECRET_NAME --from-literal=$SECRET_KEY='PASTE_TOKEN'" >&2
  fi
fi

log "Applying cloudflared Deployment (connector) for $FQDN"

# Build manifests and apply
MANIFEST=$(cat <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cloudflared-argocd-exit
  namespace: ${NAMESPACE}
  labels:
    app: cloudflared-argocd-exit
spec:
  replicas: 1
  selector:
    matchLabels:
      app: cloudflared-argocd-exit
  template:
    metadata:
      labels:
        app: cloudflared-argocd-exit
    spec:
      containers:
        - name: cloudflared
          image: ${CLOUDFLARED_IMAGE}
          imagePullPolicy: IfNotPresent
          env:
            - name: TUNNEL_TOKEN
              valueFrom:
                secretKeyRef:
                  name: ${SECRET_NAME}
                  key: ${SECRET_KEY}
          command: ["/bin/sh","-c"]
          args:
            - >-
              cloudflared tunnel --no-autoupdate run --token "$TUNNEL_TOKEN"
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              memory: 128Mi
YAML
)

echo "$MANIFEST" | kubectl apply -f -

log "Waiting for rollout..."
kubectl -n "$NAMESPACE" rollout status deploy/cloudflared-argocd-exit --timeout="$APPLY_TIMEOUT" || true

cat >&2 <<EOF
Deployed cloudflared connector:
  FQDN: ${FQDN}
  (Configure Public Hostname mapping in Cloudflare UI)
  Suggested Origin: ${BACKEND_SCHEME}://${BACKEND_HOSTPORT}
  Namespace: ${NAMESPACE}
  Secret: ${SECRET_NAME} (key: ${SECRET_KEY})

Next steps (Cloudflare side):
  1) Zero Trust -> Tunnels -> select your Named Tunnel -> Public Hostnames -> Add a hostname
     - Hostname: ${FQDN}
     - Type: HTTP
     - URL: ${BACKEND_SCHEME}://${BACKEND_HOSTPORT}
  2) DNS will be created automatically (CNAME to <uuid>.cfargotunnel.com) if proxied
  3) Zero Trust -> Access -> Applications -> Add application -> Self-hosted
     - Application domain: ${FQDN}
     - Policies: set who can access (e.g., your email or IdP groups)

Validation:
  - kubectl -n ${NAMESPACE} logs deploy/cloudflared-argocd-exit -f
  - Open https://${FQDN} in a browser; Cloudflare Access should prompt, then ArgoCD login.

If you later expose multiple apps via the same tunnel, just add more Public Hostnames.
EOF

log "Done."