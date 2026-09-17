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
# なぜトークンを 2 つに分けるのか
# ---------------------------------------------------------------------------
# 機能上は 1 つを両バケットにスコープすれば足りる。分けているのは
# **被害範囲を切るため**である。トークンはそれぞれ別の namespace の
# Secret に入るので、共有すると片方の namespace が侵害されたときに
# もう片方のバックアップまで削除できてしまう。
#
# ⚠️ Object Read & Write には削除権限が含まれ、barman はこれを必要とする
#    （retentionPolicy 30d の実行に削除が要る）。したがって書き込み専用には
#    できず、侵害されたクラスタは R2 上のバックアップを消せる。
#    ADR-0008 の S6（ランサムウェア）を完全には塞がない。
#    バケットロックで塞げることは確認したが、PBS が別筐体の砦として
#    既にあるため 2026-09-17 に見送りを決定した。判断の理由と
#    再検討の条件は ADR-0012 に、手順は
#    docs/r2-bucket-lock-2026-09-16.md にある。
#
# ---------------------------------------------------------------------------
# 認証情報の取得元
# ---------------------------------------------------------------------------
# 1Password（`op` CLI）から読む。**このリポジトリで 1Password を使う
# 最初のスクリプトである。** 他の秘密はすべて macOS Keychain にある。
# 全体を 1Password へ寄せる移行は別途進める前提で、ここでは op を直接使う。
#
# ⚠️ フィールドのラベルは ASCII にすること。`op read` の参照は
#    日本語ラベルを受け付けない（invalid character in secret reference）。
#
# 事前に必要なもの:
#   - R2 バケット 2 つ（homelab-umami-postgres / homelab-moshitoku-postgres）
#   - バケットごとにスコープした **アカウント** API トークン 2 つ
#     （ユーザートークンは、そのユーザーの権限が変わると黙って失効する）
#   - 1Password のアイテム 2 つ。それぞれ ACCESS_KEY_ID と
#     SECRET_ACCESS_KEY というラベルのフィールドを持つこと
#
# ⚠️ 既存の umami-s3-credentials / moshitoku-s3-credentials は **MinIO 用**で、
#    別のスクリプトが管理している。上書きすると次回の実行で MinIO の値へ
#    戻されるため、R2 用は別名の Secret を使う。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
KUBECONFIG_PATH="${KUBECONFIG_PATH:-${REPO_ROOT}/_out/kubeconfig}"
OP_VAULT="${OP_VAULT:-Personal}"
APPLY=false

# namespace | secret 名 | 1Password のアイテム名
TARGETS=(
  "analytics|umami-r2-credentials|Homelab Umami Cloudflare R2"
  "moshitoku|moshitoku-r2-credentials|Homelab moshitoku Cloudflare R2"
)

readonly C_RED=$'\033[0;31m' C_GREEN=$'\033[0;32m' C_YELLOW=$'\033[0;33m'
readonly C_BLUE=$'\033[0;34m' C_RESET=$'\033[0m'
info() { printf '%s[INFO]%s  %s\n' "${C_BLUE}" "${C_RESET}" "$*"; }
ok()   { printf '%s[OK]%s    %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
warn() { printf '%s[WARN]%s  %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die()  { printf '%s[ERROR]%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
使い方: reconcile-cnpg-r2-credentials.sh [--apply]

引数なしでは前提条件だけを確認する（1Password から読めるか、
対象 namespace が存在するか）。--apply で Secret を投入する。

環境変数:
  OP_VAULT         1Password の vault 名（既定: Personal）
  KUBECONFIG_PATH  既定: _out/kubeconfig

投入先:
  analytics/umami-r2-credentials       <- "Homelab Umami Cloudflare R2"
  moshitoku/moshitoku-r2-credentials   <- "Homelab moshitoku Cloudflare R2"
  いずれも キー ACCESS_KEY_ID / ACCESS_SECRET_KEY

値そのものは一切出力しない（取得の可否と文字数だけを報告する）。
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
command -v op >/dev/null || die "1Password CLI (op) が見つかりません"
[[ -s "${KUBECONFIG_PATH}" ]] || die "kubeconfig が見つかりません: ${KUBECONFIG_PATH}"

op account list >/dev/null 2>&1 || die "op にサインインしていません（op signin）"

k() { kubectl --kubeconfig "${KUBECONFIG_PATH}" "$@"; }

for target in "${TARGETS[@]}"; do
  IFS='|' read -r namespace secret item <<<"${target}"
  info "=== ${namespace}/${secret} ==="

  access_key="$(op read "op://${OP_VAULT}/${item}/ACCESS_KEY_ID" 2>/dev/null || true)"
  secret_key="$(op read "op://${OP_VAULT}/${item}/SECRET_ACCESS_KEY" 2>/dev/null || true)"
  [[ -n "${access_key}" ]] || die "op から読めません: op://${OP_VAULT}/${item}/ACCESS_KEY_ID"
  [[ -n "${secret_key}" ]] || die "op から読めません: op://${OP_VAULT}/${item}/SECRET_ACCESS_KEY"
  ok "  1Password から取得（access_key ${#access_key} 文字 / secret ${#secret_key} 文字）"

  k get namespace "${namespace}" >/dev/null 2>&1 || die "namespace がありません: ${namespace}"
  if k -n "${namespace}" get secret "${secret}" >/dev/null 2>&1; then
    info "  Secret: 既存（更新します）"
  else
    info "  Secret: 新規"
  fi

  if [[ "${APPLY}" == true ]]; then
    k -n "${namespace}" create secret generic "${secret}" \
      --from-literal=ACCESS_KEY_ID="${access_key}" \
      --from-literal=ACCESS_SECRET_KEY="${secret_key}" \
      --dry-run=client -o yaml \
      | k apply -f - >/dev/null
    k -n "${namespace}" get secret "${secret}" -o jsonpath='{.data.ACCESS_KEY_ID}' >/dev/null \
      || die "${namespace}/${secret} の反映を確認できません"
    ok "  投入しました"
  fi

  unset access_key secret_key
done

if [[ "${APPLY}" != true ]]; then
  warn "確認のみです。反映するには --apply を指定してください"
  exit 0
fi

cat <<'EOS'

次の手順:
  1. ObjectStore を R2 へ向ける（この PR のマニフェスト変更をマージする）
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
  4. 旧 MinIO のバケットは、R2 からの復元確認が取れるまで消さないこと
EOS
