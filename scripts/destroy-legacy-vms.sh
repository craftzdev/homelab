#!/usr/bin/env bash
# ===========================================================================
# 旧 kubeadm クラスタの VM を削除する
#
# ⚠️⚠️ このスクリプトは VM とそのディスクを **完全に削除** します。
#      中のデータは復旧できません。
#
# 実行前に必ず:
#   1. VM の中身が不要であることを確認する
#   2. 必要なら PBS でバックアップを取る
#
# 既定では dry-run（何も削除しない）。実際に削除するには --yes が必要。
# ===========================================================================
set -euo pipefail

PVE_HOST="${PVE_HOST:-172.16.10.11}"
PVE_SSH_USER="${PVE_SSH_USER:-root}"

# 削除対象の VMID（旧 kubeadm クラスタ + テンプレート）
LEGACY_VMIDS=(1001 1002 1003 1101 1102 1103 9050)  # 旧 6VM 構成 + テンプレート

DRY_RUN=true

readonly C_RED=$'\033[0;31m' C_GREEN=$'\033[0;32m' C_YELLOW=$'\033[0;33m'
readonly C_BLUE=$'\033[0;34m' C_BOLD=$'\033[1m' C_RESET=$'\033[0m'
info() { printf '%s[INFO]%s  %s\n' "${C_BLUE}"   "${C_RESET}" "$*"; }
ok()   { printf '%s[OK]%s    %s\n' "${C_GREEN}"  "${C_RESET}" "$*"; }
warn() { printf '%s[WARN]%s  %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die()  { printf '%s[ERROR]%s %s\n' "${C_RED}"    "${C_RESET}" "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
使い方: destroy-legacy-vms.sh [--yes]

  （引数なし）  dry-run。削除対象を一覧表示するだけで何も変更しない。
  --yes         実際に削除する。確認プロンプトが表示される。

環境変数:
  PVE_HOST      Proxmox ホスト (既定: 172.16.10.11)
  LEGACY_VMIDS_OVERRIDE
                削除対象の VMID をスペース区切りで上書きできる
                例: LEGACY_VMIDS_OVERRIDE="1001 1002" ./destroy-legacy-vms.sh
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes)     DRY_RUN=false; shift ;;
    -h|--help) usage; exit 0 ;;
    *)         die "不明な引数: $1" ;;
  esac
done

# ---------------------------------------------------------------------------
# 環境変数での上書きに対応する
#
# ⚠️ VMID は `qm destroy` に渡されるため、数値以外を受け付けない。
#    ここを緩めると root SSH 経由の任意コマンド実行につながる。
# ---------------------------------------------------------------------------
if [[ -n "${LEGACY_VMIDS_OVERRIDE:-}" ]]; then
  read -r -a LEGACY_VMIDS <<< "${LEGACY_VMIDS_OVERRIDE}"
fi

for vmid in "${LEGACY_VMIDS[@]}"; do
  [[ "${vmid}" =~ ^[0-9]+$ ]] || die "VMID は数値である必要があります: '${vmid}'"
done
unset vmid

if [[ ! "${PVE_HOST}" =~ ^[A-Za-z0-9.:_-]+$ ]]; then
  die "PVE_HOST に使用できない文字が含まれています: '${PVE_HOST}'"
