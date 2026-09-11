#!/usr/bin/env bash
# Materialize non-Git cluster secrets from the local macOS Keychain.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
KUBECONFIG_PATH="${KUBECONFIG:-${REPO_ROOT}/_out/kubeconfig}"
GRAFANA_KEYCHAIN_SERVICE="${GRAFANA_KEYCHAIN_SERVICE:-dev.craftz.homelab.grafana-admin}"
GRAFANA_KEYCHAIN_ACCOUNT="${GRAFANA_KEYCHAIN_ACCOUNT:-admin}"
MINIO_KEYCHAIN_SERVICE="${MINIO_KEYCHAIN_SERVICE:-dev.craftz.homelab.minio-root}"
MINIO_KEYCHAIN_ACCOUNT="${MINIO_KEYCHAIN_ACCOUNT:-minio-root}"
LOKI_S3_KEYCHAIN_SERVICE="${LOKI_S3_KEYCHAIN_SERVICE:-dev.craftz.homelab.loki-s3}"
LOKI_S3_KEYCHAIN_ACCOUNT="${LOKI_S3_KEYCHAIN_ACCOUNT:-loki}"
WORKER_REPO_KEYCHAIN_SERVICE="${WORKER_REPO_KEYCHAIN_SERVICE:-dev.craftz.homelab.argocd-ai-worker-deploy-key}"
WORKER_REPO_KEYCHAIN_ACCOUNT="${WORKER_REPO_KEYCHAIN_ACCOUNT:-craftzdev/ai-business-worker}"
WORKER_REPO_URL="${WORKER_REPO_URL:-git@github.com:craftzdev/ai-business-worker.git}"
AGENT_REPO_KEYCHAIN_SERVICE="${AGENT_REPO_KEYCHAIN_SERVICE:-dev.craftz.homelab.argocd-ai-agent-deploy-key}"
AGENT_REPO_KEYCHAIN_ACCOUNT="${AGENT_REPO_KEYCHAIN_ACCOUNT:-craftzdev/ai-business-agent}"
AGENT_REPO_URL="${AGENT_REPO_URL:-git@github.com:craftzdev/ai-business-agent.git}"
AGENT_API_KEYCHAIN_SERVICE="${AGENT_API_KEYCHAIN_SERVICE:-dev.craftz.homelab.ai-agent-api-token}"
AGENT_API_KEYCHAIN_ACCOUNT="${AGENT_API_KEYCHAIN_ACCOUNT:-ai-business-gateway}"
AGENT_WORKER_KEYCHAIN_SERVICE="${AGENT_WORKER_KEYCHAIN_SERVICE:-dev.craftz.homelab.ai-agent-worker-token}"
AGENT_WORKER_KEYCHAIN_ACCOUNT="${AGENT_WORKER_KEYCHAIN_ACCOUNT:-ai-business-worker}"
HOMEPAGE_PVE_KEYCHAIN_SERVICE="${HOMEPAGE_PVE_KEYCHAIN_SERVICE:-dev.craftz.homelab.homepage-proxmox-token}"
HOMEPAGE_PVE_KEYCHAIN_ACCOUNT="${HOMEPAGE_PVE_KEYCHAIN_ACCOUNT:-homepage@pve!homepage}"
HARBOR_ADMIN_KEYCHAIN_SERVICE="${HARBOR_ADMIN_KEYCHAIN_SERVICE:-dev.craftz.homelab.harbor-admin}"
HARBOR_ADMIN_KEYCHAIN_ACCOUNT="${HARBOR_ADMIN_KEYCHAIN_ACCOUNT:-admin}"
HARBOR_SECRET_KEYCHAIN_SERVICE="${HARBOR_SECRET_KEYCHAIN_SERVICE:-dev.craftz.homelab.harbor-secret-key}"
HARBOR_SECRET_KEYCHAIN_ACCOUNT="${HARBOR_SECRET_KEYCHAIN_ACCOUNT:-harbor}"
HARBOR_DATABASE_KEYCHAIN_SERVICE="${HARBOR_DATABASE_KEYCHAIN_SERVICE:-dev.craftz.homelab.harbor-database}"
HARBOR_DATABASE_KEYCHAIN_ACCOUNT="${HARBOR_DATABASE_KEYCHAIN_ACCOUNT:-postgres}"
HARBOR_PULL_KEYCHAIN_SERVICE="${HARBOR_PULL_KEYCHAIN_SERVICE:-dev.craftz.homelab.harbor-k8s-pull}"
HARBOR_PULL_KEYCHAIN_ACCOUNT="${HARBOR_PULL_KEYCHAIN_ACCOUNT:-robot\$ai-business+k8s-pull}"
HARBOR_CI_CERT_KEYCHAIN_SERVICE="${HARBOR_CI_CERT_KEYCHAIN_SERVICE:-dev.craftz.homelab.harbor-ci-proxy-tls-cert}"
HARBOR_CI_KEY_KEYCHAIN_SERVICE="${HARBOR_CI_KEY_KEYCHAIN_SERVICE:-dev.craftz.homelab.harbor-ci-proxy-tls-key}"
HARBOR_CI_TLS_KEYCHAIN_ACCOUNT="${HARBOR_CI_TLS_KEYCHAIN_ACCOUNT:-172.16.40.201}"
GATUS_CF_ID_KEYCHAIN_SERVICE="${GATUS_CF_ID_KEYCHAIN_SERVICE:-dev.craftz.homelab.gatus-cloudflare-access-client-id}"
GATUS_CF_SECRET_KEYCHAIN_SERVICE="${GATUS_CF_SECRET_KEYCHAIN_SERVICE:-dev.craftz.homelab.gatus-cloudflare-access-client-secret}"
GATUS_CF_KEYCHAIN_ACCOUNT="${GATUS_CF_KEYCHAIN_ACCOUNT:-gatus}"

