# ストレージ移行 作業記録 — 2026-09-13 〜 09-14

対象指示書：`docs/claude-code-storage-migration-instructions.md`

作業時間：2026-09-13 18:50 JST 〜 2026-09-14 01:30 JST（JST表記）。

この文書は追記式の作業ログである。§1〜§18 はステップ0（調査）時点の記録で、
**一部の結論は §22 以降で訂正されている。** 通して読む場合は
本セクションの要約 → §24〜§26（根本原因）→ §41（総括）の順が早い。

## 要約

### 何が起きていたか

Longhornのレプリカ再構築が完走せず、失敗と再試行を繰り返して縮退ボリュームが
増え続けていた（3個 → 7個）。Prometheus・Loki・MinIO はコピーが1個まで減り、
すべて worker-3 に集中していた。MinIOはCNPGバックアップの保存先であり、
worker-3 には両DBの主系も載っていたため、1筐体の喪失でDB主系とバックアップを
同時に失う状態だった。

### 原因

**ZFSプール `local-zfs` が作成以来一度もTRIMされていなかった。**

Debianの `/etc/cron.d/zfsutils-linux` には毎月のTRIMが設定されているが、
`/usr/lib/zfs-linux/trim` は既定値 `auto` のとき `pool_is_nvme_only()` を
要求する。`local-zfs` はSATA単体プールのため、cronは毎月起動しては
何もせず終了していた（§24）。

ドライブは通電約3.4年で、ADR-0009のCeph廃止時に再利用されている。
ZFSプールを作り直してもSSDのFTLには通知されないため、
プール使用率7%にもかかわらずFTLは空きブロックを認識できず、
GCで飽和していた。プール作成（2026-09-06 03:01）から**7日で壊滅**した（§25）。

### 対処

1. Longhornの再構築を一時停止し、自己増幅ループを止めた（§9-2）
2. ホスト01の `zpool trim` で仮説を検証（§15、§20）
3. ホスト02を対照群として残し、本番負荷でA/B検証（§21）
4. 恒久対策 `autotrim=on` ＋ `org.debian:periodic-trim=enable` を3ホストへ（§27）
5. 再試行上限に達した空レプリカを削除して再構築を再開（§31、§33）
6. ホスト02・03もTRIM（§35、§37）

### 結果

| 指標 | 対処前 | 対処後 |
|---|---:|---:|
| SATA write await（ホスト01） | 190〜263 ms | **1 ms** |
| SATA write await（ホスト02） | 319 ms | **1 ms** |
| busy | 88〜91% | **1%** |
| loadavg（ホスト01） | 21.97 | 2.38 |
| ZFS txg sync | 95MB / 51.8s | 1MB / 0.0s |
| 縮退ボリューム | 7 / 22 | **0 / 22** |
| 失敗レプリカ | 継続的に発生 | **0** |
| Prometheus再構築 | 25分で4%→20%後に失敗 | **数分で完走** |
| ZFSレプリケーション `1200-1` | snapshot timeout で失敗継続 | **2.9秒 / FailCount 0** |

**指示書ステップ1「SATA上の再構築ループと複製不足を解消」は完了した。**
ただし対処内容は指示書の想定と異なる（§22）。

**指示書ステップ2〜4（NVMeへのDB・worker OS移行）は実施していない。**
利用者判断により、前提が変わったため一旦停止して再評価する（§30、§41）。

### サービス中断

**なし。** 全期間を通じてノード6台はReadyを維持し、CNPG両クラスタは
3/3 Ready を維持した。VM再作成・再起動・Talos reset・ディスクフォーマットは
一切行っていない。

### 一時変更はすべて復帰済み（§38）

Gitの宣言設定（`values.yaml` 等）は変更していない。

---


## 1. 確認できた構成（指示書の記載と一致）

| 物理ホスト | 管理IP | SATA | NVMe | control VM | worker VM |
|---|---|---|---|---|---|
| sv-proxmox-01 | 172.16.10.11 | SUNEAST SE800 Lite 1024GB | CT500P2SSD8 465.8G | k8s-1 / 1001 | k8s-worker-1 / 1101 |
| sv-proxmox-02 | 172.16.10.12 | SUNEAST SE800 Lite 1024GB | CT500P2SSD8 465.8G | k8s-2 / 1002 | k8s-worker-2 / 1102 |
| sv-proxmox-03 | 172.16.10.13 | P3-1TB | CT500P1SSD8 465.8G | k8s-3 / 1003 | k8s-worker-3 / 1103 |

- Proxmox VE 9.0.11、クラスタ名 `homelab`、3ノードQuorate。
- Kubernetes v1.34.3 / Talos v1.13.9、6ノードすべて Ready。
- ローカル talosctl v1.14.0（サーバー v1.13.9 より新しい。指示書の指摘どおり）。
- Longhorn chart/manager/engine すべて **v1.12.1**、v1 data engine。

ディスク配置（`qm config` 実測）：

- control：`scsi0 = local-lvm:vm-100X-disk-0` 60G（NVMe thin）
  ＋ `scsi1 = local-zfs:vm-100X-disk-1` 300G `serial=longhorn`（**実使用 816K＝実質未使用**）
- worker：`scsi0 = local-zfs:vm-110X-disk-0` 60G ＋ `scsi1 = local-zfs:vm-110X-disk-1` 300G `serial=longhorn`

ZFS zvol の実使用量：

| VM | OS (60G) | Longhorn (300G) |
|---|---:|---:|
| 1101 worker-1 | 21.8G | 41.8G |
| 1102 worker-2 | 19.8G | 45.0G |
| 1103 worker-3 | 19.0G | 22.9G |

NVMe thin pool（3ホストとも同一）：`pve/data` 337.86 GiB、Data 17.76%、Meta 0.94%、
VG 空き 16.00g。thin pool の実使用は control OS の 60G のみ（`vm-100X-disk-0` は Data% 100）。
**データ領域の空きは 277.86 GiB** ＝ 指示書の概算278GiBと一致。

ZFS 共通設定：`sync=standard` / `compression=lz4` / `recordsize=128K` /
`logbias=latency` / `ashift=12` / **`autotrim=off`** / 全プール `ONLINE`・エラー0。

## 2. 実測で分かったこと（指示書にない中核の事実）

`/proc/diskstats` の差分から算出。`iostat` は3ホストとも未インストール。

### 2-1. 18:57頃（10秒サンプル）

| ホスト | SATA write await | busy | NVMe write await | busy |
|---|---:|---:|---:|---:|
| 01 | 123 ms | 78% | 1 ms | 9% |
| 02 | **248 ms** | 80% | 0 ms | 5% |
| 03 | 0 ms | 0% | 0 ms | 1% |

### 2-2. 19:05頃（30秒サンプル）

| ホスト | writes | 実書き込み | write await | busy | 平均I/Oサイズ |
|---|---:|---:|---:|---:|---:|
| 01 | 628 | 17.6 MB | **3 ms** | 4% | 28 KB |
| 02 | 755 | 89.5 MB | **319 ms** | **88%** | 118 KB |
| 03 | 649 | 23.9 MB | 0 ms | 0% | 36 KB |

**読み取り：**

1. 病状はホストに固定されていない。**Longhornの再構築先に追随して移動する。**
   18:57時点でホスト01が123msだったのは再構築先がworker-1だったため。
   19:05にはホスト01は3msへ回復し、再構築先になったホスト02が319msへ悪化した。
   → 指示書と既存調査の「worker-1固有の問題」という枠組みは成立しない。

2. それでも **絶対値が異常**。ホスト02は **25 write IOPS / 3 MB/s で busy 88%、
   1 I/Oあたりの実効サービス時間 約35 ms**。健全なSATA SSDなら1ms未満の領域。
   同一ワークロード帯でホスト03（P3-1TB）は 0ms / 0%。
   **SUNEAST SE800 Lite 2台が、P3-1TB に対して概ね50〜100倍遅い。**

3. SMARTに故障指標なし（両SUNEAST：代替セクタ0、Pending 0、CRC 0、
   Reallocated_Event 0）。ただし通電時間は **29,537h / 27,198h（約3.4年・3.1年）**、
   温度48℃。P3-1TBは 5,604h・30℃。
   SMARTは正常でも遅延の健全性は保証しないため、故障とは断定しない。

4. TRIM：両SUNEASTとも `TRIM Command: Available, deterministic, zeroed`、
   `discard_max_bytes=2147450880`。**ZFS側は3ホストとも `autotrim=off` かつ
   `zpool status -t` が `(untrimmed)`** ＝ プール作成以来一度もTRIMされていない。
   プール使用率は5〜7%だが、SSDのFTLはその空きを認識していない。
   これは根本原因の断定ではなく、**未検証の有力仮説**として記録する。

### 2-3. 症状は進行中

計測中に縮退ボリュームが **3個 → 5個** に増加した。

| 時刻 | 縮退ボリューム |
|---|---|
| 18:56 | prometheus, grafana, ai-business-control-plane-data |
| 19:08 | ＋ gatus, harbor-registry(50GiB) |

同じ時間帯に harbor-database と tempo の再構築は**成功**している
（再構築先がworker-2/3で、かつ小さい）。

Prometheusボリューム `pvc-364e7ae1`（17GB相当）の再構築進捗：
18:55に4% → 19:08に12%。約13分で8%。この速度では完走に約2.5時間かかり、
過去の試行（17:44 / 17:59 / 18:04）はいずれも完走前に失敗している。

`longhorn-manager` のイベントに記録された失敗理由（実物）：

- `rpc error: code = Unavailable desc = error reading from server: EOF`
- `rpc error: code = Unknown desc = replica is already rebuilding`

## 3. 指示書にない、追加で発見した事項

### 3-1. VM 1200 `ai-gateway-01` とZFSレプリケーションの失敗

指示書の構成表に存在しないVMが稼働している。

- `sv-proxmox-01` 上で稼働、`scsi0 = local-zfs:vm-1200-disk-0` 64G（実使用 2.63G）
- HA管理下（`lrm sv-proxmox-01 (active)`、他2ノードは idle）
- **ZFSレプリケーションジョブ2本がホスト01から発火している**

```
JobID   Target            LastSync             Duration  FailCount  State
1200-0  sv-proxmox-02     2026-09-13_18:45:01  146.4s    0          SYNCING
1200-1  sv-proxmox-03     2026-09-13_18:20:49  146.2s    3          FAILED
```

`1200-1` の失敗内容：
`zfs snapshot local-zfs/vm-1200-disk-0@__replicate_1200-1_1789293001__ failed: got timeout`

**ZFSのスナップショット作成がタイムアウトする**のは、プールが極端に詰まっている
ことの独立した裏付けである。同時に、このジョブは15分間隔で
飽和済みのホスト01プールへ snapshot + send の負荷を追加し続けている。

ステップ4（worker OSの移動・再起動）でホスト01を触る際、
HA管理下のVM 1200の扱いを決める必要がある。指示書はこれを考慮していない。

### 3-2. PBSの空き容量が指示書の記載より悪化

```
/dev/mapper/pbs-root  444G  406G  16G  97%  /
```

指示書は「以前の観測では使用率約91%」としているが、**実測は97%・残16GiB**。
datastoreは `gateway-backup` の1つのみで、コメントは
「Gateway VM and homely recovery backups」。
**k8sノードVM（1001-1003 / 1101-1103）のバックアップ先としては容量が足りない。**
指示書ステップ0-4「PBSの空き容量も再確認する」は**不合格**。

### 3-3. ArgoCDが全アプリで selfHeal 有効

39アプリすべてが `syncPolicy.automated.selfHeal: true`。
Longhorn / CNPG / StorageClass へのライブ変更は自動で巻き戻される。
ステップ0-3のとおり、変更前に同期停止の対象と復帰手順を決める必要がある。

既存の OutOfSync（**今回の作業とは無関係な進行中作業に由来**、触っていない）：

- `monitoring`：ServiceMonitor `ai-gateway` / `pbs-observer` が OutOfSync。
  Health は Degraded（grafana・prometheusボリュームの縮退が原因）
- `moshitoku`：Deployment 3件・ConfigMap 2件・HTTPRoute・Cluster が OutOfSync

### 3-4. 作業ツリーに他作業の変更がある

```
 M kubernetes/infra/harbor/README.md
 M scripts/bootstrap-cluster-secrets.sh
?? docs/moshitoku-monitoring-handoff.md
?? scripts/reconcile-moshitoku-harbor.py
```

これらには一切触れていない。本記録は新規ファイルとしてのみ追加した。

## 4. 指示書の記載の検証結果

### 4-1. 正しかった記載

- **`talos/patches/worker.yaml.tftpl` の UserVolumeConfig**：
  コメントは「serialで判定」と書いているが、実際の条件は `match: '!system_disk'`。
  指示書の指摘は正確。**さらに `grow: true` が設定されており**、3本目を追加すると
  「system_disk以外」が2本になって一意に決まらないだけでなく、
  掴んだディスクを残り全体まで拡張しようとする。指示書の警告より危険度は高い。
  `minSize: 50GB` は64GiBの新ディスクも通過してしまうためガードにならない。

- **CNPGの同期複製**：ライブ設定は `minSyncReplicas: 0`、`maxSyncReplicas: 0`、
  `spec.postgresql.synchronous` 未指定。SQLでも確認：

  ```
  synchronous_standby_names=(空)
  synchronous_commit=on
  fsync=on
  full_page_writes=on
  wal_level=logical
  ```

  `synchronous_standby_names` が空 ＝ **非同期複製**。
  `synchronous_commit=on` はローカルWALフラッシュのみを意味する。
  指示書の「同期複製を保証していると扱わない」は正しい。
  `fsync` と `full_page_writes` は両方 `on`（弱められていない）。

