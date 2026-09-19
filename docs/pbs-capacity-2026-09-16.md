# PBS の容量と ext4 予約ブロック — 2026-09-16

> ⚠️ **この文書の結論は 2026-09-19 に覆されました。**
> 続きは [pbs-capacity-2026-09-19.md](pbs-capacity-2026-09-19.md) を読んでください。
>
> 本書は §4 で 13.6 GiB を回収し、`PBSDatastoreFillingUp` まで 32.2 GiB の
> 余裕ができたと結論づけました。**その翌日、1 回のバックアップが 99 GiB を
> 使い切りました。** 1 晩あたりの増加量（当時 97 GiB）を測っていなかったため、
> 「32.2 GiB の余裕」が 1 晩の 1/3 でしかないことに気づけていません。
>
> §3 の「監視が埋まったため無言では進行しない」も誤りでした。15% のしきい値は
> 1 晩ぶんに満たず、警告として機能する時間帯が存在しませんでした。
>
> 本書の作業（`tune2fs -m 2`）自体は正しく、現在も有効です。
> **容量の小細工では届かない問題だった**という点だけが間違っていました。

「prune が ENOSPC で失敗し、容量を空けられなくなった」事象の再発防止として、
PBS の容量構造を調べた。

## 結論（先に）

| | |
|---|---|
| 再発リスク | **残っている**が、以前より低い。監視が埋まったため無言では進行しない |
| 直接の対処 | `tune2fs -m 2` で 13.6 GiB を回収した。**実施済み**（§4） |
| 構造的な原因 | **datastore がルートFS上にある**。これは今回直せない（§5） |

## 1. 何が起きていたのか

```
$ findmnt -no SOURCE,TARGET,FSTYPE,OPTIONS /
/dev/mapper/pbs-root / ext4 rw,relatime,errors=remount-ro

$ cat /etc/proxmox-backup/datastore.cfg
datastore: gateway-backup
	path /backup/gateway-backup        ← ルートFS上。専用の LV も mount も無い

$ df -hT /
/dev/mapper/pbs-root ext4 444G 336G 86G 80% /
```

**バックアップ置き場と OS が同じファイルシステムを共有している。**

ext4 の既定では全体の 5% が root 専用に予約される。

```
Block count:           118472704
Reserved block count:    5923635   → 22.6 GiB (5.0%)
Reserved blocks uid:           0 (user root)
```

PBS の本体は `backup` ユーザーで動く。

```
$ ps -eo user,comm | grep proxmox-backup
backup   proxmox-backup-
root     proxmox-backup-
```

したがって `backup` から見た空きが 0 になる時点で、**root 用に 22.6 GiB が
残ったまま PBS は書けなくなる。** 悪いのは、その状態で prune も動かない
ことである。prune は領域を空ける操作だが、実行自体に書き込みを伴う。

> **容量を空けるための操作が、容量が無いせいで実行できない。**
> これが「一度削除して容量を空けて」という対処が必要になった理由である。

### 1-1. 前回どう抜け出したか

記録（`docs/storage-migration-2026-09-13.md` §50-1）はこうである。

```
journalctl --vacuum-size=150M     # 547.8M → 149.1M、398.6M 解放
→ Available 0 → 312M
```

**tune2fs は使っていない（5% のまま）。** 効いたのは予約の外にある
398.6 MB を空けたことで、312 MB の余地ができて prune が動いた。

つまり **22.6 GiB の予約は、この事象では一度も使われていない。**
むしろ Available を 0 に見せていた側である。§4 の判断はこれを踏まえる。

## 2. ガベージコレクションは正常である

「GC が動いていないから溜まったのでは」を最初に疑ったが、違った。

```
2026-09-16T07:44:04+09:00: Removed garbage: 183.046 GiB
2026-09-16T07:44:04+09:00: Removed chunks: 53354
2026-09-16T07:44:08+09:00: Removed garbage: 0 B      ← 直後の再実行。もう無い
```

`.gc-status` も `pending-chunks: 0` で、回収待ちは無い。

重複排除も効いている。

```
Original data usage:   9.68 TiB
On-Disk usage:         233.128 GiB (2.35%)
Deduplication factor:  42.52
```

**9.68 TiB 相当が 233 GiB に収まっている。** 容量問題は「無駄が溜まって
いる」のではなく、単純に元データが多い。

### 2-1. 一度は誤読した

`du` が 333 GiB を示すのに GC は 233 GiB と報告し、`.chunks` のファイル数
（138,937）も GC の数え（93,497）と合わなかったため、回収漏れを疑った。
実際には GC が 07:44 に走った**後**、07:46〜08:10 に当夜のバックアップが
走っており、その分の新しいチャンクを数えていた。
**片方だけの時刻でスナップショットを取ると食い違う。**

## 3. 監視は埋まっている

以前は無言で壁に当たったが、現在は 2 段で発報する。

| アラート | 条件 | 現在値での余裕 |
|---|---|---|
| `PBSDatastoreFillingUp` | avail/total < 15% | 空き 66.6 GiB を切ると発報 |
| `PBSDatastoreFull` | avail/total < 2% | 空き 8.9 GiB。ここまで来たら手遅れに近い |

§4 の実施前は空き 86 GiB / 19.4% で、警告まで **約 19 GiB** しか無かった。
実施後は 98.7 GiB / 22.2%、警告まで **32.2 GiB** である。