info() { printf '[INFO] %s\n' "$*"; }
ok() { printf '[OK]   %s\n' "$*"; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

for tool in jq kubectl security openssl; do
  command -v "${tool}" >/dev/null || die "required command not found: ${tool}"
done
[[ -s "${KUBECONFIG_PATH}" ]] || die "kubeconfig not found: ${KUBECONFIG_PATH}"

export KUBECONFIG="${KUBECONFIG_PATH}"
kubectl version -o json >/dev/null 2>&1 || die "cluster is not reachable"

if grafana_password="$(security find-generic-password \
    -s "${GRAFANA_KEYCHAIN_SERVICE}" -a "${GRAFANA_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)"; then
  info "Using the existing Grafana credential from macOS Keychain"
else
  info "Creating the initial Grafana credential in macOS Keychain"
  grafana_password="$(openssl rand -base64 32)"
  security add-generic-password -U \
    -s "${GRAFANA_KEYCHAIN_SERVICE}" \
    -a "${GRAFANA_KEYCHAIN_ACCOUNT}" \
    -w "${grafana_password}" >/dev/null
fi
[[ -n "${grafana_password}" ]] || die "Grafana password is empty"

for namespace in monitoring logging logging-audit cert-manager security \
  image-registry harbor tailscale arc-systems arc-runners argocd portal \
  ai-agent ai-worker; do
  kubectl create namespace "${namespace}" --dry-run=client -o yaml \
    | kubectl apply -f - >/dev/null
done

# The generated password is base64 text, so it is safe to place in stringData.
# It is intentionally never printed or persisted to a temporary file.
kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: grafana-admin
  namespace: monitoring
type: Opaque
stringData:
  admin-user: admin
  admin-password: "${grafana_password}"
EOF

unset grafana_password

# Talos and the self-hosted runners must trust the same stable Harbor LAN
# certificate. cert-manager must not rotate this private trust root behind the
# nodes' backs, so its certificate and key are recovered from macOS Keychain.
harbor_ci_cert_b64="$(security find-generic-password \
  -s "${HARBOR_CI_CERT_KEYCHAIN_SERVICE}" \
  -a "${HARBOR_CI_TLS_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)" \
  || die "Harbor LAN TLS certificate is missing from macOS Keychain"
harbor_ci_key_b64="$(security find-generic-password \
  -s "${HARBOR_CI_KEY_KEYCHAIN_SERVICE}" \
  -a "${HARBOR_CI_TLS_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)" \
  || die "Harbor LAN TLS private key is missing from macOS Keychain"
harbor_ci_cert="$(printf '%s' "${harbor_ci_cert_b64}" | openssl base64 -d -A)"
harbor_ci_key="$(printf '%s' "${harbor_ci_key_b64}" | openssl base64 -d -A)"

printf '%s\n' "${harbor_ci_cert}" | openssl x509 -noout -checkend 2592000 >/dev/null \
  || die "Harbor LAN TLS certificate expires within 30 days"
harbor_ci_cert_pub="$(printf '%s\n' "${harbor_ci_cert}" \
  | openssl x509 -pubkey -noout \
  | openssl pkey -pubin -outform DER 2>/dev/null \
  | openssl dgst -sha256)"
harbor_ci_key_pub="$(printf '%s\n' "${harbor_ci_key}" \
  | openssl pkey -pubout -outform DER 2>/dev/null \
  | openssl dgst -sha256)"
[[ "${harbor_ci_cert_pub}" == "${harbor_ci_key_pub}" ]] \
  || die "Harbor LAN TLS certificate and private key do not match"

kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: harbor-ci-proxy-tls
  namespace: arc-runners
type: kubernetes.io/tls
data:
  tls.crt: ${harbor_ci_cert_b64}
  tls.key: ${harbor_ci_key_b64}
EOF
ok "Stable Harbor LAN TLS certificate reconciled"
unset harbor_ci_cert harbor_ci_key harbor_ci_cert_pub harbor_ci_key_pub
unset harbor_ci_cert_b64 harbor_ci_key_b64

# Harbor credentials remain outside Git. The database Secret uses the name
# expected by the upstream chart; Argo CD ignores Secret data and only manages
# its metadata, preventing Helm re-renders from rotating a live database.
if harbor_admin_password="$(security find-generic-password \
    -s "${HARBOR_ADMIN_KEYCHAIN_SERVICE}" -a "${HARBOR_ADMIN_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)"; then
  info "Using the existing Harbor administrator credential from macOS Keychain"
else
  info "Creating the initial Harbor administrator credential in macOS Keychain"
  harbor_admin_password="$(openssl rand -base64 32)"
  security add-generic-password -U \
    -s "${HARBOR_ADMIN_KEYCHAIN_SERVICE}" \
    -a "${HARBOR_ADMIN_KEYCHAIN_ACCOUNT}" \
    -w "${harbor_admin_password}" >/dev/null
fi

if harbor_secret_key="$(security find-generic-password \
    -s "${HARBOR_SECRET_KEYCHAIN_SERVICE}" -a "${HARBOR_SECRET_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)"; then
  info "Using the existing Harbor encryption key from macOS Keychain"
else
  info "Creating the Harbor encryption key in macOS Keychain"
  harbor_secret_key="$(openssl rand -hex 8)"
  security add-generic-password -U \
    -s "${HARBOR_SECRET_KEYCHAIN_SERVICE}" \
    -a "${HARBOR_SECRET_KEYCHAIN_ACCOUNT}" \
    -w "${harbor_secret_key}" >/dev/null
fi

if harbor_database_password="$(security find-generic-password \
    -s "${HARBOR_DATABASE_KEYCHAIN_SERVICE}" -a "${HARBOR_DATABASE_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)"; then
  info "Using the existing Harbor database credential from macOS Keychain"
else
  info "Creating the Harbor database credential in macOS Keychain"
  harbor_database_password="$(openssl rand -base64 32)"
  security add-generic-password -U \
    -s "${HARBOR_DATABASE_KEYCHAIN_SERVICE}" \
    -a "${HARBOR_DATABASE_KEYCHAIN_ACCOUNT}" \
    -w "${harbor_database_password}" >/dev/null
fi

[[ -n "${harbor_admin_password}" && "${#harbor_secret_key}" == 16 \
    && -n "${harbor_database_password}" ]] \
  || die "Harbor credential material is invalid"

kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: harbor-runtime
  namespace: harbor
type: Opaque
stringData:
  HARBOR_ADMIN_PASSWORD: "${harbor_admin_password}"
  secretKey: "${harbor_secret_key}"
---
apiVersion: v1
kind: Secret
metadata:
  name: harbor-database
  namespace: harbor
type: Opaque
stringData:
  POSTGRES_PASSWORD: "${harbor_database_password}"
EOF

# Argo CD renders Helm without live-cluster `lookup`, so the upstream Harbor
# chart cannot copy the existing database password into its generated
# harbor-core Secret. Patch only that map key after the chart has created the
# rest of the Secret. Server-side apply preserves every chart-owned key.
if kubectl -n harbor get secret harbor-core >/dev/null 2>&1; then
  current_harbor_client_password="$(kubectl -n harbor get secret harbor-core \
    -o jsonpath='{.data.POSTGRESQL_PASSWORD}' | openssl base64 -d -A)"
  if [[ "${current_harbor_client_password}" != "${harbor_database_password}" ]]; then
    kubectl apply --server-side --field-manager=harbor-bootstrap -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: harbor-core
  namespace: harbor
type: Opaque
stringData:
  POSTGRESQL_PASSWORD: "${harbor_database_password}"
EOF
    ok "Harbor database client credential reconciled"
  else
    info "Harbor database client credential is already current"
  fi
  unset current_harbor_client_password
else
  info "Harbor chart Secrets are not present yet; reconcile after its first render"
fi

if kubectl -n harbor get secret harbor-exporter >/dev/null 2>&1; then
  current_harbor_exporter_password="$(kubectl -n harbor get secret harbor-exporter \
    -o jsonpath='{.data.HARBOR_DATABASE_PASSWORD}' | openssl base64 -d -A)"
  if [[ "${current_harbor_exporter_password}" != "${harbor_database_password}" ]]; then
    kubectl apply --server-side --field-manager=harbor-bootstrap -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: harbor-exporter
  namespace: harbor
type: Opaque
stringData:
  HARBOR_DATABASE_PASSWORD: "${harbor_database_password}"
EOF
    ok "Harbor exporter database credential reconciled"
  else
    info "Harbor exporter database credential is already current"
  fi
  unset current_harbor_exporter_password
fi

unset harbor_admin_password harbor_secret_key harbor_database_password

# Workload Pods get a read-only Harbor robot account. The CI publisher uses a
# different push-capable credential that is never copied into Kubernetes.
harbor_pull_password="$(security find-generic-password \
  -s "${HARBOR_PULL_KEYCHAIN_SERVICE}" -a "${HARBOR_PULL_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)" \
  || die "Harbor pull credential is missing from macOS Keychain"
harbor_pull_auth="$(printf '%s:%s' "${HARBOR_PULL_KEYCHAIN_ACCOUNT}" \
  "${harbor_pull_password}" | openssl base64 -A)"
harbor_pull_config="$(jq -cn --arg auth "${harbor_pull_auth}" \
  '{auths: {
    "harbor.tailb6c7d.ts.net": {auth: $auth},
    "172.16.40.201:5000": {auth: $auth}
  }}')"
