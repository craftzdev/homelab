#!/usr/bin/env bash
# Proxmox 3台に node_exporter と ZFS 用 textfile collector を冪等に導入する。
#
# ---------------------------------------------------------------------------
# なぜ必要か
# ---------------------------------------------------------------------------
# 2026-09-13 のストレージ障害を診断できた決定的な指標は、いずれも
# **Proxmox ホスト側**のものだった。
#
#   - SATA の write await（ゲスト側の値は Longhorn の複製往復と LUKS を含み、
#     物理ディスクの遅延とは別物）
#   - ZFS の txg 同期時間（95MB に 51.8 秒かかっていた）
#   - zpool の TRIM 状態（作成以来一度も TRIM されていなかった）
#
# しかし Prometheus がスクレイプしていたのは VM（172.16.40.x）だけで、
# ホスト（172.16.10.x）は監視対象外だった。そのため数時間気づかれなかった。
# 経緯は docs/storage-migration-2026-09-13.md を参照。
#
# ---------------------------------------------------------------------------
# 取り込み経路
# ---------------------------------------------------------------------------
# クラスタから 172.16.10.0/24 への egress は
# CiliumClusterwideNetworkPolicy/deny-egress-to-home-network が拒否している。
# monitoring namespace を除外すると Prometheus 本体が管理ネットワークへ
# 到達できるようになるため、代わりに **既に除外されている portal の
# pbs-observer** が各ホストの node_exporter を読み、必要な指標だけを
# 再出力する。Prometheus は従来どおり pbs-observer だけを見る。
#
# ⚠️ Proxmox のファイアウォールは無効（`pve-firewall status` = disabled）。
#    そのため node_exporter は管理 LAN 上の誰からでも読める。VLAN20/30 へは
#    出さないよう、待ち受けアドレスを管理 IP に固定している。
#    node_exporter が公開するのはシステム統計であり資格情報は含まないが、
#    管理 LAN が信頼境界であるという前提に依存している点は認識しておくこと。
set -euo pipefail

PVE_HOSTS="${PVE_HOSTS:-172.16.10.11 172.16.10.12 172.16.10.13}"
PVE_SSH_USER="${PVE_SSH_USER:-root}"
LISTEN_PORT="${LISTEN_PORT:-9100}"
COLLECT_INTERVAL="${COLLECT_INTERVAL:-1min}"
TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/prometheus/node-exporter}"
APPLY=false

