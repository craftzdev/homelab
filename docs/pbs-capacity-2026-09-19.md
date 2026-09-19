# PBS が常に満杯になる理由 — 2026-09-19

[docs/pbs-capacity-2026-09-16.md](pbs-capacity-2026-09-16.md) で `tune2fs -m 2` を
実施し、空きを 86 → 99 GiB に戻した。その **翌日の 1 回のバックアップで
99 GiB を使い切った。** 09-18 と 09-19 のバックアップは ENOSPC で失敗し、
[2026-09-19 のモシトク接続障害](incidents/2026-09-19-moshitoku-outage.md) に波及した。

本書は「なぜ空けても空けてもすぐ埋まるのか」を計測で特定した記録である。

## 結論（先に）

| | |
|---|---|
| 直接の原因 | 1 晩のバックアップが **97 GiB** を消費していた |
| 根本原因 | **PBS の重複排除と圧縮が構造的に効いていない。** 同じデータを 3 重・非圧縮で保存している |
| なぜ気づけなかったか | 15% 警告に猶予が無かった。22% → 0.4% が **1 回のバックアップの中**で起きる |
| 本質的な誤り | `keep-daily=7` は 431 GiB に対して **一度も成立したことが無い**設定だった |

09-16 の `tune2fs` で回収した 13.6 GiB は正しい作業だったが、
**1 晩ぶん（97 GiB）の 1/7 にしかならない。** 容量の小細工では届かない問題である。

## 1. 何が起きたか

Prometheus に残っていた空き容量の推移。

```
09-16 18:30     98.7 GiB   ← tune2fs -m 2 実施後
09-17 00:30     98.8 GiB
09-17 06:30      1.9 GiB   ← 02:30 のバックアップ 1 回
09-18 06:30      0.0 GiB
09-19 06:30      0.0 GiB
```

**6 時間で 97 GiB。** じわじわ増えたのではなく、1 回のジョブで落ちている。

チャンクファイルの mtime から日ごとの新規量を数えると、これは事故ではなく
恒常的で、しかも**増えている**ことが分かる。

```
2026-09-11     63.4 GiB    21,993 chunks
2026-09-12     64.6 GiB    28,142 chunks
2026-09-13     73.4 GiB    34,130 chunks
2026-09-16     98.3 GiB    45,440 chunks
2026-09-17     96.8 GiB    36,796 chunks   ← ここで満杯
```

> 計測は `scripts/pbs-chunk-growth.py` で再現できる。チャンクは内容ハッシュで
> 名前が付き書き換わらないため、mtime ＝ そのチャンクが初めて現れた時刻である。

### 1-1. 保持ポリシーは最初から成立していなかった

datastore に使える領域は次のとおり。

| | |
|---|---:|
| ext4 全体 | 444 GiB |
| − OS 全体 | 3.8 GiB |
| − ext4 予約 2% | 9.0 GiB |
| **datastore に使える** | **431 GiB** |

これに対し `keep-daily=7` は 1 晩 97 GiB × 7 ＝ **680 GiB** を要求する。
weekly 4 / monthly 3 まで含めれば 1.3 TiB 相当になる。

**入るはずが無かった。** 2026-09-06 の運用開始から片道で埋まり続け、
09-17 に上限へ到達しただけである。09-16 時点で「空き 22%」に見えていたのは、
**まだ 7 世代ぶんが溜まりきっていなかったから**にすぎない。

## 2. 根本原因 — 重複排除が効いていない

PBS の容量効率は重複排除が前提である。それが効いていない。

`scripts/pbs-chunk-growth.py` の §3 が直接それを示す。

```
種別をまたいで共有されているチャンク: 0.10 GiB （0.02%）
```

428 GiB のうち、複数のディスク種別が共有しているのは **0.10 GiB しか無い。**
3 台のワーカーが持つ Longhorn データ（中身は同じ 3 レプリカ）に絞っても同じである。

```
vm/1101 の最新 scsi1: 17,867 chunks
vm/1102 の最新 scsi1: 23,763 chunks
vm/1103 の最新 scsi1: 13,892 chunks

  1 ノードだけが参照:   99.21 GiB
  2 ノードが参照    :    0.06 GiB
  3 ノードが参照    :    0.00 GiB
```