`pbs_observer_datastore_avail_bytes` は statvfs の `f_bavail`（非 root から
見た空き）に由来するため、**予約ブロックを差し引いた値**である。
つまり §4 を実施すると、この比率も実態に即して改善する。

## 4. 実施済み — `tune2fs -m 2`

専用のバックアップ機で 22.6 GiB を root のために確保し続ける意味は薄い。
このホストの OS 全体は **3,929 MiB** しかない。

```
$ du -s --block-size=1M --exclude=/backup --exclude=/proc --exclude=/sys /
3929
```

2% へ下げても root には **9.0 GiB** が残り、OS 全体の 2 倍以上である。
回収できるのは **13.6 GiB**。

```sh
# 実行は online で可能。ダウンタイムも再マウントも不要。
ssh root@172.16.10.51 'tune2fs -m 2 /dev/mapper/pbs-root'

# 確認
ssh root@172.16.10.51 'tune2fs -l /dev/mapper/pbs-root | grep "^Reserved block count"; df -h /'
```

### 4-1. 実施結果（2026-09-16）

```
$ tune2fs -m 2 /dev/mapper/pbs-root
Setting reserved blocks percentage to 2% (2369454 blocks)
```

| | 前 | 後 |
|---|---:|---:|
| Reserved block count | 5,923,635 | 2,369,454 |
| 予約サイズ | 22.6 GiB | 9.0 GiB |
| `df` の空き | 86 GiB | **99 GiB** |
| statvfs `f_bavail` / total | 19.4% | **22.2%** |
| `PBSDatastoreFillingUp`(15%) までの余裕 | 約 19 GiB | **32.2 GiB** |

Prometheus 側にも反映されている。アラートが使う式をそのまま評価した。

```
pbs_observer_datastore_avail_bytes  = 106,013,753,344   (98.7 GiB)
pbs_observer_datastore_total_bytes  = 476,497,756,160
avail / total                       = 22.25%
PBSDatastoreFull / FillingUp        = inactive
```

両メトリクスのラベルは完全に一致しており、除算は結果を返す。
**このアラートは発火し得る**（本セッションで 4 件見つけた
「設定済みだが評価できない」型ではない）。

**元に戻すのはコマンド 1 つである。**

```sh
ssh root@172.16.10.51 'tune2fs -m 5 /dev/mapper/pbs-root'
```

変更前の値は `_out/storage-migration-20260913/pbs-ext4-reserve-before.txt`
に保存してある。

> ⚠️ 0% にはしない。ただし**理由を取り違えないこと。**
>
> 予約は prune の余地にはならない。prune を実行するのは `backup` ユーザーで
> あり、root が CLI を叩いても書き込むのは `backup` である。前回 prune が
> 通ったのは、予約のおかげではなく **journal を削って非予約領域を
> 空けたから**である（§1-1）。
>
> 残す理由は別にある。datastore がルートFSと同居しているため、
> 完全に 0 まで埋まると journald も sshd も書けなくなり、
> **復旧操作そのものができなくなる。** 2% = 9.0 GiB はそのための余地で
> あって、PBS のための余地ではない。

### 4-2. 併せて検討できるもの（未実施）

VG に 16 GiB が未使用のまま残っている。

```
$ vgs
  VG  #PV #LV #SN Attr   VSize    VFree
  pbs   1   2   0 wz--n- <475.94g 16.00g
```

PBS は LVM スナップショットを使わないため、この 16 GiB に用途は無い。
ext4 の拡張は online で行える。

```sh
ssh root@172.16.10.51 'lvextend -l +100%FREE /dev/pbs/root && resize2fs /dev/mapper/pbs-root'
```

§4 と併せて **約 29.6 GiB**（空き 86 → 116 GiB）になる。

⚠️ こちらは `tune2fs` と違って簡単には戻せない。急ぐ必要は無いので、
§4 を先に入れた（実施済み）ので、まず様子を見るのが妥当である。

## 5. 直していない構造的な原因

**datastore をルートFSから分離すべきである。** 分離すれば

- datastore 側は予約 0% にできる（OS が巻き添えにならないため）
- PBS が満杯になっても OS は動き続け、復旧操作が確実にできる

しかし現状 333 GiB のデータがあり、VG の空きは 16 GiB しかない。
root LV を縮めるには offline 作業が要る。**今回の範囲を超える。**

ディスクを増設する機会があれば、そのときに分離すること。

## 6. 付随して確認したこと

### 6-1. バックアップ対象のディスク

```
k8s-1/2/3 (controlplane)   scsi0 60G  backup=1     scsi1 300G  backup=0
k8s-worker-1/2/3           scsi0 60G  backup=1     scsi1 300G  backup=1
ai-gateway-01              scsi0 64G  （定期ジョブの対象外）
```

制御プレーンの Longhorn ディスク（scsi1）は除外が効いている。
ワーカーの scsi1 は対象のままだが、これは意図どおりである
— ADR-0012 のとおり **PBS が Longhorn データの唯一のクラスタ外コピー**
だからである。

### 6-2. vm/1200 の最終バックアップは 2026-09-05

11 日前だが、これは既知の除外である。`kubernetes-daily-pbs` ジョブの
対象は 1001-1003 / 1101-1103 で、1200 は含まれない。
`PBSBackupStale` も `{backup_id!="1200"}` で明示的に外してあり、
その理由がアラートのコメントに書かれている。**見落としではない。**
