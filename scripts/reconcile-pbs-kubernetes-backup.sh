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
PRUNE_BACKUPS="${PRUNE_BACKUPS:-keep-daily=7,keep-weekly=4,keep-monthly=3}"
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
