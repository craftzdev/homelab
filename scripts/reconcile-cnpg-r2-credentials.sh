#!/usr/bin/env bash
# CloudNativePG のバックアップ先（Cloudflare R2）の認証情報を冪等に投入する。
#
# ---------------------------------------------------------------------------
# なぜ R2 へ移すのか
# ---------------------------------------------------------------------------
# CNPG のバックアップはクラスタ内 MinIO に置かれていた。その MinIO は
# Longhorn の上にあり、**守るべき対象と同じストレージ**に載っている。
# Longhorn が論理破損すれば、DB とそのバックアップが同時に消える。
# ADR-0008 がまさにこれを「旧構成の minio-for-velero と同じ問題」として
# 警告していた。
#
# 2026-09-16 の実測では、Longhorn の実データ 46.2 GiB のうち
# 代替不能なのは 231 MB だけだった（moshitoku-postgres 200MB /
# umami-postgres 31MB）。残りは保持 15 日の監視データと再ビルド可能な
# コンテナイメージである。この 231 MB をクラスタ外へ出すのが目的で、
# R2 の無料枠（10 GB）に十分収まる。
#
# 詳細は docs/storage-migration-2026-09-13.md を参照。
#
# ---------------------------------------------------------------------------
# 事前に用意するもの
# ---------------------------------------------------------------------------
#   1. R2 バケット 2 つ
#        homelab-umami-postgres
#        homelab-moshitoku-postgres
#   2. 上記 2 バケットに絞った R2 API トークン（Object Read & Write）
#   3. 認証情報の渡し方（どちらか）
#
#      a) macOS Keychain — このリポジトリの既定の流儀
#           security add-generic-password -U -a "$USER" \
#             -s dev.craftz.homelab.r2-cnpg-access-key-id     -w '<Access Key ID>'
#           security add-generic-password -U -a "$USER" \
#             -s dev.craftz.homelab.r2-cnpg-secret-access-key -w '<Secret Access Key>'
#
#      b) 環境変数 — 1Password 等、別の保管庫から渡す場合
#           R2_ACCESS_KEY_ID="$(op read 'op://<vault>/<item>/access key id')" \
#           R2_SECRET_ACCESS_KEY="$(op read 'op://<vault>/<item>/secret access key')" \
#             ./scripts/reconcile-cnpg-r2-credentials.sh --apply
#
#      環境変数があればそちらを優先する。Keychain は無くてもよい。
#
# ⚠️ Git には平文・暗号文とも置かない。既存の
#    ai-business-platform/infra/scripts/bootstrap-umami-secrets.sh と同じ流儀。
#
# 📌 このリポジトリの秘密はすべて macOS Keychain に置かれている。
#    1Password へ寄せるなら、この 1 つだけでなく全体の方針として
#    決めた方がよい（保管場所が二箇所に分かれるのが一番まずい）。
#
# ⚠️ 既存の umami-s3-credentials / moshitoku-s3-credentials は **MinIO 用**で、
#    別のスクリプトが管理している。上書きすると次回の実行で MinIO の値へ
#    戻されるため、R2 用は別名の Secret を使う。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
KUBECONFIG_PATH="${KUBECONFIG_PATH:-${REPO_ROOT}/_out/kubeconfig}"
KEYCHAIN_ACCESS_KEY="${KEYCHAIN_ACCESS_KEY:-dev.craftz.homelab.r2-cnpg-access-key-id}"
KEYCHAIN_SECRET_KEY="${KEYCHAIN_SECRET_KEY:-dev.craftz.homelab.r2-cnpg-secret-access-key}"
APPLY=false

# namespace:secret-name
TARGETS=("analytics:umami-r2-credentials" "moshitoku:moshitoku-r2-credentials")