harbor_pull_config_b64="$(printf '%s' "${harbor_pull_config}" | openssl base64 -A)"

for namespace in ai-agent ai-worker; do
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: harbor-pull
  namespace: ${namespace}
type: kubernetes.io/dockerconfigjson
data:
  .dockerconfigjson: ${harbor_pull_config_b64}
EOF
done
ok "Harbor read-only pull credential reconciled"
unset harbor_pull_password harbor_pull_auth harbor_pull_config harbor_pull_config_b64

# Gateway-facing and Worker-facing tokens are deliberately independent. They
# are generated once and retained in Keychain; changing one trust boundary does
# not force credentials from the other boundary to be exposed or reused.
if agent_api_token="$(security find-generic-password \
    -s "${AGENT_API_KEYCHAIN_SERVICE}" -a "${AGENT_API_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)"; then
  info "Using the existing Agent API token from macOS Keychain"
else
  info "Creating the Agent API token in macOS Keychain"
  agent_api_token="$(openssl rand -hex 32)"
  security add-generic-password -U \
    -s "${AGENT_API_KEYCHAIN_SERVICE}" \
    -a "${AGENT_API_KEYCHAIN_ACCOUNT}" \
    -w "${agent_api_token}" >/dev/null
fi

if agent_worker_token="$(security find-generic-password \
    -s "${AGENT_WORKER_KEYCHAIN_SERVICE}" -a "${AGENT_WORKER_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)"; then
  info "Using the existing Agent-to-Worker token from macOS Keychain"