**共有は 0.06%。** PBS から見ると、3 レプリカは 3 つの無関係なデータである。

理由はディスクごとに違う。

### 2-1. scsi0 — Talos の EPHEMERAL が LUKS2 で暗号化されている

```
$ talosctl -n 172.16.40.21 get volumestatus EPHEMERAL -o yaml
    location: /dev/sda6
    mountLocation: /dev/dm-1
    filesystem: xfs
    encryptionProvider: luks2
    configuredEncryptionKeys:
        - nodeID
```

`talos/patches/worker.yaml.tftpl` の `systemDiskEncryption` による意図した設定で、
**これ自体は正しい。** だがバックアップから見ると 3 つの効果がある。

1. **鍵がノードごとに違う**（`nodeID`）。3 台に同じコンテナイメージがあっても、
   暗号文は完全に別物になる。ノード間の重複排除が原理的に不可能である
2. **圧縮が効かない。** ZFS の実測でも `compressratio 1.00x` である
3. **1 セクタの変更が 4 MiB を道連れにする。** PBS の固定インデックスは
   4 MiB 単位で、その中の 1 セクタでも暗号文が変われば chunk 全体が新規になる

結果、毎晩 OS ディスクの **40〜62%** が新規チャンクとして積まれていた。

```
vm/1101 drive-scsi0  chunks= 9764  new= 6064 (62.1%)  new_on_disk= 23.66 GiB
vm/1102 drive-scsi0  chunks= 6380  new= 2493 (39.1%)  new_on_disk=  9.73 GiB
vm/1103 drive-scsi0  chunks= 6770  new= 3457 (51.1%)  new_on_disk= 13.48 GiB
```

変化はファイルの更新箇所に偏っていない。ディスク 60 GiB 全体にわたって
**周期約 10 GiB の帯**として現れる（XFS の 6 つのアロケーショングループに
割り当てが回っている形）。使用量が 25.57 GiB しか無いディスクで、
毎晩 23.66 GiB が新規になる理由はここにある。

### 2-2. scsi1 — Longhorn のレプリカはノードごとに配置が違う

こちらは暗号化されていない（`filesystem: type: xfs` のみ、ZFS の
`compressratio` も 1.42x）。それでも共有が 0.06% なのは、Longhorn が
レプリカをスパースファイルとして持ち、その配置がノードごとに異なるためである。
論理的な中身が同じでも、ブロックデバイスとしては別の並びになる。

**PBS は同じデータを 3 回、別物として保存している。**

## 3. 何がどれだけ占めていたか

```
=== ディスク種別ごとの占有量（合計 428.2 GiB / ユニーク 175,733 チャンク） ===
   worker/scsi1     snapshots= 18     214.8 GiB
   worker/scsi0     snapshots= 21     166.9 GiB
   control/scsi0    snapshots= 22      46.5 GiB
```

1 世代あたりに直すと次のとおり。

| 種別 | 1 世代 | 1 晩の増分 | 中身 |
|---|---:|---:|---|
| worker scsi1 | 99.3 GiB | 41.5 GiB | Longhorn。うち **Prometheus TSDB が 30.5×3＝91.7 GiB** |
| worker scsi0 | 89.1 GiB | 46.9 GiB | コンテナイメージ・Pod ログ・emptyDir |
| control scsi0 | 16.6 GiB | 8.5 GiB | etcd を含む |

Longhorn の実データは全体で約 153 GiB、その **6 割が Prometheus** である
（`kubectl -n longhorn-system get volumes.longhorn.io`、actualSize 30.56 GiB × 3 レプリカ）。
TSDB はコンパクションでブロックを丸ごと書き直すため、churn の主因でもある。

[ADR-0012](adr/0012-backup-strategy-revisited.md) は「代替不能なのは 231 MB
（moshitoku 200 MB / umami 31 MB）だけで、それは既に R2 にある」と結論づけている。
**容量の大半は、守る必要が無いと自分で結論づけたデータが占めていた。**

## 4. なぜ無警告で壁に当たったのか

監視は壊れていなかった。`PBSDatastoreFull` / `PBSDatastoreFillingUp` /
`PBSBackupTaskFailing` はいずれも正しく firing している。

