#!/usr/bin/env bash
# Proxmox 3台のZFSプールにTRIM設定を冪等に適用する。
#
# ---------------------------------------------------------------------------
# なぜこれが必要か
# ---------------------------------------------------------------------------
# Debian の /etc/cron.d/zfsutils-linux には毎月第1日曜のTRIMが最初から
# 設定されている。しかし /usr/lib/zfs-linux/trim は、プールの
# org.debian:periodic-trim が既定値 auto のとき
#
#     -|auto) if pool_is_nvme_only "${pool}"; then trim_if_not_already_trimming ...
#
# という分岐に入り、**NVMeのみで構成されたプールしかTRIMしない**。
# local-zfs は SATA 1本のプールなので、cronは毎月起動しては何もせず終了する。
#
# 2026-09-13、この設定漏れにより SUNEAST SE800 Lite の FTL が空きブロックを
# 認識できずGCで飽和し、プール作成からわずか7日で Longhorn のレプリカ再構築が
# 完走できない状態になった。ゲストの平均書き込み遅延は54秒に達した。
# 経緯は docs/storage-migration-2026-09-13.md を参照。
#
# ---------------------------------------------------------------------------
# 何を設定するか
# ---------------------------------------------------------------------------
#   zpool set autotrim=on <pool>
#       主対策。ブロック解放時に随時discardを発行する。
#       月1回では間に合わない（7日で壊滅した実績がある）。
#
#   zfs set org.debian:periodic-trim=enable <pool>
#       保険。新しいタイマーもスクリプトも追加せず、既存の月次cronを
#       SATAプールに対しても機能させるだけ。
#
# autotrim は「今後解放されるブロック」にしか効かないため、既存の蓄積は
# 一度 zpool trim で解消する必要がある。--trim-now がそれを行う。
#
# ⚠️ このスクリプトが設定するのは Proxmox ホスト自身の状態であり、
#    OpenTofu（VMのみ管理）の管理対象外である。ホストを再インストールすると
#    失われるため、再構築手順の一部として実行すること。
set -euo pipefail

PVE_HOSTS="${PVE_HOSTS:-172.16.10.11 172.16.10.12 172.16.10.13}"
PVE_SSH_USER="${PVE_SSH_USER:-root}"
ZFS_POOL="${ZFS_POOL:-local-zfs}"
TRIM_RATE="${TRIM_RATE:-200M}"
APPLY=false
TRIM_NOW=false