- **Longhorn設定**：`concurrent-replica-rebuild-per-node-limit = 1`（既に1）、
  `replica-rebuild-concurrent-sync-limit = {"v1":"1"}`、
  `replica-rebuilding-bandwidth-limit = {"v2":"0"}` ＝ **v1キーが存在しない**。
  「v1エンジンにv2専用の帯域制限を設定して効果があると扱わない」は正しい。
  その他：`replica-soft-anti-affinity=false`、`default-data-locality=disabled`、
  `storage-over-provisioning-percentage=150`、
  `storage-minimal-available-percentage=20`、
  `storage-reserved-percentage-for-default-disk=30`（予約 89GiB/ノード）。

- **DBボリューム**：6個すべて healthy、レプリカ数1、`longhorn-cnpg-retain`、10GiB。
  実データの筐体分散も既に達成済み：

  | クラスタ | worker-1 | worker-2 | worker-3 |
  |---|---|---|---|
  | umami-postgres | instance 3 | instance 2 | instance 1（主系） |
  | moshitoku-postgres | instance 2 | instance 3 | instance 1（主系） |

  両クラスタとも 3/3 Ready、主系は **どちらも worker-3**。

- **Prometheusの正常コピー**：`pvc-364e7ae1-...-r-b78ec33b` は
  **worker-3に存在し、現在も唯一のRWレプリカ**（`healthyAt: 2026-09-06T15:07:43Z`）。
  指示書の記載どおりで、再確認の結果も一致した。**削除・退避・ホスト03再起動の対象外。**

- **NVMe容量の概算**：277.86 GiB 空き。60+64=124 GiB確保後の残 153.86 GiB。
  指示書の154GiBと一致。

### 4-2. 古くなっていた／誤っていた記載

| 指示書の記載 | 実測 |
|---|---|
| 縮退：Grafana, Harbor DB, Tempo, AI control-plane | Harbor DBとTempoは**復旧済み**。代わりに **gatus と harbor-registry が新たに縮退** |
| 再構築先はworker-1、RWレプリカ1個 | 再構築先は**worker-2**に移動済み。RWは依然worker-3の1個 |
| PBS 使用率約91% | **97%・残16GiB** |
| （既存調査）node2 SATA 1.3ms | 再構築先になった状態で **248〜319ms** |
| 「worker-1の直接の負荷源」＝ worker-1固有の問題という枠組み | 病状は**再構築先に追随して移動**する。ホスト固有ではなくSSDモデル依存 |
| 構成は control 3 ＋ worker 3 | **VM 1200 `ai-gateway-01` が別途稼働**（§3-1） |

## 5. ステップ1が指示書のままでは完了しない理由

指示書ステップ1の完了条件は「指定レプリカ数まで戻す」「再構築の失敗ループが
継続していない」。以下の理由で、指示書の計画のままでは到達できない。

1. `replica-soft-anti-affinity = false` かつ storage node が3台のため、
   レプリカ数3のボリュームの**3個目は必ず残り1ノードに置かれる**。
   worker-1/worker-2 のどちらかを避けることはできない。

2. その worker-1 と worker-2 のSATAは、再構築の書き込みパターン下で
   **35ms/IO** まで劣化する。Longhornのファイル同期RPCがタイムアウトして
   `Unavailable / EOF` で落ち、Longhornが再試行し、また負荷をかける。
   自己増幅ループであり、再試行を待っても収束しない
   （17:44 / 17:59 / 18:04 / 19:05 と4回以上失敗している）。

3. 指示書の目標構成は **「大容量の監視・ログデータは当面SATAに残す」** と
   明記している。つまりNVMe移行を完了しても、Prometheus・Grafana・Loki・
   Harbor・gatus のレプリカはSATA上に残り、**このループは解消しない**。

4. 指示書は回避策（レプリカ数の一括削減、再構築数の増加、空き容量条件の緩和）を
   禁じている。この禁止自体は妥当である。

5. 指示書ステップ3-1は「正常コピーを確保できない場合、DBの移行を強行しない」と
   ゲートを設けている。よって**ステップ3以降も現状では開始できない**。

## 6. 未実施・未測定の項目

- Web（moshitoku）の応答時間・エラー率：**未測定**。
  Grafana/Prometheus自体が縮退中で、計測基盤の信頼性が確保できていないため。
- fio / pgbench：指示書の指示どおり**実施しない**。
- バックアップからの隔離環境への復元検証：**未実施**（PBS容量97%、
  CNPGバックアップ先はクラスタ内MinIO `data-minio-0`＝worker-2 のみ）。
- CNPGバックアップの最新：umami `2026-09-12 18:30 UTC`、
  moshitoku `2026-09-12 19:00 UTC`（いずれも completed、約15時間前）。
  次回スケジュールは各 `18:30 / 19:00 UTC`。
  **completedの記録は復元可能性を保証しない**（指示書の注意どおり、未検証）。
- Talos が各ディスクに対して返す serial / デバイス属性：**未取得**。
  ステップ2の前提だが、ステップ1が未達のため未着手。
- `tofu plan`：**未実行**。ステップ2に入る判断が出ていないため。

## 7. 進行・停止の判断基準（ステップ0-6として記録）

**新しい移行・再構築を開始しない条件（いずれか1つで停止）：**

- ノードが NotReady
- CNPGクラスタが 3/3 Ready を割る、または複製遅延が増大し続ける
- Prometheusの唯一のRWレプリカ `r-b78ec33b`(worker-3) が失われる
- 縮退ボリュームが現在の5個から増える
- SATA write await が新たなホストで100msを超える
- etcd の遅延警告
- NVMe thin pool の Data% が 70% を超える

**書き込み停止・再開の方法（DB切り替え時）：** 未確定。
書き込み元（moshitoku-web / moshitoku-analytics / moshitoku-collection-exporter /
moshitoku-scraper / umami）の特定までは完了。停止手順はステップ3着手時に確定する。

## 8. 変更したファイル

本記録（`docs/storage-migration-stage0-2026-09-13.md`）の新規作成のみ。
設定変更・コード変更・コミットは行っていない。

---

# 追記：ステップ1 着手ぶんの実施記録（19:10–19:15 JST）

利用者の判断：(1) `zpool trim` を先に検証する、(2) 再構築ループを一時停止する。

## 9. 実施した変更（2件）

### 9-1. ArgoCD `longhorn` アプリの自動同期を一時停止

```
kubectl -n argocd patch application longhorn --type=merge \
  -p '{"spec":{"syncPolicy":{"automated":null}}}'
```

**変更前の値（復帰用、`_out/storage-migration-20260913/argocd-longhorn-syncPolicy-before.json`）：**

```json
{"automated":{"prune":false,"selfHeal":true},
 "syncOptions":["ServerSideApply=true","CreateNamespace=false"]}
```

**復帰手順：**

```
kubectl -n argocd patch application longhorn --type=merge \
  -p '{"spec":{"syncPolicy":{"automated":{"prune":false,"selfHeal":true}}}}'
```

停止したのは `longhorn` アプリのみ。他38アプリの selfHeal は有効のまま。

### 9-2. Longhorn の再構築を一時停止

```
kubectl -n longhorn-system patch settings.longhorn.io \
  concurrent-replica-rebuild-per-node-limit --type=merge -p '{"value":"0"}'
```

- 変更前：`1`（`kubernetes/infra/longhorn/values.yaml:61`
  `concurrentReplicaRebuildPerNodeLimit: 1` がGit管理の正）
- 変更後：`0`（applied=true 確認済み）
- **データ削除は伴わない。** 新規の再構築開始を止めるだけで、
  既存レプリカ・スナップショット・PVは一切触っていない。
- Gitのvalues.yamlは**変更していない**。9-1の同期停止を戻せば `1` に復帰する。

**復帰手順：** 9-1を戻す（ArgoCDが `1` を再適用する）。
急ぐ場合は上記patchで `"1"` を直接指定してから9-1を戻す。

## 10. 実施できなかった変更（権限で拒否）

以下2件はProxmoxホストへの変更のため、実行が権限で拒否された。**回避はしていない。**

### 10-1. VM 1200 レプリケーションジョブの一時停止

`/etc/pve/replication.cfg` の実測値（復帰用に記録）：

```
local: 1200-0
	comment AI Gateway HA replica
	target sv-proxmox-02
	rate 100
	schedule */10
	source sv-proxmox-01

local: 1200-1
	comment AI Gateway HA replica
	target sv-proxmox-03
	rate 100
	schedule 5,15,25,35,45,55
	source sv-proxmox-01
```

2本を合わせると **5分ごとに1本発火**する。実行時間は正常時146秒だが、
19:07時点で `1200-0` は **864秒間 SYNCING のまま**で、次の発火と重複していた。
`1200-1` は `zfs snapshot ... got timeout` で失敗継続（FailCount 3）。
ホスト01のプールに対する事実上の常時負荷になっている。

実行したかったコマンド（ホスト01上）：

```
pvesr disable 1200-0
pvesr disable 1200-1
```

`pvesr disable` は実行中のジョブを中断しない（次回発火を止めるだけ）。
指示書1-4「稼働中のジョブは原則完了を待つ」に沿う。
復帰は `pvesr enable 1200-0 && pvesr enable 1200-1`。

### 10-2. `zpool trim` の検証

実行したかったコマンド（まずホスト01のみ）：

```
zpool trim -r 200M local-zfs     # 開始（レート制限付き）
zpool status -t local-zfs        # 進捗
zpool trim -c local-zfs          # 中止
```

根拠：3ホストとも `(untrimmed)`＝プール作成以来TRIM未実施。
SUNEASTは `TRIM Command: Available, deterministic, zeroed`。
プール使用率5〜7%に対し、SSDのFTLは空き領域を認識していない。
`zpool trim` はオンライン・非破壊（空きブロックのみdiscard）・
中止可能・レート制限可能。

## 11. 実施中に判明した状態悪化

19:09–19:12 JST（10:09–10:12 UTC）に、**再構築とは別に3つのレプリカが失敗**した。

| 失敗時刻(UTC) | ノード | ボリューム |
|---|---|---|
| 10:09:56 | k8s-worker-2 | `pvc-6984b4ab` = **data-minio-0（100GiB）** |
| 10:11:19 | k8s-worker-2 | `pvc-412905b5` = storage-loki-0 |
| 10:12:47 | k8s-worker-1 | `pvc-364e7ae1` = Prometheus |

加えて Prometheus の再構築先レプリカ `r-31dd7bef`(worker-2) は
20%まで進んだ後に失敗し、削除された。

**縮退ボリュームは 3個(18:56) → 5個(19:08) → 7個(19:14) と増え続けている。**

これらの失敗時刻は再構築停止の適用時刻とほぼ同時であり、
**停止の効果があったかどうかはまだ判定できない**（要継続観測）。

### 11-1. 現時点で最大のリスク：worker-3への一極集中

意図せず**コピーが1個だけ**になっているボリューム（DBの1レプリカ設計を除く）：

| ボリューム | PVC | 残る唯一のコピー |
|---|---|---|
| `pvc-364e7ae1` | prometheus | **k8s-worker-3** |
| `pvc-412905b5` | storage-loki-0 | **k8s-worker-3** |
| `pvc-6984b4ab` | **data-minio-0** | **k8s-worker-3** |

同時に worker-3（ホスト03）は以下も抱えている：

- **CNPG両クラスタの主系**（umami-postgres-1、moshitoku-postgres-1）
- 唯一まともに動作しているSATA SSD（P3-1TB）

そして **MinIO は CNPG のバックアップ保存先**である
（barman-cloudプラグイン、`umami-minio` / `data-minio-0`）。

→ **ホスト03を1台失うと、DB主系とDBバックアップを同時に失う。**
DB自体はworker-1/2の待機系が残るためCNPGがフェイルオーバーできるが、
バックアップは失われる。

**したがって、ホスト03には当面いかなる変更・再起動・移行も行わない。**
指示書ステップ1-1の「唯一の正常コピーを対象から外す」を、
Prometheusだけでなく Loki・MinIO にも拡大して適用する。

## 12. 次にやるべきこと（優先順）

指示書の順序より、この3件が先行する。

1. **§10-1 と §10-2 の承認**（ホスト01のレプリケーション停止 → trim検証）。
   trimでSUNEASTのサービス時間が改善すれば、worker-1/2を再びレプリカ配置先に
   使えるようになり、指示書ステップ1〜4がそのまま成立する。
2. **MinIO の2個目のコピー確保**。ただし配置先はworker-1かworker-2しかなく、
   両方とも現状では書き込みが完走しない。§10-2の結果待ち。
3. **CNPGバックアップのクラスタ外への退避**。現状の唯一のバックアップが
   worker-3上のMinIO 1コピーに乗っている。PBSは97%で受け皿にならない。

## 13. 現在の未復帰の一時変更（作業終了時に必ず戻すもの）

| # | 対象 | 現在 | 戻す先 |
|---|---|---|---|
| 1 | ArgoCD `longhorn` app `spec.syncPolicy.automated` | 削除済み | `{"prune":false,"selfHeal":true}` |
| 2 | Longhorn `concurrent-replica-rebuild-per-node-limit` | `0` | `1`（#1を戻せば自動復帰） |

Proxmox側は何も変更していない。

---

# 追記2：承認後の実施記録（19:15–19:33 JST）

利用者から §10-1 / §10-2 の承認を得て実施。

## 14. VM 1200 レプリケーション停止（実施済み）

```
ssh root@172.16.10.11 'pvesr disable 1200-0; pvesr disable 1200-1'
```

結果：両ジョブ `Enabled: No`。
`1200-0` は停止直前の実行を **546.6秒**（正常時146秒の3.7倍）かけて完走。
`1200-1` は実行中だったぶんが `timeout` で終了。以後の発火はなし。

**復帰：** `ssh root@172.16.10.11 'pvesr enable 1200-0; pvesr enable 1200-1'`

## 15. `zpool trim` 検証（ホスト01、進行中）

```
ssh root@172.16.10.11 'zpool trim -r 200M local-zfs'
```

開始 19:22:35。進捗 4%（19:33、10.5分経過）＝ **完了見込み約4.4時間**。
880GiBに対し実効 **約56 MB/s**。200MB/sのレート制限には当たっておらず、
**ドライブ側がdiscardを56MB/sしか処理できていない**（弱いコントローラの傍証）。