問題は **しきい値に猶予が無かった**ことである。

- `PBSDatastoreFillingUp` は空き 15% ＝ 65 GiB で鳴る
- 当時の 1 晩は 97 GiB

**65 GiB は 1 晩に満たない。** つまり「警告が出てから満杯になるまで」の時間帯が
存在しない。09-17 の実測どおり、22% から 0.4% へ 1 回のジョブの中で落ちる。
発報はしたが、そのときには既に手遅れだった。

> しきい値による監視は「じわじわ減る」ことを前提にしている。
> PBS の使用量は 02:30 に階段状に乗る。**形が合っていなかった。**

## 5. 対処

### 5-1. 実施済み（2026-09-19）

| | |
|---|---|
| ENOSPC で失敗した未完了バックアップの残骸を削除 | `vm/1101` と `vm/1102` の `2026-09-17T17:30:21Z`。`.tmp_fidx` のみで完了していない |
| journal を切り詰め | 94.5M → 16M（78.5M 解放）。prune を実行する最低限の余地を作るため |

変更前の状態は PBS の `/root/pbs-capacity-20260919/` に保存してある
（`prune.cfg.before` / `snapshots.before`）。

### 5-2. コード側の変更（本コミット）

| ファイル | 変更 |
|---|---|
| `tofu/10-proxmox-talos/vms.tf` | worker の scsi0 を `backup = false` に。control-plane は etcd があるため維持 |
| `scripts/reconcile-pbs-kubernetes-backup.sh` | 保持を `keep-daily=7,keep-weekly=4,keep-monthly=3` → `keep-daily=3` |
| `kubernetes/infra/monitoring/alerts.yaml` | `PBSDatastoreFillingUp` を 15% → 30%。`PBSDatastoreWillFill`（`predict_linear`）を追加 |
| `scripts/pbs-chunk-growth.py` | 新規。増加の原因を切り分ける計測スクリプト |

#### worker の scsi0 を外す理由

**優先度が低いからではなく、取ると PBS が破綻するからである。**
1 世代 89.1 GiB・毎晩 46.9 GiB を要求し、しかも §2-1 のとおり
その大半が重複排除の効かない暗号文である。

失うものは無い。EPHEMERAL の中身はコンテナイメージ・Pod ログ・emptyDir で、
いずれも再取得できる。worker の代替不能なデータは Longhorn（scsi1）にあり、
そちらは取り続ける。復旧経路も変わらない — worker が壊れたら tofu で作り直し、
`talosctl apply-config` で再参加させ、Longhorn が他の 2 台からレプリカを
再構築する。**5 日前の EPHEMERAL イメージを書き戻すより速く確実である。**

#### weekly / monthly を残さない理由

重複排除が効かないため、**1 世代前の weekly は今日とほとんどチャンクを
共有しない。** 1 世代ごとにほぼフルコピー（約 116 GiB）を要求し、
daily の差分（49.9 GiB）より高くつく。世代を持つこと自体が高価である。

### 5-3. 保持ポリシーの適用（実施済み・2026-09-19）

PBS 側の prune ジョブを `keep-daily 3` のみにし、prune を実行した。
各グループが 3 世代（09-12/13・09-15・09-16）になり、
参照されているデータは 428 GiB → **335.9 GiB** になった。

> ⚠️ **最初 `--keep-last 3 --keep-daily 3` を指定して失敗した。**
> 詳細は §6-1。PBS の prune オプションは**加算**である。

### 5-4. 適用の結果（2026-09-19 完了）

§6 の手順 1〜6 をすべて実施した。

| | 実施前 | 実施後 |
|---|---:|---:|
| `df` の使用率 | **100%**（空き 1016K） | **79%**（空き 95 GB） |
| 参照されているデータ | 428.2 GiB | **335.9 GiB** |
| 保持世代 | 各グループ 6〜8 | **各グループ 3** |
| バックアップジョブ | `enabled 0`（09-17 以降停止） | **`enabled 1` / `keep-daily=3`** |

Proxmox 側のバックアップ対象フラグは次のとおりになった。

