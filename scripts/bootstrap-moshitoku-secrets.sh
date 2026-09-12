#!/usr/bin/env bash
# ===========================================================================
# moshitoku / moshitoku-scraper の非 Git シークレットを materialize する。
#
# 情報源は macOS Keychain である。無ければ生成して Keychain へ保存し、
# 以後の再構築で同じ値が復元されるようにする（クラスタを作り直しても
# DB のパスワードが変わらないことが必要なため）。
#
# ⚠️ CloudNativePG が作る Secret は namespace をまたげない。スクレイパーは
#    別 namespace から DB へ接続するため、moshitoku から moshitoku-scraper
#    へ複製する。複製である以上、元が変わったら再実行が必要になる。
#    再構築スクリプトはその「ずれ」を検知して止まる。
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
KUBECONFIG_PATH="${KUBECONFIG:-${REPO_ROOT}/_out/kubeconfig}"

APP_NAMESPACE=moshitoku
SCRAPER_NAMESPACE=moshitoku-scraper
LOGGING_NAMESPACE=logging

MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-moshitoku-cnpg}"

info() { printf '[INFO] %s\n' "$*"; }
ok()   { printf '[OK]   %s\n' "$*"; }
die()  { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

for tool in kubectl security openssl; do
  command -v "${tool}" >/dev/null || die "required command not found: ${tool}"
done
[[ -s "${KUBECONFIG_PATH}" ]] || die "kubeconfig not found: ${KUBECONFIG_PATH}"
export KUBECONFIG="${KUBECONFIG_PATH}"
kubectl version -o json >/dev/null 2>&1 || die "cluster is not reachable"

read_keychain() { security find-generic-password -s "$1" -w 2>/dev/null; }

# 生成して保存する。値は引数で渡す（security は標準入力を読まない）。
ensure_generated_keychain_secret() {
  local service=$1 value
  value="$(read_keychain "${service}" || true)"
  if [[ -z "${value}" ]]; then
    value="$(openssl rand -hex 32)"
    security add-generic-password -U -a craftz -s "${service}" -w "${value}" >/dev/null
  fi
  printf '%s' "${value}"
}

ensure_namespace() {
  kubectl create namespace "$1" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

ensure_namespace "${APP_NAMESPACE}"
ensure_namespace "${SCRAPER_NAMESPACE}"

# ---------------------------------------------------------------------------
# 生成する値
# ---------------------------------------------------------------------------
db_password="$(ensure_generated_keychain_secret dev.craftz.moshitoku.db-password)"
django_secret="$(ensure_generated_keychain_secret dev.craftz.moshitoku.django-secret-key)"
minio_secret="$(ensure_generated_keychain_secret dev.craftz.moshitoku.minio-secret-key)"

# ---------------------------------------------------------------------------
# moshitoku namespace
#
# CloudNativePG は initdb.secret.name が指す Secret の値で owner を作る。
# クラスタを作り直しても同じパスワードでなければ、既存のデータディレクトリを
# 復元したときに認証できなくなる。だから Keychain を情報源にしている。
# ---------------------------------------------------------------------------
kubectl -n "${APP_NAMESPACE}" create secret generic moshitoku-db-owner \
  --type=kubernetes.io/basic-auth \
  --from-literal=username=moshitoku \
  --from-literal=password="${db_password}" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

kubectl -n "${APP_NAMESPACE}" create secret generic moshitoku-runtime \
  --from-literal=django-secret-key="${django_secret}" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# バックアップ先の MinIO 資格情報。CNPG の ObjectStore が参照するため
# moshitoku namespace に、MinIO 側のユーザー作成 Job が参照するため
# logging namespace にも同じものを置く。
for namespace in "${APP_NAMESPACE}" "${LOGGING_NAMESPACE}"; do
  kubectl -n "${namespace}" create secret generic moshitoku-s3-credentials \
    --from-literal=ACCESS_KEY_ID="${MINIO_ACCESS_KEY}" \
    --from-literal=ACCESS_SECRET_KEY="${minio_secret}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done

ok "moshitoku database, runtime, and backup credentials are present"

# ---------------------------------------------------------------------------
# moshitoku-scraper namespace
#
# webshare-api-key はプロキシ取得に必須で、無いと CronJob は起動直後に落ちる。
# 自動生成できる種類の値ではない（外部サービスが発行する）ので、Keychain に
# 無ければ作らずに警告する。黙って空の Secret を作ると、原因が分かりにくい
# 起動失敗になる。
#
# discord-webhook-url は通知先であり、マニフェスト側で optional: true に
# なっている。無ければ通知しないだけで動作する。
# ---------------------------------------------------------------------------
scraper_args=()
webshare_key="$(read_keychain dev.craftz.moshitoku-scraper.webshare-api-key || true)"
if [[ -n "${webshare_key}" ]]; then
  scraper_args+=(--from-literal=webshare-api-key="${webshare_key}")
else
  info "webshare API key is absent from Keychain; scraper CronJobs will fail to start"
  info "  security add-generic-password -U -a craftz -s dev.craftz.moshitoku-scraper.webshare-api-key -w '<key>'"
fi

discord_url="$(read_keychain dev.craftz.moshitoku-scraper.discord-webhook-url || true)"
if [[ -n "${discord_url}" ]]; then
  scraper_args+=(--from-literal=discord-webhook-url="${discord_url}")
else
  info "Discord webhook is absent from Keychain; scraper runs without notifications"
fi

if [[ "${#scraper_args[@]}" -gt 0 ]]; then
  kubectl -n "${SCRAPER_NAMESPACE}" create secret generic moshitoku-scraper-runtime \
    "${scraper_args[@]}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  ok "moshitoku-scraper runtime credentials are present"
fi

# ---------------------------------------------------------------------------
# namespace をまたぐ複製
#
# DB_SSLMODE=verify-full のため、CA が無い・古いと接続できない。CA は
# CloudNativePG が moshitoku namespace に作るので、そこから複製する。
# ---------------------------------------------------------------------------
kubectl -n "${SCRAPER_NAMESPACE}" create secret generic moshitoku-db-owner \
  --type=kubernetes.io/basic-auth \
  --from-literal=username=moshitoku \
  --from-literal=password="${db_password}" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

if ca_crt="$(kubectl -n "${APP_NAMESPACE}" get secret moshitoku-postgres-ca \
    -o jsonpath='{.data.ca\.crt}' 2>/dev/null)" && [[ -n "${ca_crt}" ]]; then
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: moshitoku-postgres-ca
  namespace: ${SCRAPER_NAMESPACE}
  annotations:
    homelab/replicated-from: ${APP_NAMESPACE}/moshitoku-postgres-ca
    homelab/replicated-by: scripts/bootstrap-moshitoku-secrets.sh
type: Opaque
data:
  ca.crt: ${ca_crt}
EOF
  ok "PostgreSQL CA replicated into ${SCRAPER_NAMESPACE}"
else
  info "moshitoku-postgres-ca does not exist yet; re-run after CloudNativePG creates the cluster"
fi

unset db_password django_secret minio_secret webshare_key discord_url ca_crt
ok "moshitoku secrets are reconciled"