中止：`zpool trim -c local-zfs`

### 15-1. ホスト01の前後比較

| 指標 | 停止前(19:07) | レプリケーション停止後(19:20) | TRIM中(19:30) |
|---|---:|---:|---:|
| write await | 190 ms | 263 ms | **148 ms** |
| read await | 91 ms | 688 ms | **109 ms** |
| スループット | 3.6 MB/s | 2.5 MB/s | **4.5 MB/s** |
| busy | 90% | 91% | 91% |

**TRIM自身が追加負荷であるにもかかわらず、レイテンシが下がりながら
スループットが上がっている。** 負荷変動では説明しにくく、TRIM仮説を支持する。
ただしTRIMは4%しか進んでおらず、**これは中間結果である。断定しない。**

txg sync は依然として長い（403MB/59.2s → 634MB/154.7s）。

## 16. 再構築停止の効果（判定できた）

再構築を止めた結果、**縮退ボリュームが 7個 → 4個 に減少**した。

自力で healthy に戻ったもの（再構築なし）：

- `pvc-f7bd831f` kube-prometheus-stack-grafana
- `pvc-de2c5a0c` gatus
- `pvc-70b4cc55` harbor-registry（50GiB）

これらは `failedAt` が付いていない停止中レプリカを持っており、
再構築の連打が止まったことで engine に再接続できた。
**再構築ループ自体が回復を妨げていた**ことの直接の裏付けである。

**残る縮退4件：**

| ボリューム | PVC | 状態 |
|---|---|---|
| `pvc-14410a1d` | ai-business-control-plane-data | 3個中2個RW |
| `pvc-364e7ae1` | prometheus | **コピー1個（worker-3）** |
| `pvc-412905b5` | storage-loki-0 | **コピー1個（worker-3）** |
| `pvc-6984b4ab` | **data-minio-0（100GiB）** | **コピー1個（worker-3）** |

後者3件は `failedAt` 付きでレプリカを失っており、実際の再構築が必要。
§11-1 の worker-3 一極集中リスクは**未解消**。

## 17. ホスト02・03のベースライン（再構築停止下、19:31）

| ホスト | write await | busy | スループット | txg stime |
|---|---:|---:|---:|---:|
| 02（SUNEAST、**未TRIM＝対照群**） | **10 ms** | 9% | 0.43 MB/s | 0.4–0.5 s |
| 03（P3-1TB） | 0 ms | 0% | 0.73 MB/s | 0.0 s |

**重要：** ホスト02は再構築先でなくなると 319ms → 10ms へ完全に回復する。
つまりSUNEASTは低負荷では正常で、**Longhorn再構築の書き込みパターン
（3〜11 MB/s、平均118KB I/O）で崩れる**。§2の「常時50〜100倍遅い」という
表現は正確でなく、**「再構築負荷を吸収できない」**が正しい。

ホスト02は意図的にTRIMせず**対照群として残す**。
TRIM完了後に再構築を再開し、worker-1（TRIM済み）と worker-2（未TRIM）の
どちらが再構築を完走できるかを比較すれば、仮説をA/Bで検証できる。

## 18. 次の判定手順（TRIM完了後）

1. ホスト01のアイドル時レイテンシを測定し、ホスト02（10ms）と比較する。
2. 再構築を1に戻す（§13の復帰手順）。
3. **MinIO を最優先**で再構築させる（CNPGバックアップ先のため）。
   次に Loki、最後に Prometheus（最大かつ最も失敗しやすい）。
4. worker-1への再構築が完走すれば仮説は成立。
   その場合ホスト02にも同じTRIMを適用する。
5. 完走しなければ、TRIMでは不足と確定する。その時点で
   §12の「Longhornデータ自体をNVMeへ移す」案を再検討する。

**ホスト03には引き続き一切触れない。**

---

# 追記3：TRIM検証の結果（20:48–22:40 JST）

## 19. TRIM完了

ホスト01：19:22:35 開始 → **20:48:34 完了（所要86分）**。

進捗の推移が示唆的で、**進むほど加速した**：

```
19:33  4%     19:43 13%     19:53 26%     20:03 39%
20:13 52%     20:23 66%     20:33 79%     20:48 100%
```

序盤 4%/10.5分（≒56MB/s）→ 後半 13%/10分（≒190MB/s）。
空きブロックが返るほどdiscard自体も速く処理できるようになっており、
「FTLの空き枯渇」という仮説と整合する。

フラグメンテーション 15% → 12%。プール容量・データは不変（ALLOC 70.5G）。

## 20. アイドル時の比較（22:12、再構築停止下・同一スループット）

| ホスト | 状態 | read await | write await | 書き込み量 | busy | loadavg |
|---|---|---:|---:|---:|---:|---:|
| 01 | SUNEAST・**TRIM済み** | **0 ms** | **0 ms** | 23 MB/min | **0%** | 1.81 |
| 02 | SUNEAST・**未TRIM（対照群）** | 48 ms | 24 ms | 22 MB/min | 19% | 0.43 |
| 03 | P3-1TB | 0 ms | 0 ms | 47 MB/min | 0% | 1.12 |

同型番・同負荷量で、TRIMの有無だけが違う。
**ホスト01はTRIM前 190〜263ms / busy 90% / loadavg 21.97 だった。**
TRIM後は健全なP3-1TBと同等になった。

## 21. 本番負荷でのA/B検証（22:16 再構築再開、22:40測定）

`concurrent-replica-rebuild-per-node-limit` を `0` → `1` に戻して再構築を再開。

### 21-1. 再構築の速度

| ボリューム | 再構築先 | 結果 |
|---|---|---|
| `pvc-364e7ae1` prometheus | **worker-1（TRIM済み）** | **3分で10%→48%（約32MB/s）で完走** |
| `pvc-412905b5` storage-loki-0 | worker-1 | 7分で0%→82% |
| `pvc-6984b4ab` data-minio-0 (100GiB) | worker-2（未TRIM） | 進行中、約4%/分 |
| `pvc-f7bd831f` grafana / `pvc-de2c5a0c` gatus | — | 再構築開始 |

**TRIM前の同じPrometheusボリューム：25分かけて4%→20%、その後失敗。
TRIM後：3分で10%→48%。約15倍。**

### 21-2. 再構築中のホスト負荷（22:40、MinIO再構築がworker-2へ流れている状態）

| ホスト | 役割 | read await | write await | 書き込み量 | busy | txg sync | loadavg |
|---|---|---:|---:|---:|---:|---:|---:|
| 01 **TRIM済み** | 再構築完走済み | **0 ms** | **0 ms** | 26 MB/min | **0%** | 1MB/0.0s | 3.52 |
| 02 **未TRIM** | MinIO再構築を受信中 | 230 ms | 112 ms | 280 MB/min | **80%** | **483MB/153.8s** | 18.59 |
| 03 P3-1TB | 再構築の読み出し元 | 2 ms | 11 ms | 37 MB/min | 14% | 2MB/0.9s | 1.81 |

TRIM済みホストは約32MB/sの再構築を **busy 0%** で処理した。
未TRIMホストは 4.7MB/s で **busy 80%・txg sync 154秒**に沈んでいる。
**同型番ドライブでの対照実験として、TRIM未実施が原因と結論できる。**

### 21-3. 失敗レプリカ

再開後、**`failedAt` の付いたレプリカは 0 件**。
TRIM前は数分おきに失敗していた（19:09/19:11/19:12 など）。

## 22. 結論の訂正

指示書および既存調査（`worker-1-disk-investigation-2026-09-13.md`）の
「OSとLonghornが同じSATA SSD/ZFSプールを共有していることが原因」という
枠組みは、**主因ではなかった**。

実際の主因は **ZFSプールが作成以来一度もTRIMされておらず
（`autotrim=off` かつ `zpool status -t` が `(untrimmed)`）、
SUNEAST SE800 Lite のFTLが空きブロックを認識できずGCで飽和していたこと**。

同じ共有構成のまま、TRIMだけで解消した。
ディスク共有は症状を波及させる増幅要因ではあったが、原因ではない。

これにより、指示書ステップ1（再構築ループと複製不足の解消）は
**NVMe移行を待たずに達成できる見込みとなった。**
§5で「指示書のままではステップ1が完了しない」とした判断は、
TRIMという指示書外の手段によって解消された。

## 23. 残作業

1. MinIO再構築の完走を待つ（100GiB、進行中）。
2. **ホスト02にも同じTRIMを適用する**（対照群としての役割は終了）。
   実施前に再構築の完走を待つ。
3. ホスト03（P3-1TB）も `(untrimmed)` である。現時点で症状はないが、
   同じ予防措置の対象。ただし**worker-3は唯一のコピーを複数抱えているため、
   すべての複製が回復してから最後に実施する。**
4. 恒久対策として `autotrim=on` またはTRIMの定期実行を検討する
   （宣言設定への反映方法を含め、別途提案する）。
5. §13の一時変更（ArgoCD同期停止）とVM 1200レプリケーションを復帰する。
6. 指示書ステップ2以降（NVMe移行）は、DBとCIランナーの性能改善という
   本来の目的に対しては依然として有効。ステップ1が解消した状態で再評価する。

---

# 追記4：根本原因の特定（22:45 JST）

## 24. なぜ一度もTRIMされなかったのか

`/etc/cron.d/zfsutils-linux` には**毎月第1日曜のTRIMが最初から設定されている**。

```
# TRIM the first Sunday of every month.
24 0 1-7 * * root if [ $(date +\%w) -eq 0 ] && [ -x /usr/lib/zfs-linux/trim ]; then /usr/lib/zfs-linux/trim; fi
```

にもかかわらず `zpool status -t` は `(untrimmed)` だった。
`/usr/lib/zfs-linux/trim` の実装を読むと理由が分かる。

```sh
PROPERTY_NAME="org.debian:periodic-trim"
...
case "${ret}" in
    disable);;
    enable) trim_if_not_already_trimming "${pool}" ;;
    -|auto) if pool_is_nvme_only "${pool}"; then trim_if_not_already_trimming "${pool}"; fi ;;
esac
```

**既定値 `auto` では、NVMe のみで構成されたプールしかTRIMしない。**
`pool_is_nvme_only()` は各vdevの `lsblk -dnr -o TRAN` が `nvme` であることを要求する。

`local-zfs` は **SATA**（`/dev/sda`）1本のプールなので、この分岐で常にスキップされる。
つまり **cronは毎月起動しているが、SATAプールには何もしない。**

3ホストとも同一：

| ホスト | `org.debian:periodic-trim` | `autotrim` |
|---|---|---|
| 01 | 未設定（既定 `auto`） | `off` |
| 02 | 未設定（既定 `auto`） | `off` |
| 03 | 未設定（既定 `auto`） | `off` |

**NVMe側は影響を受けない。** NVMeはZFSではなくLVM-thin（`pve/data`）で、
`fstrim.timer` が enabled、かつVMディスクに `discard=on` が設定されているため
ゲストからのdiscardが通る。手当てが漏れていたのは **ZFS/SATA側だけ** である。

## 25. 劣化の速さ

`zpool get creation local-zfs` → **2026-09-06 03:01**。

つまりプールは**作成から7日**で、Longhornの再構築が完走できない状態まで劣化した。
ドライブ自体は通電29,537時間（約3.4年）で、ADR-0009でCephを廃止した際に
再利用されている。ZFSプールを作り直してもSSDのFTLには何も通知されないため、
**Ceph時代に書かれた全領域が「使用中」のまま残っていた**と考えると、
使用率7%のプールでFTLが枯渇していた説明がつく。

ホスト03のP3-1TBは通電5,604時間と新しく、蓄積が少なかったため
同じ設定でも症状が出ていなかったと考えられる（未検証の推定）。

## 26. 恒久対策（提案）

**月1回では不足である。** 7日で壊滅したのだから、月次TRIMでは間に合わない。

```sh
# 1. 継続的TRIM（主対策）— ブロック解放時に随時discardを発行する
zpool set autotrim=on local-zfs

# 2. 定期的な全面TRIM（保険）— 既存のcronを実際に機能させる
zfs set org.debian:periodic-trim=enable local-zfs
```

2つ目は**新しいタイマーもスクリプトも追加しない**。
すでにインストール済みで毎月起動しているcronを、SATAプールに対しても
有効化するだけである。

戻す場合：`zpool set autotrim=off local-zfs` /
`zfs inherit org.debian:periodic-trim local-zfs`

適用対象は3ホストすべて。ホスト03は現時点で症状がないが、
同じ設定漏れがあり、時間の問題である。

### 26-1. 宣言設定への反映について

このリポジトリのOpenTofu（`tofu/10-proxmox-talos`）はVMを管理しており、
**Proxmoxホスト自身のZFS設定は管理対象外**である。
`scripts/` にもホストのストレージ設定を収束させるものはない。

したがって現時点では手動適用＋本記録での明文化にとどめる。
`scripts/reconcile-*.sh` の流儀に合わせた収束スクリプト化は、
別作業として提案する（今回の指示書の範囲外）。

---

# 追記5：恒久対策の適用と復旧（22:45– JST）

## 27. 恒久対策を3ホストに適用（実施済み）

```sh
zpool set autotrim=on local-zfs
zfs set org.debian:periodic-trim=enable local-zfs
```

適用結果（3ホストとも同一）：

```
autotrim                   on
org.debian:periodic-trim   enable   local
```

- `autotrim=on` が主対策。ブロック解放時に随時discardを発行する。
  月1回では7日で壊滅した実績があるため、継続的TRIMが必須。
- `periodic-trim=enable` は保険。**新しいタイマーもスクリプトも追加していない。**
  既存の `/etc/cron.d/zfsutils-linux`（毎月第1日曜）が、
  既定の `auto`（＝NVMe専用プールのみ）でスキップしていたSATAプールも
  対象に含めるようにするだけ。

**戻す場合：**

```sh
zpool set autotrim=off local-zfs
zfs inherit org.debian:periodic-trim local-zfs
```

