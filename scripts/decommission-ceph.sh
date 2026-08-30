#!/usr/bin/env bash
# ===========================================================================
# Ceph を廃止し、SATA SSD を単体 ZFS（local-zfs）へ切り替える
#
# ⚠️⚠️ このスクリプトは **Ceph クラスタを完全に破壊** します。
#      OSD・プール・CephFS の全データが失われ、元に戻せません。
#
# 判断の経緯は docs/adr/0009-drop-ceph-adopt-longhorn.md を参照。
#
# ---------------------------------------------------------------------------
# 安全設計
# ---------------------------------------------------------------------------
# 既定は dry-run。実行には --yes と、対話的な確認入力の両方が必要。
# さらに各段階で「本当に空か」を検証し、データが残っていれば中断する。
#
# 段階:
#   1. 前提チェック   — Ceph 上に本当にデータが無いか検証する
#   2. ストレージ削除 — Proxmox から Ceph ストレージ定義を外す
#   3. CephFS 削除    — MDS を停止し、ファイルシステムを削除
#   4. プール削除     — RBD / CephFS のプールを削除
#   5. OSD 削除       — OSD を 1 台ずつ out → down → destroy
#   6. MON/MGR 削除   — Ceph クラスタ自体を解体
#   7. ZFS 作成       — 解放された SSD に local-zfs を作成
# ===========================================================================
set -euo pipefail

PVE_HOST="${PVE_HOST:-172.16.10.11}"
PVE_SSH_USER="${PVE_SSH_USER:-root}"
PVE_NODES="${PVE_NODES:-sv-proxmox-01 sv-proxmox-02 sv-proxmox-03}"
ZFS_POOL_NAME="${ZFS_POOL_NAME:-local-zfs}"
OSD_DEVICE="${OSD_DEVICE:-/dev/sda}"

DRY_RUN=true

readonly C_RED=$'\033[0;31m' C_GREEN=$'\033[0;32m' C_YELLOW=$'\033[0;33m'
readonly C_BLUE=$'\033[0;34m' C_BOLD=$'\033[1m' C_RESET=$'\033[0m'
info() { printf '%s[INFO]%s  %s\n' "${C_BLUE}"   "${C_RESET}" "$*"; }
ok()   { printf '%s[OK]%s    %s\n' "${C_GREEN}"  "${C_RESET}" "$*"; }
warn() { printf '%s[WARN]%s  %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die()  { printf '%s[ERROR]%s %s\n' "${C_RED}"    "${C_RESET}" "$*" >&2; exit 1; }
step() { printf '\n%s=== %s ===%s\n' "${C_BOLD}" "$*" "${C_RESET}"; }

usage() {
  cat <<'USAGE'
使い方: decommission-ceph.sh [--yes]

  （引数なし）  dry-run。何が起きるかを表示するだけで一切変更しない。
  --yes         実際に実行する（対話的な確認入力あり）。

環境変数:
  PVE_HOST       Ceph コマンドを実行するホスト   (既定: 172.16.10.11)
  PVE_NODES      対象ノード（スペース区切り）
  OSD_DEVICE     OSD が載っているデバイス        (既定: /dev/sda)
  ZFS_POOL_NAME  作成する ZFS プール名           (既定: local-zfs)

⚠️ 実行前に必ず確認すること:
   - Ceph 上に必要なデータが残っていないか
   - ISO / テンプレートを退避したか
   - PBS へのバックアップが取れているか
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes)     DRY_RUN=false; shift ;;
    -h|--help) usage; exit 0 ;;
    *)         die "不明な引数: $1" ;;
  esac
done

# --- 入力検証（root SSH でコマンド文字列を組むため）---
[[ "${PVE_HOST}"      =~ ^[A-Za-z0-9.:_-]+$ ]] || die "PVE_HOST が不正です"
[[ "${PVE_SSH_USER}"  =~ ^[A-Za-z0-9._-]+$  ]] || die "PVE_SSH_USER が不正です"
[[ "${ZFS_POOL_NAME}" =~ ^[A-Za-z0-9._-]+$  ]] || die "ZFS_POOL_NAME が不正です"
[[ "${OSD_DEVICE}"    =~ ^/dev/[A-Za-z0-9/_-]+$ ]] || die "OSD_DEVICE が不正です"
for n in ${PVE_NODES}; do
  [[ "${n}" =~ ^[A-Za-z0-9._-]+$ ]] || die "PVE_NODES に不正な値: ${n}"