```
1001-1003 (control)  scsi0: backup=1   scsi1: backup=0
1101-1103 (worker)   scsi0: backup=0   scsi1: backup=1
```

GC は 92.339 GiB を回収した。§6-2 のとおり atime の猶予を一時的に 5 分へ
縮める必要があり、**回収後に既定へ戻してある**
（`datastore.cfg` に `tuning` 行が無いことを確認済み）。

#### 移行期間の見通し

79% は定常値ではない。残っている 3 世代が **まだ worker の scsi0 を
含んでいる**ためで、その約 120 GiB が世代交代で抜けるまでの一時的な値である。

内訳（335.9 GiB）:

| | |
|---|---:|
| worker scsi1 × 3 世代 | 182.3 GiB |
| **worker scsi0 × 3 世代（旧世代のみ。今後は増えない）** | **120.1 GiB** |
| control scsi0 × 3 世代 | 33.5 GiB |

| 時点 | 使用率 |
|---|---|
| 現在 | 79% |
| 09-20 02:30 のバックアップ後 | 約 **87%**（+49.9 GiB）。**この夜が最も窮屈である** |
| 09-21 00:00 の GC 後 | 約 70%（旧世代 1 つぶんの worker scsi0 を回収） |
| 09-22 00:00 の GC 後 | 約 58% |
| 09-23 00:00 の GC 後 | 約 **50% で定常**（＝ 115.9 + 2 × 49.9 ＝ 215.7 GiB） |

⚠️ 移行が終わるまで `PBSDatastoreFillingUp`（空き 30% 未満）は鳴り続ける。
現在の空きは 21% で、09-20 の朝は 13% まで落ちる。**想定内である。**
09-23 以降は空き 50% になるので自然に収まる。

収まらない場合、または `PBSDatastoreWillFill` が鳴る場合は、
1 晩あたりの増分が見積もり（49.9 GiB）を超えている。
`scripts/pbs-chunk-growth.py` で実測すること。

## 6. 適用手順

前提: PBS は満杯で prune の実行にも余地が要る。§5-1 で 81 MB を確保済み。

```sh
# --- 1. 保持ポリシーを実測に合わせる（PBS 側の prune ジョブ）
#     keep-daily だけにする。keep-last を併記してはいけない（§6-1）
ssh root@172.16.10.51 \
  'proxmox-backup-manager prune-job update default-gateway-backup-532eb832- \
     --keep-daily 3 --delete keep-last --delete keep-weekly --delete keep-monthly'

# --- 2. prune を実行して古い世代を削除（⚠️ 取り消せない）
#     残るのは各グループの最新 3 世代
ssh root@172.16.10.51 \
  'proxmox-backup-manager prune-job run default-gateway-backup-532eb832-'

# --- 3. GC で実際にチャンクを回収する。prune だけでは空き容量は戻らない
#     ⚠️ ただし既定では 24 時間分は回収されない（§6-2）
ssh root@172.16.10.51 \
  'proxmox-backup-manager garbage-collection start gateway-backup'
ssh root@172.16.10.51 'df -h /'
```

**4. worker の OS ディスクをバックアップ対象から外す**

⚠️ `qm` は**そのノードが持つ VM にしか使えない。** worker は 3 台とも別ノードに
いるため（1101→sv-proxmox-01 / 1102→sv-proxmox-02 / 1103→sv-proxmox-03）、
`qm config 1102` を sv-proxmox-01 で叩くと
`Configuration file ... does not exist` になる。クラスタ全体に効く `pvesh` を使う。

⚠️ ディスクの指定は**既存の値をそのまま書き直し、`backup=1` を `backup=0` に
変えるだけ**にする。ボリューム ID や `size` を省いた指定はディスクの
差し替えとして解釈され得る。`--delete scsi0` はディスクそのものを外す操作なので
使わない。