readonly C_RED=$'\033[0;31m' C_GREEN=$'\033[0;32m' C_YELLOW=$'\033[0;33m'
readonly C_BLUE=$'\033[0;34m' C_RESET=$'\033[0m'
info() { printf '%s[INFO]%s  %s\n' "${C_BLUE}" "${C_RESET}" "$*"; }
ok()   { printf '%s[OK]%s    %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
warn() { printf '%s[WARN]%s  %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die()  { printf '%s[ERROR]%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
使い方: reconcile-cnpg-r2-credentials.sh [--apply]

引数なしでは前提条件だけを確認する（Keychain に値があるか、
対象 namespace が存在するか）。--apply で Secret を投入する。

環境変数:
  R2_ACCESS_KEY_ID      指定すると Keychain より優先される
  R2_SECRET_ACCESS_KEY  同上（1Password 等から渡す場合に使う）
  KUBECONFIG_PATH       既定: _out/kubeconfig
  KEYCHAIN_ACCESS_KEY   既定: dev.craftz.homelab.r2-cnpg-access-key-id
  KEYCHAIN_SECRET_KEY   既定: dev.craftz.homelab.r2-cnpg-secret-access-key

投入先:
  analytics/umami-r2-credentials
  moshitoku/moshitoku-r2-credentials
  いずれも キー ACCESS_KEY_ID / ACCESS_SECRET_KEY
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "不明な引数: $1" ;;
  esac
done

command -v kubectl >/dev/null || die "kubectl が見つかりません"
[[ -s "${KUBECONFIG_PATH}" ]] || die "kubeconfig が見つかりません: ${KUBECONFIG_PATH}"

k() { kubectl --kubeconfig "${KUBECONFIG_PATH}" "$@"; }

read_keychain() {
  command -v security >/dev/null || return 0
  security find-generic-password -s "$1" -w 2>/dev/null || true
}

# 環境変数があればそちらを使う。無ければ Keychain を見る。
access_key="${R2_ACCESS_KEY_ID:-}"
secret_key="${R2_SECRET_ACCESS_KEY:-}"
source_label=環境変数
if [[ -z "${access_key}" || -z "${secret_key}" ]]; then
  access_key="${access_key:-$(read_keychain "${KEYCHAIN_ACCESS_KEY}")}"
  secret_key="${secret_key:-$(read_keychain "${KEYCHAIN_SECRET_KEY}")}"
  source_label=Keychain
fi

# 値そのものは出力しない。取得元と長さだけを報告する。
if [[ -z "${access_key}" || -z "${secret_key}" ]]; then
  die "認証情報を取得できません。--help の「認証情報の渡し方」を参照してください"
fi
ok "${source_label} から取得しました（access_key ${#access_key} 文字 / secret_key ${#secret_key} 文字）"

for target in "${TARGETS[@]}"; do
  namespace="${target%%:*}"
  secret="${target##*:}"
  k get namespace "${namespace}" >/dev/null 2>&1 \
    || die "namespace が存在しません: ${namespace}"
  if k -n "${namespace}" get secret "${secret}" >/dev/null 2>&1; then
    info "${namespace}/${secret}: 既存（更新します）"
  else
    info "${namespace}/${secret}: 新規"
  fi
done

if [[ "${APPLY}" != true ]]; then
  warn "確認のみです。反映するには --apply を指定してください"
  exit 0
fi

for target in "${TARGETS[@]}"; do
  namespace="${target%%:*}"
  secret="${target##*:}"
  k -n "${namespace}" create secret generic "${secret}" \
    --from-literal=ACCESS_KEY_ID="${access_key}" \
    --from-literal=ACCESS_SECRET_KEY="${secret_key}" \
    --dry-run=client -o yaml \
    | k apply -f - >/dev/null
  k -n "${namespace}" get secret "${secret}" -o jsonpath='{.data.ACCESS_KEY_ID}' >/dev/null \
    || die "${namespace}/${secret} の反映を確認できません"
  ok "${namespace}/${secret} を投入しました"
done

unset access_key secret_key

cat <<'EOS'

次の手順:
  1. ObjectStore を R2 へ向ける（この PR のマニフェスト変更）
  2. ArgoCD が同期したら、手動でバックアップを 1 回実行して疎通を確認する
       kubectl -n moshitoku create -f - <<'YAML'
       apiVersion: postgresql.cnpg.io/v1
       kind: Backup
       metadata:
         generateName: r2-verify-
       spec:
         cluster:
           name: moshitoku-postgres
         method: plugin
         pluginConfiguration:
           name: barman-cloud.cloudnative-pg.io
       YAML
  3. CNPGNoBackupEver / CNPGBackupFailing が鳴らないことを確認する
  4. 旧 MinIO のバケットは、R2 で復元確認が取れるまで消さないこと
EOS