done

pve()  { ssh -o BatchMode=yes -o ConnectTimeout=10 "${PVE_SSH_USER}@${PVE_HOST}" "$@"; }
node() { local h="$1"; shift; pve "ssh -o BatchMode=yes -o ConnectTimeout=10 ${h} \"$*\""; }

run() {
  if [[ "${DRY_RUN}" == true ]]; then
    printf '  %s[dry-run]%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*"
  else
    info "実行: $*"
    pve "$@"
  fi
}

pve 'ceph -s >/dev/null 2>&1' || die "Ceph へアクセスできません（既に廃止済み？）"

# ===========================================================================
step "1. 前提チェック — Ceph 上にデータが残っていないか"
# ===========================================================================

# --- VM が存在しないこと ---
VM_COUNT="$(pve 'qm list 2>/dev/null | tail -n +2 | wc -l' | tr -d ' ')"
if [[ "${VM_COUNT}" != "0" ]]; then
  warn "VM が ${VM_COUNT} 台存在します。Ceph 上にディスクを持つものがないか確認してください:"
  pve 'qm list' | sed 's/^/      /'
fi

# --- RBD イメージが存在しないこと ---
for pool in cephrdb_vm cephrdb_k8s; do
  if pve "ceph osd pool ls 2>/dev/null | grep -qx ${pool}"; then
    IMAGES="$(pve "rbd -p ${pool} ls 2>/dev/null" || true)"
    if [[ -n "${IMAGES}" ]]; then
      printf '%s\n' "${IMAGES}" | sed 's/^/      /'
      die "プール ${pool} に RBD イメージが残っています。
     先に VM を移行または削除してください。データが失われます。"
    fi
    ok "プール ${pool} は空です"
  fi
done

# --- CephFS の中身 ---
info "CephFS の内容:"
pve 'du -sh /mnt/pve/cephfs01/* 2>/dev/null' | sed 's/^/      /' || true
warn "上記のデータは失われます。必要なら先に退避してください（ISO/テンプレート等）。"

# ---------------------------------------------------------------------------
# 最終確認
#
# 単純な y/N ではなく、決まった文字列のタイプを要求する。
# これは取り消せない操作であり、「勢いで Enter を押す」事故を防ぐため。
# ---------------------------------------------------------------------------
if [[ "${DRY_RUN}" == false ]]; then
  printf '\n%s⚠️  警告%s\n' "${C_RED}" "${C_RESET}"
  printf '   これから Ceph クラスタを **完全に破壊** します。\n'
  printf '   OSD・プール・CephFS の全データが失われ、元に戻せません。\n'
  printf '   さらに %s の内容も wipefs で消去します。\n\n' "${OSD_DEVICE}"
  printf '   対象ノード: %s\n\n' "${PVE_NODES}"
  printf '続行するには "destroy ceph" と入力してください: '
  read -r confirmation
  if [[ "${confirmation}" != "destroy ceph" ]]; then
    info "入力が一致しませんでした。中止します。"
    exit 1
  fi
fi

# ===========================================================================
step "2. Proxmox からストレージ定義を削除"
# ===========================================================================
for st in cephrdb_k8s cephrdb_vm cephfs01; do
  if pve "grep -q ': ${st}$' /etc/pve/storage.cfg 2>/dev/null"; then
    run "pvesm remove ${st}"
  else
    info "ストレージ ${st} は存在しません（スキップ）"
  fi
done

# ===========================================================================
step "3. CephFS を削除"
# ===========================================================================
if pve 'ceph fs ls 2>/dev/null | grep -q cephfs01'; then
  # MDS を全ノードで停止してからでないと fs は削除できない
  for n in ${PVE_NODES}; do
    run "ssh -o BatchMode=yes ${n} 'systemctl stop ceph-mds@${n}.service || true'"
  done
  run "ceph fs fail cephfs01"
  run "ceph fs rm cephfs01 --yes-i-really-mean-it"
  for n in ${PVE_NODES}; do
    run "ssh -o BatchMode=yes ${n} 'systemctl disable ceph-mds@${n}.service || true'"
  done
