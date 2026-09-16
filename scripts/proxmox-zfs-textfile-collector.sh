#!/usr/bin/env bash
# node_exporter の textfile collector 向けに、ZFS プールの状態を出力する。
#
# ---------------------------------------------------------------------------
# なぜ必要か
# ---------------------------------------------------------------------------
# node_exporter 1.9.0 の ZFS collector は ARC / ZIL / dataset の統計は出すが、
#
#   - zpool の TRIM 状態（untrimmed / trimming / completed）
#   - txg の同期所要時間（stime）
#
# を出さない。2026-09-13 の障害では、この 2 つがまさに決定的な指標だった。
#
#   プールは作成以来一度も TRIM されておらず（Debian の月次 TRIM は既定で
#   NVMe 専用プールしか対象にしない）、SATA SSD の FTL が空きブロックを
#   認識できず GC で飽和していた。txg sync は 95MB に 51.8 秒かかっていた。
#
# どちらも Prometheus からは見えず、数時間誰も気づかなかった。
# 経緯は docs/storage-migration-2026-09-13.md を参照。
#
# ---------------------------------------------------------------------------
# 設置方法
# ---------------------------------------------------------------------------
# scripts/reconcile-proxmox-node-exporter.sh が各ホストへ配置し、
# systemd timer で定期実行する。単体でも動く:
#
#   ./proxmox-zfs-textfile-collector.sh > /var/lib/prometheus/node-exporter/zfs.prom
#
# ⚠️ 出力先へ直接リダイレクトしないこと。node_exporter が書きかけの
#    ファイルを読むと解析エラーになる。呼び出し側で一時ファイルへ書いてから
#    mv すること（reconcile スクリプトが生成する wrapper がそうしている）。
set -euo pipefail

command -v zpool >/dev/null || exit 0

printf '# HELP zfs_pool_trim_state ZFS vdev TRIM state (one-hot; 1 marks the current state).\n'
printf '# TYPE zfs_pool_trim_state gauge\n'
printf '# HELP zfs_pool_trim_progress_ratio TRIM progress of the vdev, 0-1.\n'
printf '# TYPE zfs_pool_trim_progress_ratio gauge\n'
printf '# HELP zfs_pool_autotrim Whether autotrim is on for the pool.\n'
printf '# TYPE zfs_pool_autotrim gauge\n'
printf '# HELP zfs_pool_periodic_trim_enabled Whether the Debian monthly TRIM cron will actually trim this pool.\n'
printf '# TYPE zfs_pool_periodic_trim_enabled gauge\n'
printf '# HELP zfs_pool_health Whether the pool is ONLINE.\n'
printf '# TYPE zfs_pool_health gauge\n'
printf '# HELP zfs_pool_fragmentation_ratio Pool fragmentation, 0-1.\n'
printf '# TYPE zfs_pool_fragmentation_ratio gauge\n'
printf '# HELP zfs_pool_capacity_ratio Pool capacity used, 0-1.\n'
printf '# TYPE zfs_pool_capacity_ratio gauge\n'
printf '# HELP zfs_pool_txg_sync_seconds_max Longest txg sync time among recent committed transaction groups.\n'
printf '# TYPE zfs_pool_txg_sync_seconds_max gauge\n'
printf '# HELP zfs_pool_txg_sync_seconds_last Sync time of the most recent committed transaction group.\n'
printf '# TYPE zfs_pool_txg_sync_seconds_last gauge\n'