**注意：これはProxmoxホスト自身の設定であり、このリポジトリの宣言設定
（OpenTofuはVMのみ管理）には含まれない。** ホストを再インストールすると失われる。
`scripts/reconcile-*.sh` 流の収束スクリプト化は別作業として提案する。

## 28. ホスト02のTRIM

開始 22:40:32。25% @ 22分（23:02）。完了見込み 00:10 頃。
ホスト01（86分）とほぼ同じペース。

## 29. 復旧状況

MinIO（`pvc-6984b4ab`、100GiB、CNPGバックアップ先）は
**22:39に再構築完走し healthy へ復帰**。§11-1で挙げた最大のリスクは解消した。

残る縮退3件（いずれも失敗レプリカ0件、worker-1上の停止中レプリカ待ち）：

- `pvc-14410a1d` ai-business-control-plane-data
- `pvc-364e7ae1` prometheus
- `pvc-412905b5` storage-loki-0（**まだコピー1個**）

23:03に再構築を再開（limit `0` → `1`）。
復旧先はいずれもTRIM済みのworker-1のため、進行中のホスト02のTRIMとは競合しない。

## 30. 方針決定（利用者判断）

- 恒久対策：**3ホストすべてに適用する**（実施済み、§27）
- 指示書本体のNVMe移行（ステップ2〜4）：**一旦停止して再評価する**

理由：ステップ1の障害がTRIMで解消したため、移行の前提が変わった。
復旧完了後の安定した状態でDB・CIランナーの実測値を取り直し、
NVMe移行が本当に必要かを判断する。PBS 97%の問題も未解決のまま。

## 31. 再試行上限に達したレプリカの解消（23:05）

再構築を再開しても3件が動かなかった。原因は Longhorn の
**`rebuildRetryCount` が上限（5）に達し、再試行を諦めていた**こと。

| ボリューム | ノード | state | retry | healthyAt |
|---|---|---|---:|---|
| `pvc-364e7ae1` prometheus | worker-1 | running | **5** | NEVER |
| `pvc-364e7ae1` prometheus | worker-2 | running | **5** | NEVER |
| `pvc-364e7ae1` prometheus | worker-3 | running | 0 | 2026-09-06（唯一の正常コピー） |
| `pvc-412905b5` loki | worker-2 | stopped | **5** | NEVER |
| `pvc-14410a1d` ai-cp | worker-1 | stopped | 1 | NEVER |

上限に達した2件を削除した。削除前に以下を確認している：

- `healthyAt` が空＝**一度も正常になっておらず、固有データを持たない**
- engine の `replicaModeMap` に含まれない＝**孤立している**
- 各ボリュームの正常コピー（prometheus は worker-3 の `r-b78ec33b`、
  loki は worker-3 の `r-a3c2f5b6`）は保持されている

```
kubectl -n longhorn-system delete replica pvc-364e7ae1-...-r-7f6583df   # worker-1
kubectl -n longhorn-system delete replica pvc-412905b5-...-r-0892922c   # worker-2
```

指示書1-1「唯一の正常コピーを削除・退避の対象から外す」を満たしている。
1-5が禁じる「全Volumeのレプリカ数を一括で減らす」には該当しない
（レプリカ数の指定値は変えておらず、空のレプリカを作り直させただけ）。

## 32. 復旧結果

削除直後から再構築が正常に流れた。

```
23:06  prometheus 34%
23:07  prometheus 35%  loki 75%
23:08  loki 完了 → 縮退3→2
23:15  prometheus 91%（worker-1、完了）
23:17  prometheus 10%（worker-2、2本目開始）
23:23  prometheus 91%
```

**`pvc-364e7ae1` prometheus：3レプリカすべて HEALTHY / retry=0 で完全復旧。**
数時間にわたり完走できなかったボリュームが、TRIM後は数分で復旧した。

**`pvc-412905b5` storage-loki-0：復旧。**
**`pvc-6984b4ab` data-minio-0：22:39に復旧済み。**

残る縮退：`pvc-14410a1d` ai-business-control-plane-data のみ
（正常コピー2個を保持、retry=1、上限未達のため通常の再試行で回復見込み）。

なお worker-2 はTRIM実行中にもかかわらず、23:17→23:23 で
prometheus を 10%→91% まで再構築した。**部分的なTRIM（43%時点）でも
既に効果が出ている。**

## 33. 全ボリューム復旧（23:37）

`pvc-14410a1d` も §31 と同じ孤立レプリカ（worker-1、`healthyAt` 空、
engine の `replicaAddressMap` に不在、`currentState: stopped` のまま
Longhorn が一切触っていない）だった。全ノードが
`Schedulable=True` かつ全条件 `True` であることを確認したうえで削除。

```
kubectl -n longhorn-system delete replica pvc-14410a1d-...-r-b73f65fe
```

削除から **約1分で healthy** に復帰した。

### 復旧後の全体確認（23:38）

```
ボリューム: healthy 22 / 22        （degraded 0）
失敗レプリカ: 0
レプリカ配置: worker-1=17  worker-2=16  worker-3=18
ノード: 6台すべて Ready
CNPG: analytics/umami-postgres 3/3、moshitoku/moshitoku-postgres 3/3
      主系は両方 worker-3、Cluster in healthy state
```

**指示書ステップ1「SATA上の再構築ループと複製不足を解消」の
完了条件を満たした。** 対処内容は指示書の想定（Longhornの設定調整・
配置先の選定）ではなく、ZFSプールのTRIMだった。

指示書1-5が禁じた対処（レプリカ数の一括削減、再構築数の増加、
空き容量条件の一括緩和）はいずれも行っていない。
`concurrent-replica-rebuild-per-node-limit` は一時的に0にしたが、
これは**増やす**方向ではなく止める方向であり、`1` に戻してある。

## 34. 復旧後のDB実測（23:40、ステップ0-5の未測定項目を回収）

### 34-1. 複製状態

| クラスタ | standby | state | sync_state | replay_lag | 遅れ |
|---|---|---|---|---|---|
| umami-postgres | umami-postgres-2 | streaming | async | 0 | 0 bytes |
| umami-postgres | umami-postgres-3 | streaming | async | 0 | 0 bytes |
| moshitoku-postgres | moshitoku-postgres-2 | streaming | async | 0 | 0 bytes |
| moshitoku-postgres | moshitoku-postgres-3 | streaming | async | 0 | 0 bytes |

全standbyが `streaming`、遅延0バイト。`sync_state=async` は §4-1 の
非同期複製の確認と一致する。

### 34-2. データベースの実サイズ — 再評価の要点

| クラスタ | DBサイズ | xact_commit | blks_read | PVC |
|---|---:|---:|---:|---:|
| analytics/umami | **7,670 kB** | 204,191 | 446 | 10 GiB |
| moshitoku | **7,670 kB** | 138,807 | 373 | 10 GiB |

**両DBとも約7.5 MB。合計15 MBである。**

指示書の目標構成は、この2つのDBのために
**worker 1台あたり64 GiBの専用NVMeディスク（3台で192 GiB）**を新設する計画。
指示書自身が「64GiBは計画値です。DBの現容量、成長量、…を確認してください」と
但し書きしているとおり、**現容量に対して4000倍以上の過剰**である。

PVCは各10 GiB（6インスタンスで60 GiB）。仮にDB専用NVMeを設けるとしても、
必要量はPVC実配置ぶんであり、64 GiB/台の根拠は現時点では確認できない。

`blks_read` が446 / 373 と極端に少なく、実データは
shared_buffers（128MB）にほぼ収まっている。**現状のDBはディスクI/Oを
ほとんど発生させていない。**

### 34-3. チェックポイント

| クラスタ | timed | requested | write_time | sync_time |
|---|---:|---:|---:|---:|
| umami | 569 | 4 | 28,795 ms | 6,164 ms |
| moshitoku | 422 | 4 | 6,039 ms | 23 ms |

`requested` が4回のみ＝WAL量起因の強制チェックポイントはほぼ発生していない。

### 34-4. 測定できなかったもの

- `track_io_timing` が **off** のため、`blk_read_time` / `blk_write_time` は
  0 のまま取得できない。有効化はDBの設定変更になるため実施していない。
- Web（moshitoku）の応答時間・エラー率は依然**未測定**。

---

# 追記6：ホスト02/03のTRIMと一時変更の復帰（2026-09-14 00:06–00:15 JST）

## 35. ホスト02のTRIM完了

開始 2026-09-13 22:40:32 → **完了 2026-09-14 00:06:17（所要86分）**。
ホスト01と同一の所要時間。

## 36. TRIM後の3ホスト比較（00:10、全ボリューム healthy の安定状態）

| ホスト | ドライブ | TRIM | read await | write await | 書込量 | busy | loadavg | txg sync |
|---|---|---|---:|---:|---:|---:|---:|---:|
| 01 | SUNEAST SE800 Lite | 済 | 1 ms | 1 ms | 32 MB/min | 1% | 2.38 | 1MB/0.0s |
| 02 | SUNEAST SE800 Lite | 済 | 0 ms | 1 ms | 32 MB/min | 1% | 1.07 | 1MB/0.0s |
| 03 | P3-1TB | 未 | 0 ms | 0 ms | 40 MB/min | 0% | 1.02 | 1MB/0.0s |

**3ホストが同等になった。**

### 経過の全体像（ホスト01・SATA write await）

| 時刻 | 状況 | write await | busy | loadavg |
|---|---|---:|---:|---:|
| 19:07 | 再構築ループ・レプリケーション稼働中 | 190 ms | 90% | 21.97 |
| 19:20 | レプリケーション停止後 | 263 ms | 91% | — |
| 19:30 | TRIM中（4%） | 148 ms | 91% | — |
| 22:12 | TRIM完了後・アイドル | 0 ms | 0% | 1.81 |
| 00:10 | 全復旧後・通常運用 | 1 ms | 1% | 2.38 |

ホスト02（対照群）：19:05 に 319ms/88% → TRIM前アイドルで 24ms/19%
→ **TRIM後 1ms/1%**。

## 37. ホスト03のTRIM

00:13:05 開始。症状は出ていないが、同じ設定漏れ（§24）の対象であり、
`autotrim=on` は**今後解放されるブロックにしか効かない**ため、
既存の蓄積を一度解消する必要がある。

全22ボリュームが healthy で3ノードに複製が揃っている状態で実施しており、
仮にホスト03が一時的に遅くなっても他ノードにコピーがある。

## 38. 一時変更の復帰（完了）

| # | 対象 | 復帰後の値 | 確認 |
|---|---|---|---|
| 1 | ArgoCD `longhorn` app `syncPolicy` | `{"automated":{"prune":false,"selfHeal":true},"syncOptions":["ServerSideApply=true","CreateNamespace=false"]}` | `_out/storage-migration-20260913/argocd-longhorn-syncPolicy-before.json` と**完全一致** |
| 2 | `concurrent-replica-rebuild-per-node-limit` | `1`（applied=true） | Git `values.yaml:61` の値と一致 |
| 3 | VM 1200 レプリケーション `1200-0` / `1200-1` | `Enabled: Yes` | `pvesr status` で確認 |

復帰後の状態：

```
argocd longhorn app : Synced / Healthy
Longhorn volumes    : healthy 22 / 22
```

**`values.yaml` は変更していない。** 一時変更はすべてライブ設定に対してのみ行い、
Gitの宣言設定との差分は残していない。

### 38-1. 復帰後に残っている観察点

`1200-1`（sv-proxmox-01 → sv-proxmox-03）は再有効化直後の `pvesr status` に
`zfs snapshot ... got timeout` / FailCount 4 が残っている。
これは停止前の失敗記録である可能性があるため、次回スケジュール実行の
結果を確認する必要がある（§39）。

## 39. VM 1200 レプリケーションの復調 — 独立した裏付け

再有効化後の実行結果（`pvesr status`、00:14–00:28）：

| ジョブ | 経路 | TRIM前 | TRIM後 | FailCount |
|---|---|---|---:|---:|
| `1200-0` | host01 → host02 | 正常時146秒、悪化時 546秒 / 864秒 | **4.2〜4.5 秒** | 0 |
| `1200-1` | host01 → host03 | `zfs snapshot ... got timeout` で失敗継続 | **2.9〜3.4 秒** | 0（前 4） |

§38-1 で残った観察点は解消。停止前のエラーは古い記録だった。

**これはLonghornと無関係な独立したワークロードでの確認である。**
ZFSのスナップショット作成すらタイムアウトしていたジョブが、
TRIM後は3秒前後で完走している。30〜190倍の改善。

Longhornの再構築（§21）とProxmoxのZFSレプリケーション（本節）という
まったく別系統の2つのワークロードが、同じ対処で同時に解消したことは、
原因がZFSプール／SSDのFTL側にあったという結論を強く支持する。

---

# 41. 総括

## 41-1. 変更したファイル

| ファイル | 種別 |
|---|---|
| `docs/storage-migration-2026-09-13.md`（本文書） | 新規作成 |

**コードおよび宣言設定の変更は0件。** `values.yaml`、`*.tf`、`*.tftpl`、
マニフェスト類は一切変更していない。したがってレビュー対象の差分もない。

作業ツリーに元からあった他作業の変更（`kubernetes/infra/harbor/README.md`、
`scripts/bootstrap-cluster-secrets.sh`、`docs/moshitoku-monitoring-handoff.md`、
`scripts/reconcile-moshitoku-harbor.py`、
`docs/worker-1-disk-investigation-2026-09-13.md`）には触れていない。

## 41-2. 実施順