else
  info "Creating the Agent-to-Worker token in macOS Keychain"
  agent_worker_token="$(openssl rand -hex 32)"
  security add-generic-password -U \
    -s "${AGENT_WORKER_KEYCHAIN_SERVICE}" \
    -a "${AGENT_WORKER_KEYCHAIN_ACCOUNT}" \
    -w "${agent_worker_token}" >/dev/null
fi
[[ -n "${agent_api_token}" && -n "${agent_worker_token}" ]] \
  || die "Agent runtime token is empty"

kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: ai-business-agent-runtime
  namespace: ai-agent
type: Opaque
stringData:
  agent-api-token: "${agent_api_token}"
  worker-api-token: "${agent_worker_token}"
EOF
unset agent_api_token agent_worker_token

# MinIO root and Loki's dedicated S3 credential are independent. Loki never
# receives the MinIO administrator password. Values are generated only once and
# kept in the macOS Keychain so a complete cluster rebuild produces the same
# application credentials without committing them to Git.
if minio_root_password="$(security find-generic-password \
    -s "${MINIO_KEYCHAIN_SERVICE}" -a "${MINIO_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)"; then
  info "Using the existing MinIO root credential from macOS Keychain"
else
  info "Creating the initial MinIO root credential in macOS Keychain"
  minio_root_password="$(openssl rand -base64 32)"
  security add-generic-password -U \
    -s "${MINIO_KEYCHAIN_SERVICE}" \
    -a "${MINIO_KEYCHAIN_ACCOUNT}" \
    -w "${minio_root_password}" >/dev/null
