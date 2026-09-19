#!/usr/bin/env bash
# Kubernetes VM 6台のPBSバックアップジョブを冪等に作成・更新する。
set -euo pipefail

PVE_HOST="${PVE_HOST:-172.16.10.11}"
PVE_SSH_USER="${PVE_SSH_USER:-root}"
PBS_STORAGE="${PBS_STORAGE:-pbs-gateway}"
JOB_ID="${JOB_ID:-kubernetes-daily-pbs}"
VM_IDS="${VM_IDS:-1001,1002,1003,1101,1102,1103}"
SCHEDULE="${SCHEDULE:-02:30}"
BW_LIMIT_KIB="${BW_LIMIT_KIB:-51200}"
# 保持ポリシー。2026-09-19 に daily=7,weekly=4,monthly=3 から変更した。
#
# 変更の理由は「7 世代が容量に入らなかった」ではなく、**一度も入ったことが
# 無かった**ことである。PBS は暗号化された EPHEMERAL と Longhorn の 3 レプリカを
# 重複排除できず（ノード間の共有はそれぞれ 0.1 GiB / 0.06%）、1 晩あたり
# 新規チャンクが 97 GiB 積み上がっていた。datastore に使える領域は 431 GiB
# しかないため、daily=7 だけで 680 GiB 必要という算術的に不可能な設定だった。
# 2026-09-06 の運用開始から片道で埋まり続け、09-18 に上限へ到達して
# 5 夜ぶんのバックアップが ENOSPC で失敗した（docs/incidents/2026-09-19-moshitoku-outage.md）。
#
# weekly / monthly を残さないのも同じ理由である。重複排除が効かないため
# 1 世代前の weekly は今日とほとんどチャンクを共有せず、**1 世代ごとに
# ほぼフルコピー（約 116 GiB）** を要求する。daily の差分より高くつく。
#
# worker の scsi0 を対象外にした（tofu/10-proxmox-talos/vms.tf）あとの実測値:
#   1 世代 115.9 GiB ＋ 1 晩あたり 49.9 GiB
#   daily=3 → 約 216 GiB (50%) / daily=5 → 約 316 GiB (73%)
#
# ⚠️ daily=3 は移行措置ではなく、当面の定常値である。
#
#    daily=5 にすると空きが 27% になり、PBSDatastoreFillingUp（空き 30% 未満）が
#    恒常的に発報する。鳴りっぱなしの警告は読まれなくなる — それは 15% の
#    しきい値が機能しなかったのと同じ失敗である。1 晩が全体の 11.6% を占める
#    この構成では、**保持世代を増やすことと容量警告が機能することは両立しない。**
#
#    上げたければ先に 1 晩あたりの増分を減らすこと（Prometheus が Longhorn の
#    6 割を占めている。docs/pbs-capacity-2026-09-19.md §7-2）。
#
# ⚠️ 値は keep-daily だけにすること。PBS の prune オプションは加算で、
#    keep-last=5,keep-daily=5 は 10 世代になる（同文書 §6-1）。
PRUNE_BACKUPS="${PRUNE_BACKUPS:-keep-daily=3}"
APPLY=false