```sh
for spec in 1101:sv-proxmox-01 1102:sv-proxmox-02 1103:sv-proxmox-03; do
  id=${spec%%:*}; node=${spec##*:}
  cur=$(ssh root@172.16.10.11 "pvesh get /nodes/$node/qemu/$id/config --output-format json" \
          | python3 -c 'import json,sys; print(json.load(sys.stdin)["scsi0"])')
  new=${cur/backup=1/backup=0}
  [ "$new" != "$cur" ] || { echo "$id: 変更不要 ($cur)"; continue; }
  echo "$id ($node): $new"
  ssh root@172.16.10.11 "pvesh set /nodes/$node/qemu/$id/config --scsi0 '$new'"
done

# 反映確認: worker は backup=0、control-plane は backup=1 のままであること
for spec in 1001:sv-proxmox-01 1101:sv-proxmox-01 1102:sv-proxmox-02 1103:sv-proxmox-03; do
  id=${spec%%:*}; node=${spec##*:}
  echo "$id: $(ssh root@172.16.10.11 "pvesh get /nodes/$node/qemu/$id/config --output-format json" \
                 | python3 -c 'import json,sys; print(json.load(sys.stdin)["scsi0"])')"
done
```

**5〜7. tofu との整合確認・ジョブ再開・翌朝の確認**

```sh
# --- 5. tofu 側と一致していることを確認（この disk 属性に差分が出ないこと）
cd tofu/10-proxmox-talos && tofu plan

# --- 6. バックアップジョブを保持 3 世代で再開
./scripts/reconcile-pbs-kubernetes-backup.sh          # 確認のみ
./scripts/reconcile-pbs-kubernetes-backup.sh --apply

# --- 7. 翌朝、1 晩の増分が 50 GiB 程度に収まったことを確認する
scp scripts/pbs-chunk-growth.py root@172.16.10.51:/tmp/
ssh root@172.16.10.51 'python3 /tmp/pbs-chunk-growth.py'
```

### 6-1. ⚠️ PBS の prune オプションは加算である

`--keep-last 3 --keep-daily 3` は「3 世代」ではない。**6 世代残る。**

各オプションは独立に枠を持ち、**先のオプションが既に残したスナップショットは、
後のオプションの枠を消費しない。** 実際に起きたことは次のとおりである。

```
keep-last 3  → 最新 3 つ（09-16, 09-15, 09-12）を残す
keep-daily 3 → まだ使われていない日のうち新しい 3 日
               （09-11, 09-10, 09-06）を残す
             → 合計 6 世代。1 つも消えないグループがあった
```

7 グループすべてが正確に 6 世代（＝3＋3）残り、`df` は 100% のままだった。

**世代数を意図どおりにしたければ、オプションは 1 つだけにすること。**
`keep-daily 3` のみなら、各グループは直近 3 日ぶんの最新スナップショットを
1 つずつ、合計 3 世代だけ残す。

> 同じことが vzdump 側の `--prune-backups` にも当てはまる。
> `scripts/reconcile-pbs-kubernetes-backup.sh` が `keep-daily=3` だけを
> 渡しているのはこのためである。**項目を足すと世代数が増える。**

### 6-2. ⚠️ GC は 24 時間ぶんを回収しない

prune の直後に GC を回しても、空き容量は戻らない。

```
Removed garbage: 0 B
Pending removals: 92.339 GiB (in 35316 chunks)
```

PBS の GC は atime で判定する。参照されているチャンクの atime を更新し、
**「最後に触られてから 24 時間（既定）経った」チャンクだけを削除する。**
これは書き込み中のバックアップが持つチャンクを誤って消さないための保護である。

prune で参照が外れたチャンクは、その直前の GC が atime を更新しているため、
**24 時間経つまで消えない。** `Pending removals` がその量である。

急ぐ場合は猶予を一時的に縮める。**実行中のバックアップが無いことを
必ず確認してから**行うこと。

```sh
ssh root@172.16.10.51 'cat /var/log/proxmox-backup/tasks/active'   # 空であること

ssh root@172.16.10.51 \
  'proxmox-backup-manager datastore update gateway-backup --tuning gc-atime-cutoff=5'
ssh root@172.16.10.51 'proxmox-backup-manager garbage-collection start gateway-backup'

# ⚠️ 必ず既定に戻す。戻し忘れると、バックアップ実行中の GC が
#    アップロード途中のチャンクを削除し得る
ssh root@172.16.10.51 \
  'proxmox-backup-manager datastore update gateway-backup --delete tuning'
ssh root@172.16.10.51 'grep tuning /etc/proxmox-backup/datastore.cfg'   # 出力が無いこと
```