fi
[[ -n "${minio_root_password}" ]] || die "MinIO root password is empty"

if loki_s3_secret="$(security find-generic-password \
    -s "${LOKI_S3_KEYCHAIN_SERVICE}" -a "${LOKI_S3_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)"; then
  info "Using the existing Loki S3 credential from macOS Keychain"
else
  info "Creating the initial Loki S3 credential in macOS Keychain"
  loki_s3_secret="$(openssl rand -base64 32)"
  security add-generic-password -U \
    -s "${LOKI_S3_KEYCHAIN_SERVICE}" \
    -a "${LOKI_S3_KEYCHAIN_ACCOUNT}" \
    -w "${loki_s3_secret}" >/dev/null
fi
[[ -n "${loki_s3_secret}" ]] || die "Loki S3 secret is empty"

kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: minio-root-credentials
  namespace: logging
type: Opaque
stringData:
  root-user: minio-root
  root-password: "${minio_root_password}"
---
apiVersion: v1
kind: Secret
metadata:
  name: loki-s3-credentials
  namespace: logging
type: Opaque
stringData:
  AWS_ACCESS_KEY_ID: loki
  AWS_SECRET_ACCESS_KEY: "${loki_s3_secret}"
EOF

unset minio_root_password loki_s3_secret

# Homepage gets a Proxmox API token with PVEAuditor only. Token creation and
# ACL reconciliation are handled by reconcile-homepage-proxmox-token.sh; this
# bootstrap step only copies the existing secret from Keychain into Kubernetes.
if homepage_pve_token="$(security find-generic-password \
    -s "${HOMEPAGE_PVE_KEYCHAIN_SERVICE}" \
    -a "${HOMEPAGE_PVE_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)"; then
  [[ -n "${homepage_pve_token}" ]] || die "Homepage Proxmox token is empty"
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: homepage-integrations
  namespace: portal
type: Opaque
stringData:
  HOMEPAGE_VAR_PROXMOX_USERNAME: "${HOMEPAGE_PVE_KEYCHAIN_ACCOUNT}"
  HOMEPAGE_VAR_PROXMOX_TOKEN: "${homepage_pve_token}"
EOF
  unset homepage_pve_token
  ok "Homepage Proxmox credential is present"
else
  info "Homepage Proxmox token is absent from Keychain; run reconcile-homepage-proxmox-token.sh"
fi