while read -r pool; do
  [ -n "${pool}" ] || continue

  # --- プール単位 -----------------------------------------------------------
  read -r health frag cap <<<"$(zpool list -H -o health,fragmentation,capacity "${pool}" 2>/dev/null || echo "- - -")"
  printf 'zfs_pool_health{pool="%s"} %d\n' "${pool}" "$([ "${health}" = "ONLINE" ] && echo 1 || echo 0)"
  case "${frag}" in *%) printf 'zfs_pool_fragmentation_ratio{pool="%s"} %s\n' "${pool}" "$(awk -v v="${frag%\%}" 'BEGIN{printf "%.4f", v/100}')";; esac
  case "${cap}" in *%) printf 'zfs_pool_capacity_ratio{pool="%s"} %s\n' "${pool}" "$(awk -v v="${cap%\%}" 'BEGIN{printf "%.4f", v/100}')";; esac

  autotrim="$(zpool get -H -o value autotrim "${pool}" 2>/dev/null || echo off)"
  printf 'zfs_pool_autotrim{pool="%s"} %d\n' "${pool}" "$([ "${autotrim}" = "on" ] && echo 1 || echo 0)"

  # Debian の /usr/lib/zfs-linux/trim は、この property が enable のときだけ
  # 非 NVMe プールを TRIM する。既定値 auto / - では SATA プールを飛ばす。
  periodic="$(zfs get -H -o value org.debian:periodic-trim "${pool}" 2>/dev/null || echo -)"
  printf 'zfs_pool_periodic_trim_enabled{pool="%s"} %d\n' "${pool}" "$([ "${periodic}" = "enable" ] && echo 1 || echo 0)"

  # --- txg の同期時間 -------------------------------------------------------
  # 列: txg birth state ndirty nread nwritten reads writes otime qtime wtime stime
  # state=C が commit 済み。stime はナノ秒。
  txgs="/proc/spl/kstat/zfs/${pool}/txgs"
  if [ -r "${txgs}" ]; then
    awk -v pool="${pool}" '
      $3 == "C" && $12 ~ /^[0-9]+$/ { s = $12 / 1e9; if (s > max) max = s; last = s; n++ }
      END {
        if (n > 0) {
          printf "zfs_pool_txg_sync_seconds_max{pool=\"%s\"} %.6f\n", pool, max
          printf "zfs_pool_txg_sync_seconds_last{pool=\"%s\"} %.6f\n", pool, last
        }
      }' "${txgs}"
  fi

  # --- vdev ごとの TRIM 状態 ------------------------------------------------
  #   sda  ONLINE  0 0 0  (untrimmed)
  #   sda  ONLINE  0 0 0  (52% trimmed, started at ...)
  #   sda  ONLINE  0 0 0  (100% trimmed, completed at ...)
  zpool status -t "${pool}" 2>/dev/null | awk -v pool="${pool}" '
    function emit(dev, st, ratio,   s) {
      split("untrimmed trimming completed unsupported", states, " ")
      for (i in states) {
        s = states[i]
        printf "zfs_pool_trim_state{pool=\"%s\",device=\"%s\",state=\"%s\"} %d\n", pool, dev, s, (s == st ? 1 : 0)
      }
      printf "zfs_pool_trim_progress_ratio{pool=\"%s\",device=\"%s\"} %.4f\n", pool, dev, ratio
    }
    # vdev 行は 5 列目まで数値 3 つ（READ WRITE CKSUM）を持つ葉デバイス
    $2 ~ /^(ONLINE|DEGRADED|FAULTED|OFFLINE|UNAVAIL|REMOVED)$/ && NF >= 5 && $1 != pool {
      dev = $1
      line = $0
      if (line ~ /\(untrimmed\)/)      { emit(dev, "untrimmed", 0); next }
      if (line ~ /trimmed, completed/) { emit(dev, "completed", 1); next }
      if (line ~ /trim unsupported/)   { emit(dev, "unsupported", 0); next }
      # "(52% trimmed, started at ...)" から数値を取り出す。
      # ⚠️ 3 引数の match(s, re, arr) は gawk 拡張で mawk には無い。
      #    Proxmox のホストによって gawk / mawk が混在するため POSIX の
      #    範囲で書く（sub/substr のみ）。
      if (line ~ /[0-9]+% trimmed/) {
        pct = line
        sub(/^.*\(/, "", pct)
        sub(/%.*$/, "", pct)
        if (pct ~ /^[0-9]+$/) { emit(dev, "trimming", pct / 100); next }
      }
    }'
done < <(zpool list -H -o name 2>/dev/null)