| # | 時刻 | 対象 | 内容 |
|---|---|---|---|
| 1 | 18:50–19:14 | 全体 | ステップ0の読み取り専用調査（§1〜§8） |
| 2 | 19:14 | Kubernetes | ArgoCD `longhorn` app の自動同期を一時停止 |
| 3 | 19:15 | Kubernetes | 再構築を一時停止（limit `1`→`0`） |
| 4 | 19:16 | host01 | VM 1200 レプリケーション 2本を `pvesr disable` |
| 5 | 19:22–20:48 | host01 | `zpool trim -r 200M local-zfs`（86分） |
| 6 | 22:16 | Kubernetes | 再構築を再開（limit `0`→`1`）、A/B検証 |
| 7 | 22:40–00:06 | host02 | `zpool trim -r 200M local-zfs`（86分） |
| 8 | 22:47 | host01/02/03 | `autotrim=on` ＋ `periodic-trim=enable` |
| 9 | 23:05, 23:36 | Kubernetes | 再試行上限に達した空レプリカ3個を削除 |
| 10 | 23:37 | — | 全22ボリューム healthy |
| 11 | 00:13–01:30 | host03 | `zpool trim -r 200M local-zfs` |
| 12 | 00:14 | 全体 | 一時変更をすべて復帰 |

## 41-3. 実際のサービス中断

**なし。**

- ノード6台は全期間 Ready を維持
- CNPG 両クラスタは全期間 3/3 Ready、主系の切り替えなし
- VMの再作成・再起動なし、Talos reset なし、ディスクのフォーマット・
  移動・削除なし
- 削除したのは Longhorn の空レプリカ3個のみ（いずれも `healthyAt` 空で
  固有データを持たず、engine の `replicaAddressMap` に不在）

指示書「中止・切り戻しの原則」で禁じられた行為
（`fsync`・`full_page_writes`・ZFS同期書き込み保証の弱体化、
Talos reset/reinstall、VM再作成、既存ディスクのフォーマット）は
いずれも行っていない。

## 41-4. 切り戻し方法

現在有効な変更は§27の2つのみ（ProxmoxホストのZFS設定）。

```sh
# 3ホストそれぞれで
zpool set autotrim=off local-zfs
zfs inherit org.debian:periodic-trim local-zfs
```

実施済みのTRIM自体は切り戻せないが、TRIMはSSDに空きブロックを
通知するだけの操作であり、データにも構成にも影響しない。

一時変更（§9-1、§9-2、§14）はすべて復帰済み（§38）。

## 41-5. 残作業

### 優先度：高

1. **PBS が 97% 使用・残16GiB**（§3-2）。datastore は `gateway-backup` 1つのみ。
   指示書ステップ4のVMバックアップ前提を満たさない。今回の作業では未対処。
2. **CNPGバックアップの独立性**。保存先はクラスタ内MinIO（`data-minio-0`）のみ。
   物理障害から独立していない。復元検証も未実施（§6）。
3. **ホスト設定の宣言化**。§27 の `autotrim` / `periodic-trim` は
   Proxmoxホスト自身の設定で、このリポジトリの管理対象外。
   ホスト再インストールで失われる。`scripts/reconcile-*.sh` 流の
   収束スクリプト化を別作業として提案する。

### 優先度：中

4. **監視の追加**。以下が可視化されていれば今回の事態は早期に検知できた。
   - `zpool status -t` の trim 状態（`(untrimmed)` の検知）
   - ZFS txg sync 時間（`/proc/spl/kstat/zfs/<pool>/txgs` の stime）
   - ホスト別 SATA/NVMe の write await
   - Longhorn の `rebuildRetryCount` 上限到達
   - `pvesr status` の FailCount
   既存パネルの活用と重複回避は指示書ステップ5のとおり。
5. **ホスト03のTRIM完了確認**（01:30頃見込み）。

### 指示書ステップ2〜4の再評価材料

6. **DBの実サイズは両方あわせて15 MB**（§34-2）。
   64 GiB/台の専用NVMeディスクという計画値の根拠は現時点で確認できない。
   `blks_read` も446/373と極小で、DBはディスクI/Oをほとんど発生させていない。
7. **`talos/patches/worker.yaml.tftpl` の `match: '!system_disk'` ＋ `grow: true`**
   は未修正（§4-1）。3本目のディスクを追加する場合は先に直す必要がある。
   ディスクを追加しない限り現状は正常に動作しているため、
   今回は変更していない。
8. **worker OS の NVMe 移行**は、TRIM後のSATAが 1ms / busy 1% で
   動作している現状では、当初想定した改善幅が得られない可能性がある。
   CIランナーの実測値を取り直してから判断するのが妥当。

## 41-6. 指示書に対する評価

指示書の技術的な注意点は総じて正確だった。特に以下は実測で裏付けられた。

- `!system_disk` の曖昧さ（実際は `grow: true` の分さらに危険）
- CNPGが同期複製ではないこと
- v1エンジンに v2 専用の帯域制限が効かないこと
- 「再試行を連打しない」「一括で緩めない」という禁止事項
- 唯一の正常コピーを保護すること

一方、**根本原因の枠組み（OSとLonghornのディスク共有）は主因ではなかった。**
指示書自身が「SSD故障、autotrim未設定、DB主系の偏りを根本原因と断定しない」と
書いており、`autotrim` に言及はしていたが、それが主因である可能性は
検討されていなかった。

指示書が求めた「実機を再確認し、各段階の前提条件を満たしてから進める」
「未検証のコマンドを推測して実行しない」という進め方が、
この差異の発見につながった。

---

# 追記7：ホスト03のTRIM完了とCI実行時の観測（01:29–01:40 JST）

## 40. ホスト03のTRIM完了

00:13:05 開始 → **01:29:36 完了（所要77分）**。3ホストとも完了。

### 40-1. 3ホストの最終設定

| ホスト | trim | autotrim | periodic-trim | fragmentation |
|---|---|---|---|---:|
| 01 | completed | on | enable | 8%（前 15%） |
| 02 | completed | on | enable | 9%（前 14%） |
| 03 | completed | on | enable | 10%（前 12%） |

### 40-2. VM 1200 レプリケーション（01:30時点）

```
1200-0  Yes  sv-proxmox-02  2026-09-14_01:30:02  3.48s  FailCount 0  OK
1200-1  Yes  sv-proxmox-03  2026-09-14_01:25:02  2.68s  FailCount 0  OK
```

安定して数秒で完走している。

## 42. CI実行時の観測（指示書ステップ5の「代表的なCI実行」）

01:27頃から `arc-runners/moshitoku-builder-d6wvl-runner-*` が3 Pod、
**worker-1（ホスト01）** で起動した。TRIM後としては初のCIビルドである。

### 42-1. CI実行中のホスト負荷（01:35、60秒サンプル）

| ホスト | 状況 | スループット | write await | busy | loadavg | 最大txg sync |
|---|---|---:|---:|---:|---:|---:|
| 01 | **CIビルド3 Pod 実行中** | **29 MB/s** | **7 ms** | 57% | 16.89 | 10.6 s |
| 02 | アイドル | 0 MB/s | 1 ms | 2% | 0.92 | 0.1 s |
| 03 | アイドル | 0 MB/s | 0 ms | 0% | 1.30 | 0.0 s |

### 42-2. 対処前との比較（同じホスト01のSATA）

| | 対処前（19:07、再構築中） | 対処後（01:35、CIビルド中） |
|---|---:|---:|
| スループット | 2.5〜3.6 MB/s | **29 MB/s** |
| write await | 190〜263 ms | **7 ms** |
| busy | 90% | 57% |
| txg sync | 51.8s / 154.7s | 10.6 s |

**スループット約12倍、レイテンシ約30分の1。**
ドライブは働いている（57% busy）が、飽和・崩壊はしていない。

なお 01:30 の30秒サンプルでは 8 MB/s / 69ms / 84% を記録している。
バースト時には依然として待ち時間が伸びる。**「完全に解決した」ではなく
「実用域に戻った」が正確である。**

### 42-3. ステップ2〜4の再評価に対する含意

指示書がworker OSのNVMe移行で狙った「CIランナーのディスク性能改善」について：

- 対処前はCI・OS・Longhornが同じ飽和したプールで待たされていた
- 対処後はCIビルドが 29 MB/s を 7ms で処理できている
- NVMe移行による追加の改善は**あり得るが、程度の問題であって
  機能不全の解消ではなくなった**

DBについては §34-2 のとおり実サイズ15 MBで、ディスクI/Oをほぼ発生させていない。

したがって指示書ステップ2〜4は、**当初の切迫した理由を失っている。**
実施する場合は、CIビルド時間の実測（前後比較）を根拠に改めて判断すべきで、
現時点でその測定は行っていない（対処前のCIビルド時間の記録がないため
厳密な前後比較はできない）。

---

# 追記8：残作業の着手（2026-09-15）

## 43. 対策の持続確認（42時間後）

2026-09-15 19:33 時点。TRIM完了（09-14 01:29）から約42時間。

```
Longhorn: healthy 22 / 22、失敗レプリカ 0
ノード  : 6台すべて Ready
CNPG    : 両クラスタ 3/3、主系 worker-3、healthy
```

| ホスト | autotrim | periodic-trim | frag | 状況 | write await | busy | maxtxg |
|---|---|---|---:|---|---:|---:|---:|
| 01 | on | enable | 12% | CIビルド実行中（24MB/s） | **9 ms** | 54% | 4.2 s |
| 02 | on | enable | 10% | アイドル | 0 ms | 1% | 0.1 s |
| 03 | on | enable | 8% | アイドル | 9 ms | 13% | 0.4 s |

`pvesr` は `1200-0` 3.2秒 / `1200-1` 9.8秒、FailCount 0。
**負荷下でも await は一桁 ms に収まっており、対策は持続している。**

## 44. PBS：バックアップが5夜連続で失敗していた

§3-2 で「PBS 97%」として積み残していた件を調べたところ、
**より深刻な状態だった。**

### 44-1. 事実

```
/dev/mapper/pbs-root  444G  422G  0  100%  /
```

vzdump タスクの結果（`/var/log/pve/tasks/index`）:

| 実行時刻 | 結果 |
|---|---|
| 2026-09-11 02:30 | job errors |
| 2026-09-12 02:30 | job errors |
| 2026-09-13 02:30 | job errors |
| 2026-09-14 02:30 | job errors |
| 2026-09-15 02:30 | job errors |

失敗理由（タスクログ実物）:

```
ERROR: VM 1001 qmp command 'backup' failed - backup connect failed:
       command error: No space left on device (os error 28)
```

**09-13 の報告で「k8sノードVMのバックアップ先としては容量が足りない」と
書いたのは不正確だった。訂正する。**
k8s VM 6台（1001-1003 / 1101-1103）は `kubernetes-daily-pbs` ジョブで
**日次バックアップされていた**。ただし 09-11 以降はすべて失敗している。

### 44-2. なぜ容量が尽きたか — 二重のデッドロック

1. **vzdump 側の保持ポリシーは正しく設定されている**

   ```
   vzdump: kubernetes-daily-pbs
       prune-backups keep-daily=7,keep-weekly=4,keep-monthly=3
       remove 1
   ```

   しかし prune は**バックアップ成功後**に走る。即失敗するため prune が動かない。

2. **PBS 側の prune ジョブには keep-* が1つも無かった**

   ```
   prune: default-gateway-backup-532eb832-
       schedule daily
       store gateway-backup
   ```

   保持条件が無いため、毎日起動しては何も削除せずに終了していた。

結果：**削除されない → 満杯 → バックアップ失敗 → prune が走らない**。

### 44-3. 何が溜まっていたか

各グループ 10〜11 スナップショットのうち **7 個が 2026-09-06 の同日内**
（02:42 / 05:17 / 06:27 / 07:43 / 09:08 / 10:35 / 12:10）。
クラスタ再構築日に約1.5時間おきに取られた作業スナップショットである。
以降は 09-10〜09-13 の日次のみ。実在する日付は5日分しかない。

各スナップショットは `drive-scsi0`（60GiB OS）と
`drive-scsi1`（300GiB Longhorn）の**両方**を含む。

### 44-4. 実施したこと

PBS 側の prune ジョブに保持ポリシーを設定した。

```
proxmox-backup-manager prune-job update "default-gateway-backup-532eb832-" \
  --keep-last 3 --keep-daily 7 --keep-weekly 4 --keep-monthly 3
```

`keep-last 3` は vzdump 側のポリシーに無いが意図的に追加した。
VM 1200 は `kubernetes-daily-pbs` の対象外（vmid は 1001-1003,1101-1103）で
定期バックアップが無く、09-05 のスナップショット3個しか無い。
日付ベースの条件だけだと1個に減ってしまう。
**バックアップが5夜黙って止まっていた事実を踏まえた安全弁**でもある。

### 44-5. 未完了：prune を実行できていない

```
$ proxmox-backup-manager prune-job run "default-gateway-backup-532eb832-"
Error: write failed: No space left on device (os error 28)
```

**削除するにも書き込みが必要で、それができない。**

原因は ext4 の root 予約ブロック：

```
Block count:          118472704   (× 4096 = 485 GB)
Reserved block count:   5923635   (× 4096 = 24.3 GB, 5%)
Reserved blocks uid:  0 (user root)
```

PBS のプロセスは `backup` ユーザーで動くため、
利用可能量が 0 に見える（root にはまだ 24.3 GB ある）。
ジャーナルログ 492MB を削っても予約量に届かず効果が無い。

**必要な操作（権限で拒否されたため未実施）:**

```
ssh root@172.16.10.51 'tune2fs -m 2 /dev/mapper/pbs-root'
```

5%（24.3GB）→ 2%（9.7GB）で約14GBが `backup` ユーザーに開放される。
非破壊・即時・可逆（戻す場合は `tune2fs -m 5`）。
データストアがルートFS上にあり418G/444Gをバックアップが占める構成のため、
2% は恒久設定としても妥当と考える。

その後に prune → GC の順で実行する。
GC は atime ベースで 24h5m の猶予があるため、prune 直後の GC で
すぐに容量が戻るとは限らない点に注意。

## 45. 監視の追加

### 45-1. 何が足りなかったか

調べた結果、**メトリクスはすべて収集されていた。アラートが無かっただけ**だった。

PBS 障害の当日、Prometheus には以下が入っていた：

```
pbs_observer_datastore_avail_bytes{datastore="gateway-backup"} = 0
pbs_observer_latest_backup_task_success{node=...}              = 0   (3ノードとも)
pbs_observer_backup_tasks_failed_24h{node=...}                 = 1   (3ノードとも)
```