# Gatus probes the published Cloudflare hostnames from inside the cluster, so it
# needs an Access service token of its own. It is deliberately not the
# saas-worker token: revoking one must never interrupt the other, and the Access
# log stays able to tell monitoring traffic apart from real traffic.
#
# The value reaches Kubernetes through a heredoc on this process's stdin. It is
# never a command argument and never touches a temporary file.
if gatus_cf_client_id="$(security find-generic-password \
    -s "${GATUS_CF_ID_KEYCHAIN_SERVICE}" \
    -a "${GATUS_CF_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)" \
  && gatus_cf_client_secret="$(security find-generic-password \
    -s "${GATUS_CF_SECRET_KEYCHAIN_SERVICE}" \
    -a "${GATUS_CF_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)"; then
  [[ -n "${gatus_cf_client_id}" ]] || die "Gatus Access client ID is empty"
  [[ -n "${gatus_cf_client_secret}" ]] || die "Gatus Access client secret is empty"
  # Cloudflare service token IDs always carry this suffix. Catching a wrong
  # Keychain entry here is far cheaper than debugging silent 403s later.
  [[ "${gatus_cf_client_id}" == *.access ]] \
    || die "Gatus Access client ID does not look like a Cloudflare service token"
  kubectl create namespace status --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: gatus-cloudflare-access
  namespace: status
type: Opaque
stringData:
  CF_ACCESS_CLIENT_ID: "${gatus_cf_client_id}"
  CF_ACCESS_CLIENT_SECRET: "${gatus_cf_client_secret}"
EOF
  unset gatus_cf_client_id gatus_cf_client_secret
  ok "Gatus Cloudflare Access credential is present"
else
  unset gatus_cf_client_id gatus_cf_client_secret 2>/dev/null || true
  info "Gatus Cloudflare Access token is absent from Keychain; create it with tofu -chdir=tofu/20-cloudflare apply and store the outputs"
fi

# The private Worker repository uses a repository-scoped, read-only GitHub
# Deploy Key. The Keychain value is base64 so multiline OpenSSH key material
# survives an exact round trip through the `security` CLI.
if worker_repo_key_b64="$(security find-generic-password \
    -s "${WORKER_REPO_KEYCHAIN_SERVICE}" \
    -a "${WORKER_REPO_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)"; then
  worker_repo_key="$(printf '%s' "${worker_repo_key_b64}" | openssl base64 -d -A)"
  [[ "${worker_repo_key}" == '-----BEGIN OPENSSH PRIVATE KEY-----'* ]] \
    || die "Worker repository Deploy Key in Keychain is invalid"
  indented_worker_repo_key="$(printf '%s\n' "${worker_repo_key}" | sed 's/^/    /')"
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: ai-business-worker-repository
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
type: Opaque
stringData:
  type: git
  url: ${WORKER_REPO_URL}
  sshPrivateKey: |
${indented_worker_repo_key}
EOF
  unset worker_repo_key_b64 worker_repo_key indented_worker_repo_key
  ok "Argo CD Worker repository credential is present"
else
  info "Worker repository Deploy Key is absent from Keychain; recovery restore must provide it"
fi

# The Agent repository has an independent Deploy Key so revocation never
# broadens or interrupts access to the Worker repository.
if agent_repo_key_b64="$(security find-generic-password \
    -s "${AGENT_REPO_KEYCHAIN_SERVICE}" \
    -a "${AGENT_REPO_KEYCHAIN_ACCOUNT}" -w 2>/dev/null)"; then
  agent_repo_key="$(printf '%s' "${agent_repo_key_b64}" | openssl base64 -d -A)"
  [[ "${agent_repo_key}" == '-----BEGIN OPENSSH PRIVATE KEY-----'* ]] \
    || die "Agent repository Deploy Key in Keychain is invalid"
  indented_agent_repo_key="$(printf '%s\n' "${agent_repo_key}" | sed 's/^/    /')"
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: ai-business-agent-repository
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
type: Opaque
stringData:
  type: git
  url: ${AGENT_REPO_URL}
  sshPrivateKey: |
${indented_agent_repo_key}
EOF
  unset agent_repo_key_b64 agent_repo_key indented_agent_repo_key
  ok "Argo CD Agent repository credential is present"
else
  info "Agent repository Deploy Key is absent from Keychain; recovery restore must provide it"
fi

ok "Cluster bootstrap secrets are present"