readonly C_RED=$'\033[0;31m' C_GREEN=$'\033[0;32m' C_YELLOW=$'\033[0;33m'
readonly C_BLUE=$'\033[0;34m' C_RESET=$'\033[0m'
info() { printf '%s[INFO]%s  %s\n' "${C_BLUE}" "${C_RESET}" "$*"; }
ok()   { printf '%s[OK]%s    %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
warn() { printf '%s[WARN]%s  %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die()  { printf '%s[ERROR]%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
使い方: reconcile-proxmox-node-exporter.sh [--apply]

引数なしでは各ホストの現在値だけを確認する。

環境変数:
  PVE_HOSTS         対象ホスト（既定: 172.16.10.11 172.16.10.12 172.16.10.13）
  PVE_SSH_USER      SSHユーザー（既定: root）
  LISTEN_PORT       node_exporter の待ち受けポート（既定: 9100）
  COLLECT_INTERVAL  ZFS collector の実行間隔（systemd 形式、既定: 1min）
  TEXTFILE_DIR      textfile collector のディレクトリ

導入するもの:
  - prometheus-node-exporter（apt）。待ち受けは各ホストの管理 IP に固定
  - scripts/proxmox-zfs-textfile-collector.sh を /usr/local/bin へ配置
  - zfs-textfile-collector.service / .timer（一時ファイルへ書いて mv する）
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "不明な引数: $1" ;;
  esac
done

[[ "${PVE_SSH_USER}" =~ ^[A-Za-z0-9._-]+$ ]] || die "PVE_SSH_USERが不正です"
[[ "${LISTEN_PORT}" =~ ^[0-9]+$ ]] || die "LISTEN_PORTが不正です"
[[ "${COLLECT_INTERVAL}" =~ ^[0-9]+(s|sec|min|h)$ ]] || die "COLLECT_INTERVALが不正です"
for host in ${PVE_HOSTS}; do
  [[ "${host}" =~ ^[A-Za-z0-9.:_-]+$ ]] || die "PVE_HOSTSに不正なホストがあります: ${host}"
done

command -v ssh >/dev/null || die "sshが見つかりません"
command -v scp >/dev/null || die "scpが見つかりません"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COLLECTOR_SRC="${SCRIPT_DIR}/proxmox-zfs-textfile-collector.sh"
[[ -f "${COLLECTOR_SRC}" ]] || die "collector が見つかりません: ${COLLECTOR_SRC}"

pve() { local h="$1"; shift; ssh -o BatchMode=yes -o ConnectTimeout=10 "${PVE_SSH_USER}@${h}" "$@"; }

changed=0

for host in ${PVE_HOSTS}; do
  info "=== ${host} ==="
  pve "${host}" true || die "SSH接続できません: ${host}"

  installed="$(pve "${host}" "dpkg-query -W -f='\${Version}' prometheus-node-exporter 2>/dev/null || true")"
  listening="$(pve "${host}" "ss -lnt 2>/dev/null | grep -c ':${LISTEN_PORT} ' || true")"
  has_collector="$(pve "${host}" "test -x /usr/local/bin/proxmox-zfs-textfile-collector.sh && echo yes || echo no")"
  timer="$(pve "${host}" "systemctl is-enabled zfs-textfile-collector.timer 2>/dev/null | head -1" || true)"
  timer="${timer:-absent}"

  printf '  node_exporter=%s  listening=%s  collector=%s  timer=%s\n' \
    "${installed:-none}" "${listening}" "${has_collector}" "${timer}"

  if [[ "${APPLY}" != true ]]; then
    [[ -n "${installed}" ]] || warn "  node_exporter が未導入です"
    [[ "${has_collector}" == yes ]] || warn "  ZFS collector が未配置です"
    [[ "${timer}" == enabled ]] || warn "  timer が未設定です"
    continue
  fi

  if [[ -z "${installed}" ]]; then
    pve "${host}" "DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq prometheus-node-exporter" >/dev/null
    changed=$((changed + 1))
    ok "  prometheus-node-exporter を導入しました"
  fi

  # 待ち受けを管理 IP に固定する。既定は 0.0.0.0 で、VLAN20/30 にも出てしまう。
  pve "${host}" "
    set -e
    ip=\$(ip -4 -o addr show dev vmbr0 | awk '{print \$4}' | cut -d/ -f1)
    [ -n \"\$ip\" ] || { echo 'vmbr0 に IPv4 がありません' >&2; exit 1; }
    want=\"ARGS=\\\"--web.listen-address=\$ip:${LISTEN_PORT} --collector.textfile.directory=${TEXTFILE_DIR}\\\"\"
    cur=\$(grep '^ARGS=' /etc/default/prometheus-node-exporter 2>/dev/null || true)
    if [ \"\$cur\" != \"\$want\" ]; then
      sed -i '/^ARGS=/d' /etc/default/prometheus-node-exporter
      printf '%s\n' \"\$want\" >> /etc/default/prometheus-node-exporter
      systemctl restart prometheus-node-exporter
      echo CHANGED
    fi"  | grep -q CHANGED && { changed=$((changed + 1)); ok "  待ち受けアドレスを設定しました"; } || true

  scp -q -o BatchMode=yes "${COLLECTOR_SRC}" \
    "${PVE_SSH_USER}@${host}:/usr/local/bin/proxmox-zfs-textfile-collector.sh"
  pve "${host}" "chmod 0755 /usr/local/bin/proxmox-zfs-textfile-collector.sh"

  # 一時ファイルへ書いてから mv する。node_exporter が書きかけを読むと
  # 解析エラーになり、そのホストのメトリクスが丸ごと落ちる。
  pve "${host}" "cat > /etc/systemd/system/zfs-textfile-collector.service <<'UNIT'
[Unit]
Description=Write ZFS pool TRIM and txg metrics for node_exporter
Documentation=https://github.com/craftzdev/homelab/blob/main/docs/storage-migration-2026-09-13.md

[Service]
Type=oneshot
ExecStart=/bin/sh -c '/usr/local/bin/proxmox-zfs-textfile-collector.sh > ${TEXTFILE_DIR}/zfs.prom.\$\$ && mv ${TEXTFILE_DIR}/zfs.prom.\$\$ ${TEXTFILE_DIR}/zfs.prom'
Nice=10
IOSchedulingClass=idle
UNIT
cat > /etc/systemd/system/zfs-textfile-collector.timer <<'UNIT'
[Unit]
Description=Collect ZFS pool TRIM and txg metrics periodically

[Timer]
OnBootSec=1min
OnUnitActiveSec=${COLLECT_INTERVAL}
AccuracySec=10s

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now zfs-textfile-collector.timer >/dev/null
systemctl start zfs-textfile-collector.service"
  ok "  ZFS collector と timer を設定しました"

  # 反映確認
  pve "${host}" "test -s ${TEXTFILE_DIR}/zfs.prom" \
    || die "${host}: ${TEXTFILE_DIR}/zfs.prom が生成されていません"
  n="$(pve "${host}" "ip=\$(ip -4 -o addr show dev vmbr0 | awk '{print \$4}' | cut -d/ -f1); curl -s --max-time 5 http://\$ip:${LISTEN_PORT}/metrics | grep -c '^zfs_pool_' || true")"
  [[ "${n}" -gt 0 ]] || die "${host}: node_exporter が zfs_pool_* を返しません"
  ok "  node_exporter が zfs_pool_* を ${n} 件公開しています"
done

if [[ "${APPLY}" != true ]]; then
  warn "確認のみです。反映するには --apply を指定してください"
  exit 0
fi

ok "変更 ${changed} 件"
cat <<'EOS'

取り込み側（portal/pbs-observer）も必要:
  kubernetes/infra/homepage/pbs-exporter/exporter.py      PVE_NODE_EXPORTERS
  kubernetes/infra/homepage/pbs-exporter/networkpolicy.yaml  9100 への egress
EOS