既存の `homelab.monitoring` グループは
`PBSObserverTargetMissing` と `BackupTelemetryCollectionFailing` のみ、
つまり「observer が動いているか」しか見ていなかった。
**収集は正常で、バックアップだけが失敗していた**ため何も鳴らなかった。

ストレージ障害についても同様で、ゲスト側の遅延は記録されていた。
障害時間帯（09-13 18:00–19:30 JST）の平均書き込み遅延ピーク：

| ノード | ピーク |
|---|---:|
| k8s-worker-1 | **54,449 ms** |
| k8s-worker-2 | **41,997 ms** |
| k8s-worker-3 | 5,495 ms |
| k8s-1 / k8s-2 / k8s-3（NVMe） | 0〜1 ms |

平常時は 0〜3 ms。control-plane が無傷なのは NVMe 上にあるため。

### 45-2. 追加したアラート（`kubernetes/infra/monitoring/alerts.yaml`）

新グループ `homelab.backup`:

| アラート | 条件 | severity |
|---|---|---|
| `PBSDatastoreFull` | 空き < 2%、10m | critical |
| `PBSDatastoreFillingUp` | 空き < 15%、1h | warning |
| `PBSBackupTaskFailing` | `latest_backup_task_success == 0`、6h | critical |
| `PBSBackupStale` | 最新スナップショットが36h以上前、30m | critical |
| `PBSBackupScheduleDisabled` | `schedule_enabled == 0`、1h | warning |
| `PBSGarbageCollectionFailing` | `gc_last_success == 0`、2h | warning |

`homelab.storage` に追加:

| アラート | 条件 | severity |
|---|---|---|
| `NodeDiskWriteLatencyHigh` | 平均書き込み遅延 > 100ms、15m | warning |
| `NodeDiskWriteLatencyCritical` | 平均書き込み遅延 > 1s、10m | critical |

### 45-3. 検証結果

CRD と Prometheus Operator の admission webhook（PromQL構文）を通過：

```
kubectl apply --dry-run=server -f kubernetes/infra/monitoring/alerts.yaml
→ prometheusrule.monitoring.coreos.com/homelab-rules configured (server dry run)
```

実データに対する評価：

| アラート | 現在 | 障害時 |
|---|---|---|
| `PBSDatastoreFull` | **発火**（実障害） | — |
| `PBSBackupTaskFailing` | **発火** 3ノード（実障害） | — |
| `PBSBackupStale` | **発火** 6VM（1200は正しく除外） | — |
| `PBSBackupScheduleDisabled` | 無発火（正） | — |
| `PBSGarbageCollectionFailing` | 無発火（正） | — |
| `NodeDiskWriteLatencyHigh/Critical` | **無発火**（誤検知なし） | **worker-1 で19サンプル中18回発火** |

`PBSBackupStale` の `backup_id!="1200"` 除外は、除外を外すと1200が
追加で一致することを確認済み（意図どおり動いている）。

### 45-4. Longhorn メトリクスが1つも無かった

`longhorn_*` のメトリクスが Prometheus に1件も存在しなかった。
`kubernetes/infra/longhorn/values.yaml` の ServiceMonitor が
`enabled: false` のままだったため。コメントには

> Prometheus Operator の CRD は monitoring 導入後に有効化する。

とあり、**回収されていない TODO** だった。CRD は既に存在する
（`kubectl get crd servicemonitors.monitoring.coreos.com`）ので
`enabled: true` にした。

⚠️ Longhorn のボリューム縮退・再構築に対するアラートは**まだ書いていない**。
manager の 9500 番ポートに直接 HTTP で問い合わせても空応答で、
実際のメトリクス名を確認できなかったため。
メトリクスが流れ始めてから、実名を確認した上で追加する。
**推測でルールを書くことはしない。**

## 46. `talos/patches/worker.yaml.tftpl` の修正

### 46-1. 実測：Talos は serial を見ていない

指示書は「Proxmox側の既存serialは `longhorn` だが、ゲストへの見え方を
推測しない」と警告していた。実際に確認した結果、**警告が的中した。**

`talosctl -n 172.16.40.21 get disks <id> -o yaml`（Talos v1.13.9）:

| Talos | サイズ | model | transport | serial | by-id symlink |
|---|---|---|---|---|---|
| `sda` | 60 GiB | QEMU HARDDISK | virtio | **無し** | `scsi-0QEMU_QEMU_HARDDISK_drive-scsi0` |
| `sdb` | 300 GiB | QEMU HARDDISK | virtio | **無し** | `scsi-0QEMU_QEMU_HARDDISK_drive-scsi1` |

`serial` フィールドは両方とも空で、by-id にも `longhorn` は現れない。
`disk.serial == "longhorn"` に書き換えていたら**何にも一致せず
ボリュームが作られなくなっていた。**

### 46-2. 変更内容

1. **コメントの訂正。** 「serial で判定している」は二重に誤り
   （設定が serial を使っていない上に、serial 自体が存在しない）。
   実測結果と確認コマンドを書いた。
2. **`minSize: 50GB` → `200GB`。** 将来 64GiB の DB 用ディスクを追加しても
   それを Longhorn 用として掴まないためのガード。
   現在の 300GiB ディスクは条件を満たすため**今日の挙動は変わらない**。

`match: '!system_disk'` と `grow: true` は**変更していない。**
検証できない CEL 式への書き換えは行わない。

### 46-3. 未検証・未適用

```
$ tofu state list
Error: Failed to request input from user for variable
       var.state_encryption_passphrase
```

state が暗号化されており passphrase が無いため **`tofu plan` を実行できない。**
指示書ステップ2-5「planでVM置換、既存ディスク削除・縮小、意図しない
Talos再適用や再起動がないことを確認する」は**未実施**。

**この変更はレビュー可能な差分として残すのみで、適用していない。**
適用前に必ず plan で確認すること。

## 47. `scripts/reconcile-proxmox-zfs-trim.sh`（新規）

§26-1 で「別作業として提案する」とした収束スクリプトを追加した。
`scripts/reconcile-pbs-kubernetes-backup.sh` の流儀に合わせている
（引数なしは確認のみ、`--apply` で反映、日本語コメント、冪等）。

```
使い方: reconcile-proxmox-zfs-trim.sh [--apply] [--trim-now]
```

- `--apply`：`autotrim=on` と `org.debian:periodic-trim=enable` を設定
- `--trim-now`：`--apply` 併用時のみ。未TRIMのプールに `zpool trim` を開始。
  実行中のTRIMがあるホストは飛ばす。

事前条件として、プールの存在と `health=ONLINE` を確認してから変更する。

検証：

```
bash -n          → OK
shellcheck       → 指摘なし
引数なし実行     → 3ホストの現在値を正しく表示
--apply          → 設定変更 0 件（冪等性を確認）
--trim-now 単独  → 「--apply と併用してください」で exit 1
```

## 48. Longhorn の外部バックアップ先 — 未実施

```
$ kubectl -n longhorn-system get backuptargets.longhorn.io
NAME      URL   CREDENTIAL   AVAILABLE
default                      false
```

`longhorn-backup-credential` Secret はクラスタにもリポジトリにも存在しない。
`kubernetes/infra/longhorn/README.md` に Cloudflare R2 を使う手順があるが、

- R2 バケットの作成
- R2 API トークンの発行
- SOPS での暗号化

はいずれも**利用者の認証情報が必要**で、こちらでは実施できない。
外部サービス上にリソースを作る操作でもあるため、独断では行わない。

CNPG のバックアップがクラスタ内 MinIO のみに依存している問題
（§41-5 の優先度:高 の2番）と同根であり、**未解決のまま残る。**

## 49. 変更ファイル（2026-09-15 ぶん）

| ファイル | 変更 | 適用状態 |
|---|---|---|
| `kubernetes/infra/monitoring/alerts.yaml` | アラート8個追加 | 未適用（ArgoCD経由でmergeされたら反映） |
| `kubernetes/infra/longhorn/values.yaml` | ServiceMonitor を有効化 | 未適用（同上） |
| `talos/patches/worker.yaml.tftpl` | コメント訂正＋`minSize` 200GB | **未適用・plan未実行** |
| `scripts/reconcile-proxmox-zfs-trim.sh` | 新規 | 実行して検証済み |
| `docs/storage-migration-2026-09-13.md` | 本追記 | — |

ライブに反映済みの変更は PBS の prune ジョブ保持ポリシーのみ（§44-4）。

他作業の変更（`kubernetes/infra/harbor/README.md`、
`scripts/bootstrap-cluster-secrets.sh`、`docs/moshitoku-monitoring-handoff.md`、
`scripts/reconcile-moshitoku-harbor.py`）には触れていない。

---

# 追記9：PBS の容量回復とバックアップ復旧（2026-09-16）

## 50. 実施内容

§44-5 で「tune2fs が必要」としたが、**tune2fs を使わずに解決できた。**

### 50-1. 空き容量の確保（journal の削減）

PBS のプロセスは `backup` ユーザーで動くため、ext4 の root 予約 5%（22.6 GiB）
により Available が 0 に見えていた。`prune` すら書き込めない状態。

systemd journal を削って `backup` ユーザーに書き込み余地を作った。

```
journalctl --vacuum-size=150M     # 547.8M → 149.1M、398.6M 解放
→ Available 0 → 312M
```

**tune2fs による予約率変更は行っていない（5% のまま）。**

### 50-2. prune

§44-4 で設定した保持ポリシーで実行。

```
proxmox-backup-manager prune-job run "default-gateway-backup-532eb832-"
→ TASK OK
```

スナップショット **66 個 → 30 個**（削除 36 個）。
削除されたのは全て 2026-09-06 の同日内スナップショット（§44-3）。
VM 1200 は `keep-last 3` により 3 個とも保持された（意図どおり）。

ただしインデックスは 1 グループ 29〜32 MB しかなく、この時点では
**412M しか空かない**。実体は `.chunks` にある。

### 50-3. GC

```
proxmox-backup-manager garbage-collection start gateway-backup
→ TASK OK
```

| 指標 | 前 | 後 |
|---|---:|---:|
| `/` 使用 | 422G (100%) | **238G (57%)** |
| 空き | **0** | **184G** |
| `.chunks` | 417G | 234G |
| チャンク数 | 139,775 | 93,497 |
| pending-bytes | 11.2 GiB | 0 |

**183GB を回収した。**

前回 GC が 2026-09-15 00:00 で、チャンクの atime がそこで止まっており、
GC の 24h5m 猶予を超えていたため即座に回収できた。

### 50-4. バックアップの復旧確認

単体で検証してから残りを実行した。

```
VM 1001: dirty-bitmap 2.1 GiB / 360.0 GiB、46 秒、99% 再利用 → OK
```

⚠️ **作業中のミス：** 残り 5 台を sv-proxmox-01 から一括実行したが、
`vzdump` はローカルノードの VM しか対象にできない。
1002/1003/1102/1103 は空振りした。各 VM の所在ノードで実行し直した。

| VM | ノード |
|---|---|
| 1001, 1101, 1200 | sv-proxmox-01 |
| 1002, 1102 | sv-proxmox-02 |
| 1003, 1103 | sv-proxmox-03 |

最終結果（2026-09-16 07:45–07:58 JST）:

```
1001 (6)  2026-09-15T22:45:04Z      1101 (5)  2026-09-15T22:46:15Z
1002 (6)  2026-09-15T22:57:52Z      1102 (5)  2026-09-15T22:58:37Z
1003 (6)  2026-09-15T22:57:52Z      1103 (5)  2026-09-15T22:58:36Z
1200 (3)  2026-09-05T19:33:25Z      ← 日次ジョブ対象外（§44-4）
```

**k8s ノード VM 6 台すべてが最新化され、5 夜の欠測が解消した。**
容量は 280G 使用 / 142G 空き / 67%。

## 51. control-plane の Longhorn ディスクをバックアップ対象から除外

### 51-1. 根拠

- Longhorn の storage node として登録されているのは **worker 3 台のみ**
  （`kubectl -n longhorn-system get nodes.longhorn.io`）
- control の `scsi1` は実使用 **816K / 300G** の空ディスク
- それでも毎回 360 GiB 分の対象として扱われていた

worker 側は ADR-0008 階層 3 として Longhorn データの唯一のクラスタ外
コピーであり（階層 2 の Velero は**未デプロイ**であることを確認）、
**必ず対象に残す。**

### 51-2. 変更

`tofu/10-proxmox-talos/vms.tf`：

```hcl
disk {
  ...
  serial = "longhorn"
  backup = each.value.role != "controlplane"
}
```

あわせて、同ファイルにあった誤ったコメント
（「serial を固定している。Talos の diskSelector がこの値で判別する」）を
§46-1 の実測結果に基づいて訂正した。

`tofu fmt -check` / `tofu validate` は通過。

ライブ側は `qm set` で `backup=1` → `backup=0` に変更した。
変更前後の文字列を比較し、**backup トークン以外が変わらないことを
確認してから**適用している。

### 51-3. 検証

```
INFO: include disk 'scsi0' 'local-lvm:vm-1001-disk-0' 60G
INFO: exclude disk 'scsi1' 'local-zfs:vm-1001-disk-1' (backup=no)
INFO: transferred 380.00 MiB in 11 seconds
INFO: Finished Backup of VM 1001 (00:00:11)
```

control のバックアップは **2.1 GiB / 46 秒 → 380 MiB / 11 秒**。
worker 3 台は `backup=1` のまま、6 ノードすべて Ready を維持。

### 51-4. 本来の解決策

PBS が 1 台あたり 360 GiB を扱っているのは、ADR-0008 階層 2 が
存在しないためである。Longhorn の外部バックアップ（→ S3/R2）を
用意すれば PBS は OS ディスクだけで足り、1 台 360 GiB → 60 GiB になる。
R2 の認証情報が必要なため未実施（§48）。

---

# 追記10：ディスク遅延アラートの閾値を訂正（2026-09-16、マージ後）