### 6-3. `keep-daily=3` は暫定ではなく、当面の定常値である

当初は「移行が済んだら 5 世代へ上げる」つもりだったが、**上げない。**
数字を詰めたところ、5 世代は警告の余地を食い潰すことが分かった。

1 世代 115.9 GiB ＋ 1 晩 49.9 GiB（いずれも worker scsi0 除外後の実測）。
使用可能な領域は 431 GiB である。

| 保持 | 使用量 | 使用率 | 空き | 空きは何晩ぶんか |
|---|---:|---:|---:|---:|
| **3 世代** | **215.7 GiB** | **50%** | **215 GiB** | **4.3 晩** |
| 4 世代 | 265.6 GiB | 62% | 165 GiB | 3.3 晩 |
| 5 世代 | 315.5 GiB | 73% | 115 GiB | 2.3 晩 |
| 7 世代 | 415.3 GiB | 96% | 16 GiB | 0.3 晩 |

5 世代にすると空きが 27% になり、**`PBSDatastoreFillingUp`（30%）が
恒常的に発報する。** 鳴りっぱなしの警告は読まれなくなる — それは
15% のしきい値が機能しなかったのと同じ失敗である（§4）。

**この構成では、保持世代を増やすことと、容量の警告が機能することは
両立しない。** 1 晩が全体の 11.6% を占めるためである。

5 世代以上にしたければ、先に 1 晩あたりの増分を減らすこと。
手立ては §7-2（Prometheus が Longhorn の 6 割を占めている）にある。
増分が半分になれば 7 世代でも 48% に収まり、警告の余地も残る。

> 上げる場合、prune の指定は **`keep-daily` だけ**にすること。
> `--keep-last 5 --keep-daily 5` は 10 世代になる（§6-1）。
>
> ```sh
> PRUNE_BACKUPS=keep-daily=5 ./scripts/reconcile-pbs-kubernetes-backup.sh --apply
> ssh root@172.16.10.51 \
>   'proxmox-backup-manager prune-job update default-gateway-backup-532eb832- \
>      --keep-daily 5'
> ```

## 7. 直していない構造的な原因

### 7-1. datastore がルートFSと同居している（[09-16 の §5](pbs-capacity-2026-09-16.md) から未解決）

満杯になると prune も GC も書き込めず、**容量を空ける操作が容量不足で
実行できない。** 今回も先に journal を削って余地を作る必要があった。
09-19 の障害で OS 側まで巻き添えになったのも同じ構造である。

PBS は 512GB SSD 1 本のベアメタルで、空きスロットも VG の空きも無い
（VG の空きは 16 GiB のみ）。**分離にはディスク増設が要る。**
増設すれば datastore 側を予約 0% にでき、満杯でも OS は生き残り、
prune / GC が必ず実行できる。**次にハードを触る機会に必ず入れること。**

### 7-2. Prometheus が Longhorn の 6 割を占めている

worker scsi1 の 99.3 GiB／世代のうち大半が Prometheus TSDB（30.5 GiB × 3 レプリカ）
である。ここを削れば `keep-daily=7` を取り戻せる。

| 案 | 効果 | 代償 |
|---|---|---|
| 保持期間 15 日 → 5 日 | 1 世代 99.3 → 約 50 GiB | 履歴が短くなる |
| Longhorn レプリカ 3 → 1 | 同程度 | ノード障害で監視データを失う |

ADR-0012 が「監視データは再取得可能」と位置づけている以上、
どちらも取り得る。**今回は実施しない**（バックアップ再開を優先する）。
`keep-daily=5` で運用してみて、それでも窮屈なら検討する。

### 7-3. ランサムウェア対策の遡及窓が 5 日になる

ADR-0012 の S6 は「PBS のみで受容する」としている。その PBS の遡及窓が
daily 7 + weekly 4 + monthly 3（名目上 3 か月）から **実質 5 日**になる。

名目が実態と違っていたのは元からである（7 世代すら入っていなかった）。
**5 日は、初めて実際に保持できている数字である。**
窓を伸ばしたければ 7-1 のディスク増設か 7-2 が要る。
ADR-0012 の更新が必要になった時点で本書を参照すること。