fi
if [[ ! "${PVE_SSH_USER}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  die "PVE_SSH_USER に使用できない文字が含まれています: '${PVE_SSH_USER}'"
fi

pve() { ssh -o BatchMode=yes -o ConnectTimeout=10 "${PVE_SSH_USER}@${PVE_HOST}" "$@"; }

pve 'true' 2>/dev/null || die "Proxmox へ SSH できません（${PVE_SSH_USER}@${PVE_HOST}）"

# ---------------------------------------------------------------------------
# 対象 VM の所在と状態を調べる
# ---------------------------------------------------------------------------
info "削除対象の VM を調査しています..."

declare -a TARGETS=()

CLUSTER_VMS="$(pve 'pvesh get /cluster/resources --type vm --output-format json' 2>/dev/null || echo '[]')"

for vmid in "${LEGACY_VMIDS[@]}"; do
  entry="$(printf '%s' "${CLUSTER_VMS}" \
    | jq -r --argjson id "${vmid}" \
      '.[] | select(.vmid == $id) | "\(.vmid)|\(.name)|\(.node)|\(.status)|\(.template)"' 2>/dev/null || echo "")"
  if [[ -n "${entry}" ]]; then
    TARGETS+=("${entry}")
  fi
done

if [[ ${#TARGETS[@]} -eq 0 ]]; then
  ok "削除対象の VM は存在しません。何もすることはありません。"
  exit 0
fi

printf '\n%s削除対象:%s\n' "${C_BOLD}" "${C_RESET}"
printf '  %-7s %-16s %-16s %-10s %s\n' "VMID" "NAME" "NODE" "STATUS" "TEMPLATE"
printf '  %s\n' "----------------------------------------------------------------------"
for t in "${TARGETS[@]}"; do
  IFS='|' read -r vmid name node status template <<< "${t}"
  printf '  %-7s %-16s %-16s %-10s %s\n' "${vmid}" "${name}" "${node}" "${status}" "${template}"
done
printf '\n'

if [[ "${DRY_RUN}" == true ]]; then
  cat <<EOF
${C_YELLOW}これは dry-run です。何も削除していません。${C_RESET}

実際に削除するには:
  $0 --yes

⚠️ 削除すると VM のディスクも一緒に消えます。
   中のデータが不要であることを必ず確認してください。

EOF
  exit 0
fi

# ---------------------------------------------------------------------------
# 確認プロンプト
#
# 単純な y/N ではなく、対象数のタイプを要求する。
# 「勢いで Enter を押す」事故を防ぐため。
# ---------------------------------------------------------------------------
printf '%s⚠️  警告: %d 個の VM とそのディスクを完全に削除します。%s\n' \
  "${C_RED}" "${#TARGETS[@]}" "${C_RESET}"
printf '   この操作は取り消せません。\n\n'
printf '続行するには "delete %d vms" と入力してください: ' "${#TARGETS[@]}"
read -r confirmation

if [[ "${confirmation}" != "delete ${#TARGETS[@]} vms" ]]; then
  info "入力が一致しませんでした。中止します。"
  exit 1
fi

# ---------------------------------------------------------------------------
# 削除
# ---------------------------------------------------------------------------
for t in "${TARGETS[@]}"; do
  IFS='|' read -r vmid name node status template <<< "${t}"

  info "VM ${vmid} (${name}) を ${node} から削除しています..."

  # 稼働中なら先に停止する
  if [[ "${status}" == "running" ]]; then
    info "  停止しています..."
    pve "qm stop ${vmid}" || warn "  停止に失敗しました（強制削除を試みます）"
    # 停止完了を待つ
    for _ in $(seq 1 30); do
      current="$(pve "qm status ${vmid} 2>/dev/null | awk '{print \$2}'" || echo "unknown")"
      [[ "${current}" != "running" ]] && break
      sleep 2
    done
  fi

  # --purge: バックアップジョブや HA 設定からも削除する
  # --destroy-unreferenced-disks: 参照が外れたディスクも削除する
  if pve "qm destroy ${vmid} --purge --destroy-unreferenced-disks 1"; then
    ok "  VM ${vmid} を削除しました"
  else
    warn "  VM ${vmid} の削除に失敗しました"
  fi
done

printf '\n'
ok "旧 VM の削除が完了しました。"

cat <<'EOF'

  残ったディスクイメージの確認（孤児が残っていないか）:
    ssh root@172.16.10.11 pvesm list local-zfs

  次の手順:
    ./scripts/preflight.sh
    cd tofu/10-proxmox-talos && tofu apply

EOF