## 52. マージ後の反映確認

PR #1 をマージ後、ArgoCD が `fe88259` を取り込むまで待って確認した
（ポーリング待ちだったため hard refresh で促した）。

| 項目 | 結果 |
|---|---|
| trivy スキャンジョブの securityContext | ✅ `runAsNonRoot` / `seccompProfile` が反映 |
| スキャンジョブ | ✅ PSA 拒否なし、Pod が Created → Started |
| `trivy_image_vulnerabilities` | ✅ **出現（合計 71）** — §45-4 の課題1は解決 |
| Longhorn ServiceMonitor | ✅ 作成、3 ターゲット up、`longhorn_*` 51 メトリクス |
| 新規アラート 8 個 | ✅ ロード済み |

Longhorn のメトリクス名も確定した（`longhorn_volume_robustness` /
`longhorn_volume_state` / `longhorn_node_status` / `longhorn_disk_status` /
`longhorn_replica_state` 等）。

> ⚠️ `longhorn_volume_robustness` は 88 系列あり、うち 66 系列が値 0 だった。
> 3 台の manager が全ボリュームについて報告し、所有していないものは 0 を
> 返すためと見られる。アラートを書く際は `max by (volume)` 等で
> 畳む必要がある。**未検証のため、まだルールは書いていない。**

## 53. 自分が入れたアラートに欠陥があった

§45-2 で追加した `NodeDiskWriteLatencyHigh` / `Critical`（100ms / 1s）は、
反映直後から複数ノードで pending になった。実測すると：

| デバイス | 遅延 | IOPS |
|---|---:|---:|
| worker-1 `dm-1`（LUKS 層） | 3,924 ms | 242 |
| worker-3 `sdg`（Longhorn ボリューム） | 1,780 ms | — |
| worker-1 `sda`（OS ディスク） | 1,549 ms | 119 |

同時刻の Proxmox ホスト側 SATA は **1 ms / busy 20%**。
つまりゲスト側の値は物理ディスクの遅延ではなく、
**Longhorn の複製往復と LUKS 層を含んだ数字**であり、
CI やイメージスキャンの最中に数秒へ達するのは正常だった。

24 時間 288 サンプルでの誤検知率：

| 閾値 | 24h 誤検知 | 09-13 障害時の一致 |
|---|---:|---:|
| > 0.1 s（当初の warning） | **45 / 288** | — |
| > 1 s（当初の critical） | **28 / 288** | 18 / 19 |
| > 5 s | 8 / 288 | — |
| > 10 s | 0 / 288 | **2 / 19** |
| > 30 s | 0 / 288 | — |

**当初の閾値は常態的に鳴り続けて無視されるようになる。**
一方、単純に閾値を上げると障害時も `for: 15m` を満たさなくなる
（> 10 s は 19 サンプル中 2 回のみ、しかも非連続）。

## 54. 訂正：瞬間値ではなく持続時間で見る

`min_over_time(...[30m:1m])`＝「30 分間一度も下回らなかったか」で判定する。
スパイクと持続的劣化を分離できる。

| 式 | 24h 誤検知 | 障害時の一致 |
|---|---:|---:|
| `min_over_time(...[30m:1m]) > 0.5` | **1 / 288** | **13 / 19** |
| `min_over_time(...[30m:1m]) > 1` | **0 / 288** | 5 / 19 |

採用値：

- warning `> 0.5` / critical `> 1`、いずれも `for: 5m`（デバウンスのみ）
- `device!~"dm-.*"` を追加。LUKS / device-mapper が下位デバイスの I/O を
  二重計上するため

検証（訂正後）：

```
現在        : warning 0 系列 / critical 0 系列
24h 誤検知  : warning 1 / 288、critical 0 / 288
09-13 障害時: warning  172.16.40.21×9  172.16.40.23×6  172.16.40.30×4
              critical 172.16.40.23×5
```

## 55. 残る限界

この指標は**物理ディスクの遅延そのものではない。**
本来は Proxmox ホスト（172.16.10.11-13）を node_exporter で直接
スクレイプすべきだが、現状は監視対象外である。
今回の障害で最も直接的な証拠だった
「SATA write await」「ZFS txg sync 時間」「zpool の未 TRIM 状態」は、
いずれも**今も Prometheus からは見えない**。

---

# 追記11：残った穴を埋める（2026-09-16）

§55 で「物理ディスクの遅延・ZFS の txg 同期時間・zpool の未 TRIM 状態は
今も Prometheus から見えない」と書いた件、および Longhorn のアラート未作成を解消した。

## 56. Longhorn のアラート（6 件）

### 56-1. メトリクスの実体を確認した

§52 で「`longhorn_volume_robustness` は 88 系列中 66 系列が 0 で、
3 台の manager が全ボリュームを報告しているため畳み込みが必要」と
書いたが、**これは誤りだった。**

実際には `state` ラベルによる one-hot である。

```
longhorn_volume_robustness{volume,pvc,pvc_namespace,node,state}
  state = healthy | degraded | faulted | unknown
  該当する state が 1、それ以外が 0
```

したがって `max by (volume)` ではなく `{state="degraded"} == 1` で判定する。

### 56-2. レプリカ数では判定できない

`longhorn_replica_state{state="running"}` で実レプリカ数は取れるが、
**CNPG の 6 ボリュームは設計上レプリカ 1**（冗長化は PostgreSQL の
3 インスタンスが担う、§4-1）。単純な「コピー 1 個」判定は誤検知する。

robustness は希望数との比較を含むため、これを主判定に使う。
そのうえで「希望数を下回り、かつ実際に動いているコピーが 1 個」を
join で取り出したのが `LonghornVolumeDegradedSingleReplica` で、
これが §11-1 の状況（Prometheus / Loki / MinIO が worker-3 に 1 コピー）に対応する。

### 56-3. 追加したルール

| アラート | 条件 | for | severity |
|---|---|---|---|
| `LonghornVolumeDegraded` | `robustness{state="degraded"} == 1` | 30m | warning |
| `LonghornVolumeDegradedSingleReplica` | 上記 かつ running レプリカ < 2 | 10m | critical |
| `LonghornVolumeFaulted` | `robustness{state="faulted"} == 1` | 5m | critical |
| `LonghornNodeNotReady` | `node_status{condition="ready"} == 0` | 10m | critical |
| `LonghornNodeNotSchedulable` | `node_status{condition="schedulable"} == 0` | 30m | warning |
| `LonghornDiskFillingUp` | `disk_usage / disk_capacity > 0.75` | 1h | warning |

`degraded` に 30m を置いたのは、再構築中は正常に degraded を通過するため。
30 分続く場合は「再構築が進んでいない」を意味する。

検証：6 件すべて現在 silent。single-replica の join が
**設計上 1 レプティカの 6 ボリュームに一致しない**ことも確認した
（1 レプリカ 6 件、うち degraded 0 件）。
障害時の再現検証はできない（当時メトリクスが存在しなかったため）。

## 57. Proxmox ホストのメトリクス

### 57-1. 経路の設計

`CiliumClusterwideNetworkPolicy/deny-egress-to-home-network` が
172.16.10.0/24 への egress を全面拒否している（ポート指定なしの `toCIDR`）。
ホストは 172.16.10/20/30 にしかアドレスを持たず、VLAN40 には居ないため、
迂回路も無い。

`monitoring` を除外リストへ追加すれば直接スクレイプできるが、それは
**Prometheus 本体に管理ネットワークへの経路を与える**ことを意味する。
加えて、除外すると同時に `egress: toEntities: all` の対象からも外れるため、
Prometheus の egress をすべて書き直す必要がある（書き損じるとスクレイプが止まる）。

採用した経路：

```
Proxmox ホスト
  prometheus-node-exporter      … 物理ディスクの I/O カウンタ
  zfs-textfile-collector.timer  … TRIM 状態・txg 同期時間
    → portal/pbs-observer が 9100 を読み、必要な系列だけ pve_ 接頭辞で再出力
      → Prometheus は従来どおり pbs-observer だけをスクレイプ
```

`portal` は既に除外リストにあり、許可先を 172.16.10.11-13 の 9100 のみ
追加した。宅内へ到達できるのは 212 行の専用エクスポーターのままで、
Prometheus 本体には経路を与えない。

### 57-2. ホスト側（実施済み・3 台とも稼働中）

`scripts/reconcile-proxmox-node-exporter.sh --apply`

- `prometheus-node-exporter` 1.9.0-1+b4 を導入
- 待ち受けを **vmbr0 の管理 IP に固定**（既定の 0.0.0.0 だと VLAN20/30 にも出る）
- `scripts/proxmox-zfs-textfile-collector.sh` を配置、1 分間隔の systemd timer

⚠️ Proxmox のファイアウォールは無効（`pve-firewall status` = disabled）。
node_exporter は管理 LAN 上の誰からでも読める。公開するのはシステム統計で
資格情報は含まないが、**管理 LAN が信頼境界であるという前提に依存する**。

### 57-3. node_exporter が出さないもの

node_exporter 1.9.0 の ZFS collector は ARC / ZIL / dataset は出すが、
**TRIM 状態と txg 同期時間は出さない**（`node_zfs_zpool_*` は
dataset と ZIL のみ）。今回の障害で決定的だった 2 つがちょうど欠けている。

そのため textfile collector を自作した。出力（1 ホストあたり 12 系列）:

```
zfs_pool_trim_state{pool,device,state}      untrimmed/trimming/completed/unsupported の one-hot
zfs_pool_trim_progress_ratio{pool,device}
zfs_pool_autotrim{pool}
zfs_pool_periodic_trim_enabled{pool}        ← §24 の設定漏れを直接検知する
zfs_pool_health{pool}
zfs_pool_fragmentation_ratio{pool}
zfs_pool_capacity_ratio{pool}
zfs_pool_txg_sync_seconds_max{pool}         直近 98 txg（約 495 秒）の最大
zfs_pool_txg_sync_seconds_last{pool}
```

**⚠️ 実装上の落とし穴：** ホストによって awk の実装が違う。

```
host 11: /usr/bin/gawk
host 12: /usr/bin/mawk
host 13: /usr/bin/mawk
```

最初 3 引数の `match(s, re, arr)`（gawk 拡張）を使ったため、
ホスト 11 では動きホスト 12/13 で `syntax error` になった。
`sub()` だけを使う POSIX の範囲へ書き直してある。

### 57-4. 取り込み側（`pbs-observer`）

`NODE_EXPORTER_KEEP` に列挙した系列と `zfs_pool_` 接頭辞だけを通し、
`pve_` を付けて `pve_node` ラベルを足して再出力する。

実データでの絞り込み結果（ホスト 11）:

```
node_exporter 全系列 : 3,649
再出力する系列       :   155  (4.2%)
```

素通しすると数千系列になるため、明示したものだけを通す設計にしている。

### 57-5. 追加したルール（6 件）

| アラート | 条件 | severity |
|---|---|---|
| `ProxmoxZFSPoolUntrimmed` | `trim_state{state="untrimmed"} == 1`、1h | warning |
| `ProxmoxZFSPeriodicTrimDisabled` | `periodic_trim_enabled == 0`、6h | warning |
| `ProxmoxZFSAutotrimDisabled` | `autotrim == 0`、6h | warning |
| `ProxmoxZFSTxgSyncSlow` | `txg_sync_seconds_max > 30`、10m | critical |
| `ProxmoxHostDiskWriteLatencyHigh` | 物理 write await > 50ms、15m | warning |
| `ProxmoxZFSPoolNotOnline` | `health == 0`、5m | critical |

閾値の根拠（いずれも本記録の実測値）:

- txg 同期：障害時 51.8s / ピーク 154.7s、CI 負荷下の平常値 13.6s → **30s**
- 物理 write await：障害時 190〜319ms、平常時 1ms 未満 → **50ms**

ホスト側は Longhorn の複製往復も LUKS も挟まらない物理ディスクの値なので、
ゲスト側の代理指標（§54 で 0.5s / 1s まで上げざるを得なかった）より
**1 桁鋭い閾値が使える。** これが §55 で埋めたかった穴そのものである。

`ProxmoxZFSPoolUntrimmed` と `ProxmoxZFSPeriodicTrimDisabled` は、
**今回の根本原因そのものを検知する。** 設定漏れがあれば 2026-09-06 の
プール作成直後に鳴っていた。

## 58. 検証できたこと・できていないこと

| 対象 | 状態 |
|---|---|
| node_exporter 3 台稼働、`zfs_pool_*` 12 系列ずつ公開 | ✅ 実機確認 |
| collector が gawk / mawk 両方で動作 | ✅ 両方で実行確認 |
| 再出力フィルタ（3,649 → 155 系列、ラベル結合） | ✅ 実データで確認 |
| NetworkPolicy / alerts.yaml / kustomize build | ✅ server dry-run 通過 |
| Longhorn 6 ルールが現在 silent、CNPG を誤検知しない | ✅ 実データで確認 |
| **pod → ホスト 9100 の到達（E2E）** | ❌ **未確認** |
| **`pve_*` が Prometheus に届くこと** | ❌ **未確認** |

E2E を確認できていないのは、ArgoCD の `root` アプリ（app-of-apps）が
`homepage` の同期設定を戻し、Deployment と NetworkPolicy を
git の内容へ巻き戻すため。マージ後に確認する。

一時的に適用したものはすべて ArgoCD が巻き戻し済みで、手当ての残骸は無い。

---

# 追記12：マージ後の検証と、自分が入れた欠陥の修正（2026-09-16）

## 59. §58 で未確認だった E2E が通った

PR #4 マージ後に確認した。

```
pod → 172.16.10.11:9100  OK  zfs_pool_ 12 系列
pod → 172.16.10.12:9100  OK  zfs_pool_ 12 系列
pod → 172.16.10.13:9100  OK  zfs_pool_ 12 系列

pbs_observer_source_success{source="node_exporter_sv-proxmox-0X"} 1.0  ×3
pbs-observer が出す系列: 521（うち pve_* が 455）
Prometheus に届いた pve_* のメトリクス名: 17
```

