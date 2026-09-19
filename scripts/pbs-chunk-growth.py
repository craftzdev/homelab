#!/usr/bin/env python3
"""PBS データストアの増加量を、原因が分かる形で計測する。

    scp scripts/pbs-chunk-growth.py root@172.16.10.51:/tmp/
    ssh root@172.16.10.51 'python3 /tmp/pbs-chunk-growth.py'

PBS の Web UI と `proxmox-backup-manager datastore list` が出すのは
「いま何 GiB 使っているか」だけで、**なぜ増えたか**は分からない。
2026-09-19 の障害では、そこが分からないまま 3 日で 99 GiB を失った。

このスクリプトは 3 つを出す。

1. 日ごとの新規チャンク量 — チャンクファイルの mtime から。
   「1 晩のバックアップが何 GiB 積み増すか」が分かる。保持世代数を
   決めるにはこの値が要る（保持世代 × この値 ＋ 1 世代ぶん ≒ 必要容量）。

2. ディスク種別ごとの占有量 — どの .fidx だけが参照しているチャンクか。
   「どれを対象外にすれば何 GiB 戻るか」が分かる。

3. 種別をまたいで共有されているチャンク量 — 重複排除が効いているかどうか。
   ここがほぼ 0 なら、PBS は同じ内容を何重にも保存している。
   暗号化されたディスク（Talos の LUKS2 EPHEMERAL）と、ノードごとに
   レイアウトが違う Longhorn レプリカがこの状態になる。

背景と実測値は docs/pbs-capacity-2026-09-19.md を参照。
"""

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

GIB = 2**30
# fixed index (.fidx): 4096 バイトのヘッダ + 32 バイトのチャンクダイジェストの配列
FIDX_HEADER = 4096
DIGEST_LEN = 32


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--datastore", default="/backup/gateway-backup",
                   help="datastore のパス（既定: /backup/gateway-backup）")
    p.add_argument("--days", type=int, default=14,
                   help="日別集計をさかのぼる日数（既定: 14）")
    return p.parse_args()


def read_digests(path):
    """.fidx から、ゼロ（穴）以外のチャンクダイジェストを集合で返す。"""
    with open(path, "rb") as f:
        f.seek(FIDX_HEADER)
        body = f.read()
    zero = b"\0" * DIGEST_LEN
    return {
        body[i:i + DIGEST_LEN]
        for i in range(0, len(body) - DIGEST_LEN + 1, DIGEST_LEN)
        if body[i:i + DIGEST_LEN] != zero
    }


def chunk_path(chunks_dir, digest):
    h = digest.hex()
    return os.path.join(chunks_dir, h[:4], h)


def daily_growth(chunks_dir, days):
    """チャンクファイルの mtime から、日ごとの新規バイト数を数える。

    チャンクは内容ハッシュで名前が付き、一度書かれたら書き換わらない。
    したがって mtime ＝ そのチャンクが初めて現れた時刻であり、
    日ごとに足せば「その晩のバックアップが新しく要求した量」になる。
    """
    per_day = defaultdict(lambda: [0, 0])  # date -> [bytes, count]
    cutoff = (datetime.now() - timedelta(days=days)).timestamp()
    for root, _, files in os.walk(chunks_dir):
        for name in files:
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            day = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
            per_day[day][0] += st.st_size
            per_day[day][1] += 1
    return per_day


def classify(vmid, disk, control_ids):
    role = "control" if vmid in control_ids else "worker"
    return f"{role}/{disk}"


def collect_indexes(datastore):
    """(種別 -> ダイジェスト集合) と、種別ごとのスナップショット数を返す。"""
    owners = defaultdict(set)      # digest -> {種別}
    snapshots = defaultdict(int)   # 種別 -> スナップショット数
    # control-plane と worker の区別は vmid の桁で決まる（10xx / 11xx）。
    # docs/10-network-design.md の割り当てに従う。
    control_ids = {"1001", "1002", "1003"}

    vm_root = os.path.join(datastore, "vm")
    if not os.path.isdir(vm_root):
        sys.exit(f"vm グループが見つかりません: {vm_root}")

    for vmid in sorted(os.listdir(vm_root)):
        group = os.path.join(vm_root, vmid)
        if not os.path.isdir(group):
            continue
        for snap in sorted(os.listdir(group)):
            snap_dir = os.path.join(group, snap)
            if not snap.endswith("Z") or not os.path.isdir(snap_dir):
                continue
            for name in sorted(os.listdir(snap_dir)):
                if not name.endswith(".fidx"):
                    continue
                disk = name.replace("drive-", "").replace(".img.fidx", "")
                cls = classify(vmid, disk, control_ids)
                snapshots[cls] += 1
                for d in read_digests(os.path.join(snap_dir, name)):
                    owners[d].add(cls)
    return owners, snapshots


def main():
    args = parse_args()
    chunks_dir = os.path.join(args.datastore, ".chunks")
    if not os.path.isdir(chunks_dir):
        sys.exit(f"datastore が見つかりません: {args.datastore}")

    print(f"datastore: {args.datastore}\n")

    print(f"=== 1. 日ごとの新規チャンク量（直近 {args.days} 日） ===")
    print("   1 晩ぶんの増加量。保持世代数はこの値から決める。")
    per_day = daily_growth(chunks_dir, args.days)
    if not per_day:
        print("   （この期間に新しいチャンクはありません）")
    for day in sorted(per_day):
        size, count = per_day[day]
        bar = "#" * int(size / GIB / 2)
        print(f"   {day}  {size / GIB:7.1f} GiB  {count:7,} chunks  {bar}")

    owners, snapshots = collect_indexes(args.datastore)
    sizes = {}

    def size_of(digest):
        if digest not in sizes:
            try:
                sizes[digest] = os.path.getsize(chunk_path(chunks_dir, digest))
            except OSError:
                sizes[digest] = 0
        return sizes[digest]

    exclusive = defaultdict(int)
    shared = 0
    total = 0
    for digest, classes in owners.items():
        s = size_of(digest)
        total += s
        if len(classes) == 1:
            exclusive[next(iter(classes))] += s
        else:
            shared += s

    print(f"\n=== 2. ディスク種別ごとの占有量（合計 {total / GIB:.1f} GiB / "
          f"ユニーク {len(owners):,} チャンク） ===")
    print("   その種別だけが参照しているチャンク ＝ 対象外にすれば戻る量。")
    for cls in sorted(exclusive, key=lambda c: -exclusive[c]):
        print(f"   {cls:16s} snapshots={snapshots[cls]:3d}   "
              f"{exclusive[cls] / GIB:7.1f} GiB")

    print("\n=== 3. 重複排除が効いているか ===")
    ratio = shared / total * 100 if total else 0
    print(f"   種別をまたいで共有されているチャンク: {shared / GIB:.2f} GiB "
          f"（{ratio:.2f}%）")
    if ratio < 1:
        print("   ⚠️ ほぼ 0 である。PBS は同じ内容を種別ごとに別々に保存している。")
        print("      原因になりやすいのは次の 2 つ:")
        print("      - ディスクが暗号化されている（Talos の LUKS2 EPHEMERAL）。")
        print("        鍵がノードごとに違うため、同じイメージでも別チャンクになる")
        print("      - Longhorn のレプリカ。中身は同じだがノードごとに配置が違う")
        print("      この状態では保持世代を増やすコストが世代数に比例する。")


if __name__ == "__main__":
    main()
