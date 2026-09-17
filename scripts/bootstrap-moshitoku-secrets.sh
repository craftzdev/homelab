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
# Secret は argv に載せず標準入力から渡す（lib/secrets.sh の apply_secret）。
# shellcheck source=scripts/lib/secrets.sh
source "${SCRIPT_DIR}/lib/secrets.sh"


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

# namespace はアプリ側リポジトリが Pod Security ラベル付きで所有する。ここでは
# 「まだ無いときだけ」作る。既存へ apply すると last-applied-configuration を
# 後付けして所有権が曖昧になり、Argo CD と差分を取り合う下地になる。
ensure_namespace() {
  kubectl get namespace "$1" >/dev/null 2>&1 && return
  kubectl create namespace "$1" >/dev/null
}

ensure_namespace "${APP_NAMESPACE}"
ensure_namespace "${SCRAPER_NAMESPACE}"

# ---------------------------------------------------------------------------
# 生成する値
# ---------------------------------------------------------------------------
db_password="$(ensure_generated_keychain_secret dev.craftz.moshitoku.db-password)"
django_secret="$(ensure_generated_keychain_secret dev.craftz.moshitoku.django-secret-key)"
minio_secret="$(ensure_generated_keychain_secret dev.craftz.moshitoku.minio-secret-key)"
ingest_token="$(ensure_generated_keychain_secret dev.craftz.moshitoku.ingest-api-token)"

# /_analytics/api/send のレート制限キーを作る HMAC 鍵。
#
# 空だと _rate_limit_key() が鍵なしで "IP:分" をハッシュするため、
# キャッシュのダンプや監視の出力から総当たりで接続元IPが復元できる。
# 匿名化の意味が無くなるので、必ず値を入れる。
#
# ⚠️ 値が変わるとレート制限の窓が一度だけリセットされる。
#    影響はその 1 分だけなので、失った場合は作り直してよい。
analytics_rate_hash_key="$(ensure_generated_keychain_secret dev.craftz.moshitoku.analytics-rate-hash-key)"

# ---------------------------------------------------------------------------
# 内部取り込み API が許可する Source
#
# 正本は moshitoku の Site.source_key である。ここはその写しであり、
# 増えたときに追従しないと、その Source を api モードへ切り替えた時点で
# 403（SOURCE_FORBIDDEN）になる。runbook の shadow 段階で必ず露見する。
#
# ⚠️ ここは「このスクレイパーが書いてよい Source」の一覧であり、
#    「いま内部API経由で送る Source」ではない。後者は scraper 側の
#    INGEST_MODE_<SOURCE> が決める。混ぜると、切り替えのたびに
#    資格情報を触ることになる。
# ---------------------------------------------------------------------------
INGEST_SOURCE_KEYS=(
  amefri chobirich djob ecnavi fruitmail gendama getmoney gmo hapitas
  moppy nifty pointi poney powl rebates trima warau
)

# ---------------------------------------------------------------------------
# moshitoku namespace
#
# CloudNativePG は initdb.secret.name が指す Secret の値で owner を作る。
# クラスタを作り直しても同じパスワードでなければ、既存のデータディレクトリを
# 復元したときに認証できなくなる。だから Keychain を情報源にしている。
# ---------------------------------------------------------------------------
apply_secret "${APP_NAMESPACE}" moshitoku-db-owner kubernetes.io/basic-auth \
  "username=moshitoku" \
  "password=${db_password}"

# 内部取り込み API の token 設定。{token: [許可する source_key, ...]} の JSON。
# Web の Deployment が INGEST_TOKEN_CONFIG として読む。
ingest_token_config="$(
  printf '%s\n' "${INGEST_SOURCE_KEYS[@]}" \
    | python3 -c 'import json,sys; print(json.dumps({sys.argv[1]: sys.stdin.read().split()}))' \
      "${ingest_token}"
)"

apply_secret "${APP_NAMESPACE}" moshitoku-runtime "" \
  "django-secret-key=${django_secret}" \
  "ingest-token-config=${ingest_token_config}" \
  "analytics-rate-hash-key=${analytics_rate_hash_key}"

# バックアップ先の MinIO 資格情報。CNPG の ObjectStore が参照するため
# moshitoku namespace に、MinIO 側のユーザー作成 Job が参照するため
# logging namespace にも同じものを置く。
for namespace in "${APP_NAMESPACE}" "${LOGGING_NAMESPACE}"; do
  apply_secret "${namespace}" moshitoku-s3-credentials "" \
    "ACCESS_KEY_ID=${MINIO_ACCESS_KEY}" \
    "ACCESS_SECRET_KEY=${minio_secret}"
done

ok "moshitoku database, runtime, and backup credentials are present"

# ---------------------------------------------------------------------------
# moshitoku-scraper の外部発行シークレットはここで作らない
#
# webshare-api-key と discord-webhook-url は外部サービスが発行する静的な値で
# あり、SOPS で Git 管理して Argo CD に配送させている
# （kubernetes/infra/moshitoku-secrets/）。理由は暗号強度ではなく配送で、
# このスクリプトの実行を人が忘れても CronJob が動くようにするためである。
#
# ⚠️ ここで moshitoku-scraper-runtime を作ってはならない。同じ Secret を
#    スクリプトと Argo CD の両方が書くと、selfHeal と取り合って値が
#    往復する。生成値（DB パスワード等）と複製値だけがこのスクリプトの担当。
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 内部取り込み API の token（送信側）
#
# 自前生成の値なので Keychain が情報源であり、SOPS には入れない。
# 上の警告のとおり moshitoku-scraper-runtime へは足せないため、別の
# Secret にする。受け取り側（moshitoku-runtime の ingest-token-config）と
# 同じ値でなければ 401 になる。ここで同時に配るのはそのためである。
# ---------------------------------------------------------------------------
apply_secret "${SCRAPER_NAMESPACE}" moshitoku-scraper-ingest "" \
  "ingest-api-token=${ingest_token}"

ok "internal ingest API token is present on both sides"

# ---------------------------------------------------------------------------
# スクレイパーへ DB 資格情報を渡さない
#
# 収集結果は内部取り込み API へ送る。スクレイパーは PostgreSQL へ接続せず、
# 接続に使っていた db-owner と CA の複製も要らなくなった（設計 §20.2 M4
# 「全Source移行後に旧DB資格情報を失効させる」）。
#
# 既に配ってしまったものは、このスクリプトでは消さない。消す操作は
# 一度きりで、繰り返し実行する reconcile の役目ではない。残っていれば
# 次の一行で消せる。
#
#   kubectl -n moshitoku-scraper delete secret moshitoku-db-owner moshitoku-postgres-ca
#
# ⚠️ 順序がある。スクレイパー側の参照（DB_PASSWORD の env、postgres-ca の
#    マウント、CloudNativePG への egress）を先に外し、moshitoku 側の
#    ingress を閉じてから消すこと。逆にすると、旧イメージで動いている
#    収集が DB へ繋げなくなる。
# ---------------------------------------------------------------------------

unset db_password django_secret minio_secret webshare_key discord_url
unset ingest_token ingest_token_config
ok "moshitoku secrets are reconciled"