**§55 で「今も見えない」と書いた 3 つが、すべて見えるようになった。**

```
pve_zfs_pool_trim_state{state="untrimmed"} == 1  →  0 件（3 台とも TRIM 済み）
pve_zfs_pool_txg_sync_seconds_max  sv-proxmox-01: 12.43s（CI 負荷下）
                                   sv-proxmox-02:  0.24s
                                   sv-proxmox-03:  0.04s
物理 write await  sda: 0〜10ms / nvme0n1: 0ms
```

## 60. `ProxmoxHostDiskWriteLatencyHigh` に欠陥があった

閾値 50ms は物理ディスク（sda 0〜10ms、nvme 0ms）に対しては妥当だったが、
**デバイスを絞っていなかった。**

ホストの node_exporter は zvol も `node_disk_*` として出す。
1 ホストあたり 55 系列あり、実測で最大 73ms。

| デバイス種別 | 件数 | write await |
|---|---:|---:|
| 物理（sda / nvme0n1） | 2 | 0〜10 ms |
| zvol（zd*） | 55 | 最大 73 ms |

zvol は VM の仮想ディスクで、下位デバイスの I/O を別名で二重計上している。
**デプロイ直後の時点で 3 系列が閾値を超えており、誤検知する状態だった。**

§53 でゲスト側の指標について同じ誤りをしている（Longhorn の複製往復と
LUKS を含む値に物理ディスク向けの閾値を当てた）。今回は「ホスト側だから
物理値」と考えてデバイス種別を見落とした。同じ失敗の形である。

### 60-1. 修正

**エクスポーター側**で `zd*` / `dm-*` / `loop*` / `sr*` を落とす。

```
1 ホストあたり 155 系列 → 25 系列
残る物理デバイス: sda, nvme0n1
```

VM の仮想ディスクの遅延は、ゲスト側の node_exporter で見るのが正しい。
ホスト側で二重に持つ意味が無く、系列数も無駄に増える。

**アラート側**でも `device=~"sd.*|nvme.*"` を明示する。
将来デバイス種別が増えたときに、黙って対象が広がらないようにするため。

### 60-2. 検証

```
絞り込み前（デプロイ中の式）: 3 系列が一致（すべて zvol）
絞り込み後                  : 0 系列（silent）
```

---

# 追記13：CNPG のバックアップを Cloudflare R2 へ（2026-09-16）

§48 で「Longhorn の外部バックアップ先が未設定」として残していた件。
利用者の判断で、Longhorn 全体ではなく **CNPG のバックアップだけ**を
クラスタ外へ出す方針とした。

## 61. 何を守る必要があるのかを先に測った

Longhorn の実データ 46.2 GiB の内訳（`status.actualSize` 実測）:

| 分類 | 容量 | 失ったら |
|---|---:|---|
| **DB バックアップ**（MinIO 内 moshitoku 200MB / umami 31MB） | **231 MB** | **復旧不能** |
| 監視データ（Prometheus 22.9G / Loki 4.1G＋chunks 2.2G / Tempo） | 約 29 GB | 困らない（保持 15 日） |
| コンテナイメージ（harbor-registry 2.7G / image-registry 1.1G） | 約 4 GB | CI で再ビルド可能 |
| Harbor 内部（trivy DB / redis / jobservice） | 約 1.8 GB | 再構築可能 |
| DB 本体のボリューム（6 インスタンス） | 約 4.4 GB | 上の 231MB から復元可能 |

**守る必要があるのは 231 MB。** DB の論理サイズは各 7.5 MB で、
残りは WAL とインデックスである。

### 61-1. 本当の穴は「置き場所」だった

「バックアップが無い」わけではなかった。PBS が worker VM の 300GiB
ディスク（＝Longhorn データそのもの）を日次で取っている。

穴は一点だけ：**CNPG のバックアップが MinIO＝Longhorn 上にあり、
守るべき対象と同じストレージに載っていた。** Longhorn が論理破損すれば
DB とそのバックアップが同時に消える。ADR-0008 が旧構成の
`minio-for-velero` について警告していたのと同じ形である。

## 62. コスト

R2 の料金（2026-09-16 時点の理解。契約前に現行料金を要確認）:
ストレージ $0.015/GB・月、Class A $4.50/100万、Class B $0.36/100万、
**エグレス無料**、無料枠 10 GB・月。

| 案 | 容量 | 月額 |
|---|---:|---:|
| **採用：CNPG バックアップのみ** | 231 MB（30 日保持で 1〜2 GB） | **$0**（無料枠内） |
| Longhorn 全ボリューム | 46 GB | 約 $0.54 |

エグレス無料は復元時に効く（S3 なら 46GB の復元で約 $4）。

## 63. 設計判断

- **MinIO を置き換える**（併用しない）。barman-cloud は WAL アーカイバを
  1 つしか持てず、MinIO→R2 の同期 CronJob を足すと「同期が黙って止まる」
  失敗モードが増える。本セッションで既にその形の失敗を 2 度見ている。
- **トークンはバケットごとに 2 つに分ける。** 機能上は 1 つで足りるが、
  それぞれ別 namespace の Secret に入るため、共有すると片方の namespace が
  侵害されたときにもう片方のバックアップまで削除できる。
- **アカウント API トークンを使う**（ユーザートークンではない）。
  ユーザーの権限が変わると黙って失効するため。

### 63-1. 塞がらない穴

Object Read & Write には削除権限が含まれ、barman は `retentionPolicy: 30d`
の実行にこれを必要とする。**書き込み専用にはできず、侵害されたクラスタは
R2 上のバックアップを削除できる。** ADR-0008 の S6（ランサムウェア）は
完全には塞がらない。バケットロック／オブジェクト保持で塞げる可能性があるが、
現行仕様は未確認。

主目的の S5（ストレージの論理破損で DB とバックアップを同時に失う）は
R2 に置くだけで確実に塞がる。

## 64. 実機で確認したこと

### 64-1. バケット名が違っていた

依頼時に伝えられた `homelab-moshitoku-postgre` は、実際には
**`homelab-moshitoku-postgres`** だった（末尾の `s`）。
署名付き HEAD リクエストで両方を試して確定させた。

```
[moshitoku] homelab-moshitoku-postgres : OK (200)
[moshitoku] homelab-moshitoku-postgre  : HTTP 403
[umami]     homelab-umami-postgres     : OK (200)
[umami]     homelab-umami-postgre      : HTTP 403
```

1 文字違えばバックアップは無言で失敗する。**推測で書かず実機で確かめた。**
互いのバケットに 403 が返ることから、トークンのスコープ分離も確認できた。

### 64-2. NetworkPolicy の追加は不要だった

当初「両 namespace に R2 への egress 追加が必要」と判断したが、**誤りだった。**

`CiliumClusterwideNetworkPolicy/deny-egress-to-home-network` は
除外リスト（`ceph-csi` / `kube-system` / `portal` / `velero` / `status`）
以外の全 namespace に `egress: toEntities: all` を与えている。
`analytics` も `moshitoku` も対象なので、宅内 CIDR 以外へは既に出られる。

実機確認:

```
analytics/umami-postgres-1  → R2:443  CONNECTED
moshitoku/moshitoku-postgres-1 → R2:443  CONNECTED
```

一度書いたポリシー追加は撤回した。`rules.dns` を足すと namespace 全体の
DNS を壊すリスクがあり、不要な変更でそれを冒す理由が無い。
代わりに ObjectStore のコメントへ「将来除外リストへ入れたら明示的な
egress が要る」と記録した。

### 64-3. R2 は IPv4/IPv6 両方を返す

クラスタは `enable-ipv6=false` のため IPv4 が使われる。
`getent ahosts` では IPv6 が先に返るが、接続は成立している。

## 65. ⚠️ 誤診断を 1 つ記録しておく

検証中、ObjectStore を R2 に向けた直後のバックアップが `completed` に
なったのに R2 は空だった。このとき **「ObjectStore を変更しても稼働中の
インスタンスは古い設定を使い続ける」と誤って結論し**、不要な
ローリング再起動（`cnpg.io/restartedAt`）まで試した。

実際は **ArgoCD が設定を巻き戻していた**だけだった。
`umami` アプリの自動同期を止めても、root（app-of-apps）がそれを復元し、
未 push のブランチではなく `main` の内容＝MinIO へ selfHeal していた。
バックアップはその時点で正しく MinIO へ書いていた。CNPG の挙動は正常。

**この環境ではマージ前のライブ検証が成立しない**（`homepage` でも
同じ壁に当たった）。`root` アプリを止めない限り、どの app の一時変更も
巻き戻される。

## 66. 変更内容

| リポジトリ / ファイル | 変更 |
|---|---|
| homelab `kubernetes/infra/umami/database.yaml` | ObjectStore を R2 へ |
| homelab `scripts/reconcile-cnpg-r2-credentials.sh` | 新規（1Password から Secret 投入） |
| homelab `kubernetes/infra/monitoring/alerts.yaml` | CNPG バックアップ監視 4 件（追記済み） |
| moshitoku `deploy/kubernetes/database.yaml` | ObjectStore を R2 へ |

Secret は投入済み（`analytics/umami-r2-credentials`、
`moshitoku/moshitoku-r2-credentials`、いずれも 1Password 由来）。

## 67. マージ後に必ず確認すること

1. ObjectStore が R2 を指していること
2. 手動バックアップを 1 回実行し、**R2 のバケットに実際にオブジェクトが
   増えることを確認する**（`completed` の表示だけでは足りない。§65）
3. `CNPGNoBackupEver` / `CNPGBackupFailing` が鳴らないこと
4. WAL アーカイブが R2 へ流れること（`cnpg_pg_stat_archiver_failed_count`
   が増えないこと）

**旧 MinIO のバケットは、R2 からの復元確認が取れるまで削除しない。**
切り替え後は更新が止まるだけで、中身は残る。

## 68. 残る課題

- **R2 からの復元は未検証。** バックアップが取れることと戻せることは別である。
  隔離した環境への復元確認は別作業として残る。
- Longhorn 本体の外部バックアップ（監視データ・コンテナイメージ）は
  引き続き PBS のみ。§61 のとおり代替可能なデータなので優先度は低い。
- ADR-0008 の階層 2（Velero）は未デプロイのまま。本変更は階層 2 の
  目的（S5）を CNPG に限って満たすもので、ADR の全体像を満たすものではない。
  **ADR-0008 を実態に合わせて更新する必要がある。**

---

# 追記14：ディスク遅延アラートの 3 度目の修正（2026-09-16）

## 69. またも自分が入れた欠陥

R2 移行の検証中に `NodeDiskWriteLatencyHigh` が発火した。

```
172.16.40.23:9100  sdj  615ms  （worker-3 の Longhorn ボリューム）
```

同時刻のホスト側物理ディスクは **sda 0ms / nvme 0ms**。実害は無かった。

原因は**分母の小ささ**だった。

```
sdj の直近 1 時間のピーク書き込み IOPS = 0.026   （約 39 秒に 1 回）
現在の分子 / 分母 = 0 / 0 → NaN
```

ほぼアイドルなボリュームへの散発的な遅い書き込みを、5 分レートの窓が
持ち回ることで「30 分間 0.5s を下回らない」条件を満たし続けていた。

## 70. これで同じ種類の失敗が 3 度目である

| # | 何をした | 何を見落とした |
|---|---|---|
| 1 | §53（PR #2） | ゲスト側の値に Longhorn の複製往復と LUKS が含まれることを見落とし、物理ディスク向けの閾値を当てた |
| 2 | §60（PR #5） | ホスト側で zvol を除外せず、VM の仮想ディスクを物理ディスクとして扱った |
| 3 | 本節（PR #8） | 分母（IOPS）を見ず、アイドルなデバイスでも比が成立することを見落とした |

いずれも**「その数字がどの層の、どういう条件下の値か」を確かめずに
閾値を置いた**という同じ形をしている。

## 71. 修正：書き込みが実際に起きていることを条件に加える

障害時に遅かったデバイスは **1.4〜4.0 IOPS** 出ていた。
誤検知した `sdj` は **0.026 IOPS**。20 倍以上の差がある。

```promql
(min_over_time( (write_time率 / writes_completed率) [30m:1m] ) > 閾値)
  and
(min_over_time( writes_completed率 [30m:1m] ) > 0.5)
```

### 71-1. critical は 1s → 800ms へ下げた

ガードを入れると、**1s では 09-13 の障害を一度も拾えなくなった。**
1s を 30 分継続していたのは IOPS がガードを下回るデバイスだけだったためである。

| 閾値 | IOPS 下限 | 24h 誤検知 | 障害時 |
|---|---|---:|---|
| > 1s | > 0.5 | 0/288 | **なし** |
| > 800ms | > 0.5 | 0/288 | worker-1 ×7 |

鳴らない critical に意味は無いので下げた。

### 71-2. 検証結果

| ルール | 現在 | 24h 誤検知 | 09-13 障害時 |
|---|---|---:|---|
| `NodeDiskWriteLatencyHigh`（>0.5s / IOPS>0.5） | silent | **1/288**（ガード無しは 4/288） | worker-1×8、.30×3 |
| `NodeDiskWriteLatencyCritical`（>0.8s / IOPS>0.5） | silent | **0/288** | worker-1×7 |

## 72. R2 移行の検証（続き）

`umami` は R2 への切り替え済みで、実際に書かれることを確認した。

```
umami-postgres-v1/base/20260916T033427/backup.info      1,432 B
umami-postgres-v1/base/20260916T033427/data.tar.gz      4.07 MiB
```

WAL アーカイブも動作している（`archived_count` 15→16、`failed_count` は
273 のまま増えず）。一度停止して見えたのは、完全にアイドルな DB では
`pg_switch_wal()` が実質 no-op になるためだった。`txid_current()` で
書き込みを起こせば正常に流れる。

**moshitoku は未マージのため MinIO のままである。** 守るべき 231 MB のうち
200 MB は moshitoku 側なので、そちらをマージするまで目的は達成されていない。