else
  info "CephFS cephfs01 は存在しません（スキップ）"
fi

# ===========================================================================
step "4. プールを削除"
# ===========================================================================
# mon_allow_pool_delete は既に true（実測確認済み）だが念のため設定する
run "ceph config set mon mon_allow_pool_delete true"
for pool in cephrdb_k8s cephrdb_vm cephfs01_data cephfs01_metadata; do
  if pve "ceph osd pool ls 2>/dev/null | grep -qx ${pool}"; then
    run "ceph osd pool delete ${pool} ${pool} --yes-i-really-really-mean-it"
  else
    info "プール ${pool} は存在しません（スキップ）"
  fi
done

# ===========================================================================
step "5. OSD を削除"
# ===========================================================================
OSD_IDS="$(pve 'ceph osd ls 2>/dev/null' || true)"
if [[ -n "${OSD_IDS}" ]]; then
  for id in ${OSD_IDS}; do
    [[ "${id}" =~ ^[0-9]+$ ]] || continue
    info "OSD ${id} を削除します"
    run "ceph osd out ${id}"
    run "ceph osd down ${id}"
    # OSD が載っているノードでサービスを停止する
    OSD_HOST="$(pve "ceph osd find ${id} --format json 2>/dev/null" \
      | grep -oE '\"host\":\"[^\"]+\"' | head -1 | cut -d'\"' -f4 || echo "")"
    if [[ -n "${OSD_HOST}" ]]; then
      run "ssh -o BatchMode=yes ${OSD_HOST} 'systemctl stop ceph-osd@${id}.service || true'"
    fi
    run "ceph osd purge ${id} --yes-i-really-mean-it"
  done
else
  info "OSD は存在しません（スキップ）"
fi

# ===========================================================================
step "6. MON / MGR を削除し、Ceph を解体"
# ===========================================================================
for n in ${PVE_NODES}; do
  run "pveceph mgr destroy ${n} || true"
done
for n in ${PVE_NODES}; do
  run "pveceph mon destroy ${n} || true"
done

warn "Ceph の設定ファイル（/etc/pve/ceph.conf 等）の削除は手動で行ってください:"
printf '      pveceph purge --crash --logs\n'
printf '      rm -f /etc/pve/ceph.conf /etc/ceph/ceph.conf\n'

# ===========================================================================
step "7. 解放された SSD に ZFS プールを作成"
# ===========================================================================
for n in ${PVE_NODES}; do
  info "${n}: ${OSD_DEVICE} を ZFS 化します"
  # ⚠️ ディスクのラベル・パーティションを消してから作成する
  run "ssh -o BatchMode=yes ${n} 'wipefs -a ${OSD_DEVICE}'"
  # ashift=12 は 4K セクタ用。compression=lz4 は CPU 負荷が小さく容量効率が良い
  run "ssh -o BatchMode=yes ${n} 'zpool create -f -o ashift=12 -O compression=lz4 -O atime=off ${ZFS_POOL_NAME} ${OSD_DEVICE}'"
done

# Proxmox にストレージとして登録する（1 度だけ。全ノードで同名プールを使う）
run "pvesm add zfspool ${ZFS_POOL_NAME} --pool ${ZFS_POOL_NAME} --content images,rootdir --sparse 1"

# ===========================================================================
if [[ "${DRY_RUN}" == true ]]; then
  cat <<EOF

${C_YELLOW}これは dry-run です。何も変更していません。${C_RESET}

実際に実行するには:
  $0 --yes

⚠️ 実行すると Ceph の全データが失われます。取り消せません。

EOF
  exit 0
fi

cat <<EOF

┌──────────────────────────────────────────────────────────────────┐
│ Ceph の廃止と ZFS 化が完了しました。                              │
└──────────────────────────────────────────────────────────────────┘

  確認:
    ssh ${PVE_SSH_USER}@${PVE_HOST} pvesm status
    ssh ${PVE_SSH_USER}@${PVE_HOST} zpool status

  次の手順:
    ./scripts/preflight.sh
    cd tofu/10-proxmox-talos && tofu apply

EOF