readonly C_RED=$'\033[0;31m' C_GREEN=$'\033[0;32m' C_YELLOW=$'\033[0;33m'
readonly C_BLUE=$'\033[0;34m' C_RESET=$'\033[0m'
info() { printf '%s[INFO]%s  %s\n' "${C_BLUE}" "${C_RESET}" "$*"; }
ok()   { printf '%s[OK]%s    %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
warn() { printf '%s[WARN]%s  %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die()  { printf '%s[ERROR]%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
使い方: reconcile-pbs-kubernetes-backup.sh [--apply]

引数なしでは前提条件と現在値だけを確認する。--applyを指定すると、
ProxmoxクラスタのPBSバックアップジョブを作成または更新する。

環境変数:
  PVE_HOST        操作するProxmoxノード（既定: 172.16.10.11）
  PBS_STORAGE     Proxmox側PBSストレージID（既定: pbs-gateway）
  JOB_ID          バックアップジョブID（既定: kubernetes-daily-pbs）
  VM_IDS          対象VMID（既定: 1001,1002,1003,1101,1102,1103）
  SCHEDULE        systemd calendar形式（既定: 毎日02:30 JST）
  BW_LIMIT_KIB    VMごとの帯域上限KiB/s（既定: 51200）
  PRUNE_BACKUPS   PBS保持ポリシー
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "不明な引数: $1" ;;
  esac
done

[[ "${PVE_HOST}" =~ ^[A-Za-z0-9.:_-]+$ ]] || die "PVE_HOSTが不正です"
[[ "${PVE_SSH_USER}" =~ ^[A-Za-z0-9._-]+$ ]] || die "PVE_SSH_USERが不正です"
[[ "${PBS_STORAGE}" =~ ^[A-Za-z0-9._-]+$ ]] || die "PBS_STORAGEが不正です"
[[ "${JOB_ID}" =~ ^[A-Za-z0-9._-]+$ ]] || die "JOB_IDが不正です"
[[ "${VM_IDS}" =~ ^[0-9]+(,[0-9]+)*$ ]] || die "VM_IDSが不正です"
[[ "${SCHEDULE}" =~ ^[A-Za-z0-9*:,.\ /_-]+$ ]] || die "SCHEDULEが不正です"
[[ "${BW_LIMIT_KIB}" =~ ^[0-9]+$ ]] || die "BW_LIMIT_KIBが不正です"
[[ "${PRUNE_BACKUPS}" =~ ^[A-Za-z0-9=,-]+$ ]] || die "PRUNE_BACKUPSが不正です"

pve() {
  ssh -o BatchMode=yes -o ConnectTimeout=10 \
    "${PVE_SSH_USER}@${PVE_HOST}" "$@"
}

command -v ssh >/dev/null || die "sshが見つかりません"
pve true || die "ProxmoxへSSH接続できません: ${PVE_HOST}"

pve "pvesm status --storage ${PBS_STORAGE} 2>/dev/null | awk 'NR == 2 && \$3 == \"active\" { found=1 } END { exit !found }'" \
  || die "PBSストレージ ${PBS_STORAGE} がactiveではありません"
ok "PBSストレージを確認: ${PBS_STORAGE}"

IFS=',' read -r -a vmids <<<"${VM_IDS}"
for vmid in "${vmids[@]}"; do
  pve "pvesh get /cluster/resources --type vm --output-format json | grep -Eq '\"vmid\"[[:space:]]*:[[:space:]]*${vmid}([,}])'" \
    || die "対象VM ${vmid} がProxmoxクラスタに存在しません"
done
ok "対象VMを確認: ${VM_IDS}"

# このジョブは --remove 1 を設定する。「バックアップ後に保持ポリシーを適用する」
# 設定で、PBS のトークンに Datastore.Prune を要求する。
#
# ⚠️ 既定の DatastoreBackup ロールには Datastore.Prune が無い。その場合、
#    バックアップ自体は成功し、**その後の prune だけが静かに失敗し続ける。**
#    PRUNE_BACKUPS に何を書いても効かず、使用量が伸び続ける。
#    2026-09-20 に発覚した（docs/pbs-capacity-2026-09-19.md §8）。
#
# 直近のタスクログに痕跡が残っていれば警告する。PBS へは SSH しないので
# 権限そのものは確認できない。
if pve "grep -rlF --include='*vzdump*' 'Datastore.Prune on /datastore' /var/log/pve/tasks 2>/dev/null | head -1 | grep -q ."; then
  warn "直近のバックアップで prune が権限不足により失敗しています"
  warn "PBS トークンに Datastore.Prune がありません。--remove 1 が効いていません"
  warn "対処: docs/pbs-capacity-2026-09-19.md §8-1"
fi

if pve "pvesh get /cluster/backup/${JOB_ID} >/dev/null 2>&1"; then
  action="set"
  info "既存ジョブを更新します: ${JOB_ID}"
else
  action="create"
  info "新規ジョブを作成します: ${JOB_ID}"
fi

if [[ "${APPLY}" != true ]]; then
  warn "確認のみです。反映するには --apply を指定してください"
  printf '  storage: %s\n  vmid: %s\n  schedule: %s\n  retention: %s\n  bwlimit: %s KiB/s\n' \
    "${PBS_STORAGE}" "${VM_IDS}" "${SCHEDULE}" "${PRUNE_BACKUPS}" "${BW_LIMIT_KIB}"
  exit 0
fi

common_args="--storage ${PBS_STORAGE} --vmid ${VM_IDS} --schedule ${SCHEDULE} \
--mode snapshot --compress zstd --zstd 1 --bwlimit ${BW_LIMIT_KIB} --ionice 8 \
--prune-backups ${PRUNE_BACKUPS} --remove 1 --repeat-missed 0 --enabled 1 \
--comment managed-by-homelab-reconcile-pbs-kubernetes-backup"

if [[ "${action}" == create ]]; then
  pve "pvesh create /cluster/backup --id ${JOB_ID} ${common_args}"
else
  pve "pvesh set /cluster/backup/${JOB_ID} ${common_args}"
fi

actual="$(pve "pvesh get /cluster/backup/${JOB_ID} --output-format json-pretty")"
printf '%s\n' "${actual}"
printf '%s\n' "${actual}" | grep -q "\"storage\" : \"${PBS_STORAGE}\"" \
  || die "反映後のstorageが一致しません"
printf '%s\n' "${actual}" | grep -q "\"vmid\" : \"${VM_IDS}\"" \
  || die "反映後のVMIDが一致しません"
ok "PBSバックアップジョブを反映しました: ${JOB_ID}"
