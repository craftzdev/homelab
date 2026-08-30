#!/usr/bin/env bash
# ===========================================================================
# etcd のスナップショットを取得する
#
# バックアップ 3 階層のうち「階層 1: クラスタ状態」を担う
# （docs/adr/0008-backup-strategy.md）。
#
# GitOps を徹底していればクラスタは Git から再構築できるが、
# etcd スナップショットがあれば復旧が圧倒的に速い。
#
# cron での定期実行例（運用端末）:
#   0 3 * * * cd /path/to/homelab && ./scripts/etcd-snapshot.sh >> /var/log/etcd-snapshot.log 2>&1
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

TALOSCONFIG_PATH="${TALOSCONFIG:-${REPO_ROOT}/_out/talosconfig}"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-${REPO_ROOT}/_out/etcd-snapshots}"
NODE="${NODE:-172.16.40.11}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

readonly C_RED=$'\033[0;31m' C_GREEN=$'\033[0;32m' C_BLUE=$'\033[0;34m' C_RESET=$'\033[0m'
info() { printf '%s[INFO]%s  %s\n' "${C_BLUE}"  "${C_RESET}" "$*"; }
ok()   { printf '%s[OK]%s    %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
die()  { printf '%s[ERROR]%s %s\n' "${C_RED}"   "${C_RESET}" "$*" >&2; exit 1; }

command -v talosctl >/dev/null || die "talosctl が見つかりません"
[[ -f "${TALOSCONFIG_PATH}" ]] || die "talosconfig が見つかりません: ${TALOSCONFIG_PATH}"

export TALOSCONFIG="${TALOSCONFIG_PATH}"

mkdir -p "${SNAPSHOT_DIR}"
chmod 700 "${SNAPSHOT_DIR}"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SNAPSHOT_FILE="${SNAPSHOT_DIR}/etcd-${TIMESTAMP}.snapshot"

info "etcd のスナップショットを取得しています（ノード: ${NODE}）..."
talosctl -n "${NODE}" etcd snapshot "${SNAPSHOT_FILE}"

# ---------------------------------------------------------------------------
# スナップショットの検証
#
# 「取得できた」ことと「復元に使える」ことは別である。
# 最低限、ファイルサイズが妥当かを確認する。
# ---------------------------------------------------------------------------
if [[ ! -s "${SNAPSHOT_FILE}" ]]; then
  rm -f "${SNAPSHOT_FILE}"
  die "スナップショットが空です。取得に失敗しています。"
fi

SIZE_BYTES="$(wc -c < "${SNAPSHOT_FILE}" | tr -d ' ')"
if [[ "${SIZE_BYTES}" -lt 65536 ]]; then
  die "スナップショットのサイズが異常に小さいです（${SIZE_BYTES} バイト）。
     取得に失敗している可能性があります: ${SNAPSHOT_FILE}"
fi

chmod 600 "${SNAPSHOT_FILE}"
ok "スナップショットを取得しました: ${SNAPSHOT_FILE} ($((SIZE_BYTES / 1024 / 1024)) MiB)"

# ---------------------------------------------------------------------------
# 古いスナップショットの削除
# ---------------------------------------------------------------------------
info "${RETENTION_DAYS} 日より古いスナップショットを削除しています..."
DELETED="$(find "${SNAPSHOT_DIR}" -name 'etcd-*.snapshot' -type f \
  -mtime "+${RETENTION_DAYS}" -print -delete | wc -l | tr -d ' ')"
if [[ "${DELETED}" -gt 0 ]]; then
  ok "${DELETED} 個の古いスナップショットを削除しました"
fi

REMAINING="$(find "${SNAPSHOT_DIR}" -name 'etcd-*.snapshot' -type f | wc -l | tr -d ' ')"
info "保持中のスナップショット: ${REMAINING} 個"

cat <<EOF

  ⚠️ このスナップショットは運用端末のローカルにあります。
     端末の故障で失われないよう、別の場所へも保管してください:

       - PBS の VM バックアップに含める（Kubernetes ノードとは別経路）
       - 外部ストレージへコピーする

  復元手順（クラスタ全損時）:
     docs/50-operations.md §災害復旧 を参照

EOF