readonly C_RED=$'\033[0;31m' C_GREEN=$'\033[0;32m' C_YELLOW=$'\033[0;33m'
readonly C_BLUE=$'\033[0;34m' C_RESET=$'\033[0m'
info() { printf '%s[INFO]%s  %s\n' "${C_BLUE}" "${C_RESET}" "$*"; }
ok()   { printf '%s[OK]%s    %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
warn() { printf '%s[WARN]%s  %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die()  { printf '%s[ERROR]%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
使い方: reconcile-proxmox-zfs-trim.sh [--apply] [--trim-now]

引数なしでは各ホストの現在値だけを表示する。

  --apply      autotrim=on と org.debian:periodic-trim=enable を設定する
  --trim-now   --apply と併用したときだけ有効。一度もTRIMされていない
               プールに対して zpool trim を開始する（非破壊・中止可能）。
               実行中のTRIMがある場合はそのホストを飛ばす。

環境変数:
  PVE_HOSTS     対象ホスト（既定: 172.16.10.11 172.16.10.12 172.16.10.13）
  PVE_SSH_USER  SSHユーザー（既定: root）
  ZFS_POOL      プール名（既定: local-zfs）
  TRIM_RATE     zpool trim -r に渡す上限（既定: 200M）

TRIMの所要時間の目安: 1TB SATA SSD・使用率7%で 77〜86分。
進捗確認:  ssh root@<host> 'zpool status -t <pool>'
中止:      ssh root@<host> 'zpool trim -c <pool>'
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=true; shift ;;
    --trim-now) TRIM_NOW=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "不明な引数: $1" ;;
  esac
done

[[ "${PVE_SSH_USER}" =~ ^[A-Za-z0-9._-]+$ ]] || die "PVE_SSH_USERが不正です"
[[ "${ZFS_POOL}" =~ ^[A-Za-z0-9._:-]+$ ]] || die "ZFS_POOLが不正です"
[[ "${TRIM_RATE}" =~ ^[0-9]+[KMGT]?$ ]] || die "TRIM_RATEが不正です"
for host in ${PVE_HOSTS}; do
  [[ "${host}" =~ ^[A-Za-z0-9.:_-]+$ ]] || die "PVE_HOSTSに不正なホストがあります: ${host}"
done

if [[ "${TRIM_NOW}" == true && "${APPLY}" != true ]]; then
  die "--trim-now は --apply と併用してください"
fi

command -v ssh >/dev/null || die "sshが見つかりません"

pve() {
  local host="$1"; shift
  ssh -o BatchMode=yes -o ConnectTimeout=10 "${PVE_SSH_USER}@${host}" "$@"
}

changed=0
started=0

for host in ${PVE_HOSTS}; do
  info "=== ${host} ==="
  pve "${host}" true || die "SSH接続できません: ${host}"

  pve "${host}" "zpool list -H -o name ${ZFS_POOL} >/dev/null 2>&1" \
    || die "${host}: プール ${ZFS_POOL} が見つかりません"

  health="$(pve "${host}" "zpool list -H -o health ${ZFS_POOL}")"
  [[ "${health}" == "ONLINE" ]] \
    || die "${host}: プールが ONLINE ではありません (${health})。先に障害を解消すること"

  autotrim="$(pve "${host}" "zpool get -H -o value autotrim ${ZFS_POOL}")"
  periodic="$(pve "${host}" "zfs get -H -o value org.debian:periodic-trim ${ZFS_POOL}")"
  trimstate="$(pve "${host}" "zpool status -t ${ZFS_POOL} | grep -oE '\((untrimmed|[0-9]+% trimmed|trimmed, completed)[^)]*\)' | head -1")"

  printf '  autotrim=%s  periodic-trim=%s\n  trim: %s\n' \
    "${autotrim}" "${periodic}" "${trimstate:-(不明)}"

  if [[ "${APPLY}" != true ]]; then
    [[ "${autotrim}" == "on" ]] || warn "  autotrim が on ではありません"
    [[ "${periodic}" == "enable" ]] || warn "  periodic-trim が enable ではありません（既定の auto はSATAプールを無視する）"
    continue
  fi

  if [[ "${autotrim}" != "on" ]]; then
    pve "${host}" "zpool set autotrim=on ${ZFS_POOL}"
    changed=$((changed + 1))
    ok "  autotrim=on を設定しました"
  fi

  if [[ "${periodic}" != "enable" ]]; then
    pve "${host}" "zfs set org.debian:periodic-trim=enable ${ZFS_POOL}"
    changed=$((changed + 1))
    ok "  org.debian:periodic-trim=enable を設定しました"
  fi

  # 反映確認
  pve "${host}" "test \"\$(zpool get -H -o value autotrim ${ZFS_POOL})\" = on" \
    || die "${host}: autotrim の反映を確認できません"
  pve "${host}" "test \"\$(zfs get -H -o value org.debian:periodic-trim ${ZFS_POOL})\" = enable" \
    || die "${host}: periodic-trim の反映を確認できません"

  if [[ "${TRIM_NOW}" == true ]]; then
    if [[ "${trimstate}" == *"% trimmed"* ]]; then
      warn "  TRIMが既に実行中のため開始しません: ${trimstate}"
    elif [[ "${trimstate}" == *"trimmed, completed"* ]]; then
      info "  TRIM済みのため開始しません"
    else
      pve "${host}" "zpool trim -r ${TRIM_RATE} ${ZFS_POOL}"
      started=$((started + 1))
      ok "  zpool trim を開始しました（レート上限 ${TRIM_RATE}）"
    fi
  fi
done

if [[ "${APPLY}" != true ]]; then
  warn "確認のみです。反映するには --apply を指定してください"
  exit 0
fi

ok "設定変更 ${changed} 件、TRIM開始 ${started} 件"

if (( started > 0 )); then
  cat <<EOS

TRIMはバックグラウンドで進行する。完了まで1台あたり概ね80分。
  進捗: ssh ${PVE_SSH_USER}@<host> 'zpool status -t ${ZFS_POOL}'
  中止: ssh ${PVE_SSH_USER}@<host> 'zpool trim -c ${ZFS_POOL}'

Longhornのレプリカが1コピーしか無いボリュームがある状態では実行しない。
  kubectl -n longhorn-system get volumes.longhorn.io \\
    -o custom-columns=V:.metadata.name,R:.status.robustness
EOS
fi
