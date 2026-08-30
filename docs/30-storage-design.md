# 30. ストレージ設計（既存 Ceph の Kubernetes 統合）

## 1. 既存 Ceph の実測状態

```
cluster:
  id:     10f95430-060d-4bd2-9d93-db845d9c748f
  health: HEALTH_WARN
          3 OSD(s) experiencing slow operations in BlueStore
          2 OSD(s) experiencing stalled read in db device of BlueFS
services:
  mon: 3 daemons (sv-proxmox-01/02/03)
  mgr: 1 active + 2 standby
  mds: 1/1 active + 2 standby
  osd: 3 osds: 3 up, 3 in
data:
  pools:  5 pools, 129 pgs
  usage:  592 GiB used, 2.2 TiB / 2.8 TiB avail
  pgs:    129 active+clean
```

| プール | 用途 | size/min_size | 本構成での扱い |
| --- | --- | --- | --- |
| `.mgr` | Ceph 内部 | 3/2 | 触らない |
| `cephfs01_data` / `cephfs01_metadata` | CephFS `cephfs01` | 3/2 | ISO・バックアップ用途。K8s は `/volumes/csi` 配下のみ使用 |
| `cephrdb_vm` | Proxmox VM ディスク | 3/2 | Proxmox 専用。**K8s からはアクセス不可にする** |
| `cephrdb_k8s` | Kubernetes 用 RBD | 3/2 | **本構成のメイン**（既に約 186 GiB 使用済み） |

## 2. ⚠️ 構築前に解決すべき HEALTH_WARN

`scripts/preflight.sh` はこの警告を検出したら **exit 1** する。理由は、
Kubernetes の PV がこの Ceph の上に載る以上、ストレージ層の不安定さが
そのままアプリの不安定さになるためである。

### 想定される原因と切り分け

| 原因候補 | 確認コマンド | 対処 |
| --- | --- | --- |
| コンシューマ向け SSD の DRAM-less / SLC キャッシュ枯渇 | `ceph tell osd.* bench`、`smartctl -a /dev/nvme0n1` | エンタープライズ SSD（PLP 付き）へ換装 |
| SSD の TBW 消耗・寿命 | `smartctl -A` の `Percentage Used` / `Media Wearout` | 換装 |
| BlueStore の RocksDB (BlueFS) 肥大化 | `ceph daemon osd.N perf dump bluefs` | `ceph-bluestore-tool` で compact |
| discard/TRIM 未発行 | `ceph config get osd bdev_enable_discard` | `bdev_enable_discard=true`, `bdev_async_discard=true` |
| ネットワーク（VLAN30）の遅延 | `ceph osd perf`、`iperf3` | MTU / リンク速度の確認 |

> 3 OSD しか無い構成では 1 本の劣化が全体に波及する。**Kubernetes を載せる前に
> 原因を特定すること**を強く推奨する。preflight を `--skip-ceph-health` で
> 迂回することは可能だが、その場合はリスクを理解した上での判断とする。

### 2026-08-30 に実施した調査と対処

#### 判明した事実

```
# OSD のバッキングデバイス
NAME      SIZE   ROTA MODEL
sda     953.9G     0  SUNEAST SE800 Lite SSD 1024GB   ← OSD が載っているのはこちら
nvme0n1 465.8G     0  CT500P2SSD8                     ← Proxmox のシステム領域

# SMART
Power_On_Hours          29201     （約 3.3 年）
Wear_Leveling_Count     100       （摩耗は問題なし）
Media_Wearout_Indicator 261624

# OSD のログ（osd.1 起動時）
bdev(/var/lib/ceph/osd/ceph-1/block) read stalled read 0xbb8de6a000~3000 (buffered)
  since 35252.30s, timeout is 5.00s
```

**結論: 消耗ではなく、SSD そのものの応答特性が原因である。**

`SUNEAST SE800 Lite` はコンシューマ向けの SATA SSD であり、

- **PLP（電源断保護）を持たない** — Ceph が発行する同期書き込み（fsync）ごとに
  実際のフラッシュ書き込みが発生し、レイテンシが跳ね上がる
- **DRAM キャッシュを持たない可能性が高い**（"Lite" 型番） — FTL のアドレス変換
  テーブルを NAND から都度読むため、ランダム読み取りが数秒スパイクすることがある

摩耗指標が正常であることから、「壊れかけている」のではなく
「元々 Ceph の要求に対して性能が足りない」状態である。
`DB_DEVICE_STALLED_READ_ALERT` は Ceph 19.2 (Squid) で追加された比較的新しい警告で、
まさにこの種のデバイスを検出するためのものである。

#### 実施した対処

```bash
# TRIM/discard を有効化（コンシューマ SSD では書き込み性能に大きく効く）
ceph config set osd bdev_enable_discard true
ceph config set osd bdev_async_discard_threads 1

# 設定を反映するため OSD を 1 台ずつ再起動
ceph osd set noout && ceph osd set norebalance
systemctl restart ceph-osd@0      # sv-proxmox-01
# → active+clean を確認してから次へ
systemctl restart ceph-osd@1      # sv-proxmox-02
systemctl restart ceph-osd@2      # sv-proxmox-03
ceph osd unset noout && ceph osd unset norebalance
```

結果: 警告の対象が **3 OSD → 1 OSD** に減少。全 PG が `active+clean`。

#### ⚠️ ただし、これは根本解決ではない

正直に記録しておく。

| 項目 | 評価 |
| --- | --- |
| `bdev_enable_discard` の効果 | **書き込み**性能の劣化を抑える。TRIM が発行されることで SSD 内部の GC が効率化する |
| stalled **read** への効果 | **限定的**。読み取りの遅延は SSD コントローラの応答特性に起因するため、Ceph 側の設定では解消しきれない |
| 警告が減った理由 | 警告は直近の観測に基づくため、OSD の再起動でカウンタがリセットされた側面がある。時間経過で再発する可能性がある |

**根本解決には、PLP 付きのエンタープライズ SSD（Intel/Solidigm D3-S4520、
Samsung PM893、Micron 5400 PRO 等）への換装が必要である。**

現状のまま Kubernetes を載せることは可能だが、以下のリスクを許容することになる。

- PVC への書き込みレイテンシが不安定になる（特に DB 系ワークロード）
- OSD の再起動時に数分間の stall が発生しうる
- 負荷が上がった際に `slow ops` が頻発し、Pod の I/O がブロックされる

**推奨する運用**: まずは軽量なワークロードから載せ、
Prometheus の `ceph_osd_op_r_latency` / `ceph_osd_op_w_latency` を監視しながら
段階的に負荷を上げること。レイテンシが恒常的に悪化するようであれば換装を検討する。

## 3. なぜ Rook ではなく ceph-csi を直接使うのか

[ADR-0004](adr/0004-ceph-csi.md) に詳述するが、要点は次の通り。

- Ceph クラスタの**運用主体は Proxmox 側**にある（mon/osd/mds は Proxmox が管理）。
  Rook は「Kubernetes で Ceph を運用する」ためのオペレータであり、外部 Ceph に
  対しては `external mode` という限定的な役割しか担わない。
- Rook external mode は結局のところ ceph-csi をデプロイするだけであり、
  間に CRD とオペレータという抽象が 1 層増える。障害時の切り分けが難しくなる。
- ceph-csi を直接使えば、Helm values がそのまま設定の全てになり、
  ArgoCD の差分がそのまま「何が変わるか」を意味する。

## 4. Kubernetes 用 Ceph ユーザー（最小権限）

`scripts/ceph-create-k8s-user.sh` が作成する。**`client.admin` は決して使わない。**

```bash
# RBD 用 — cephrdb_k8s プールのみ
ceph auth get-or-create client.k8s-rbd \
  mon 'profile rbd' \
  osd 'profile rbd pool=cephrdb_k8s' \
  mgr 'profile rbd pool=cephrdb_k8s'

# CephFS 用 — cephfs01 の /volumes/csi 配下のみ
ceph fs subvolumegroup create cephfs01 csi
ceph auth get-or-create client.k8s-cephfs \
  mon 'allow r fsname=cephfs01' \
  mds 'allow rw fsname=cephfs01 path=/volumes/csi' \
  osd 'allow rw tag cephfs data=cephfs01' \
  mgr 'allow rw'
```

### 権限分離の意図

| もし Kubernetes が侵害されたら | 結果 |
| --- | --- |
| `client.admin` を渡していた場合 | **全プール削除、Proxmox VM のディスク破壊、Ceph 設定改竄が可能** |
| 本構成（`k8s-rbd` / `k8s-cephfs`） | `cephrdb_k8s` と `cephfs01:/volumes/csi` 以外には一切触れない |

生成されたキーは `kubernetes/infra/ceph-csi/secret.sops.yaml` に
SOPS で暗号化して格納する。

## 5. StorageClass 設計

| StorageClass | プロビジョナ | アクセスモード | reclaim | 用途 |
| --- | --- | --- | --- | --- |
| `ceph-rbd`（**default**） | `rbd.csi.ceph.com` | RWO | Delete | 一般的なステートフルアプリ |
| `ceph-rbd-retain` | `rbd.csi.ceph.com` | RWO | **Retain** | DB 等、誤削除で消えては困るもの |
| `ceph-fs` | `cephfs.csi.ceph.com` | **RWX** | Delete | 複数 Pod で共有するファイル領域 |

共通設定:

- `volumeBindingMode: WaitForFirstConsumer`
  — Pod のスケジュール先が決まってからボリュームを作る。
- `allowVolumeExpansion: true`
  — オンラインでの拡張を許可する。
- `csi.storage.k8s.io/fstype: ext4`
  — RBD の既定。xfs も選べるが、縮小不可・メタデータ破損時の復旧性を考え ext4 とする。
- **`imageFeatures: layering`** のみを有効化
  — `exclusive-lock`, `object-map`, `fast-diff` はカーネル RBD クライアントの
    対応状況に依存するため、互換性を優先して layering に限定する。

### VolumeSnapshotClass

`ceph-rbd-snapshot`（`rbd.csi.ceph.com`）を定義し、Velero の CSI スナップショット
連携および snapshot-controller 経由の手動スナップショットに使う。

## 6. Talos 側で必要な設定

Talos はイミュータブル OS であり、任意のカーネルモジュールが常にロードされて
いるわけではない。ceph-csi（krbd）を使うため、machine config で明示する。

```yaml
machine:
  kernel:
    modules:
      - name: rbd        # RBD ブロックデバイス
      - name: libceph    # rbd の依存
```

また、ceph-csi の nodeplugin は `hostPID` / privileged / `mountPropagation:
Bidirectional` を要求するため、`ceph-csi` namespace のみ Pod Security を
`privileged` に緩和する（それ以外は `restricted` のまま）。

## 7. 容量計画

| 項目 | 値 |
| --- | --- |
| Ceph raw 容量 | 2.8 TiB |
| replica 3 での実効容量 | 約 933 GiB |
| 現在の使用量 | 592 GiB raw（約 197 GiB 実効） |
| VM ディスク（本構成で新規に消費） | CP 60 GiB × 3 + WK 120 GiB × 3 = 540 GiB（thin provision） |
| 残余 | 約 190 GiB 実効（PVC 用） |

> **注意**: `cephrdb_k8s` は既に 186 GiB を使用している。旧クラスタの残骸である
> 可能性が高い。`scripts/preflight.sh` が孤児 RBD イメージを一覧表示するので、
> 内容を確認した上で削除すること（自動削除はしない）。

`nearfull` の閾値（既定 85%）に達すると Ceph は書き込みを制限する。
Prometheus の `ceph_cluster_total_used_bytes` を監視し、80% でアラートを出す。

## 8. バックアップとの関係

| 対象 | 手段 | 保存先 |
| --- | --- | --- |
| PVC の中身 | Velero + CSI VolumeSnapshot → オブジェクトストア | MinIO（クラスタ内） or 外部 S3 |
| 定期スナップショット | snapscheduler | Ceph 内（同一障害ドメイン。**単独では不十分**） |
| VM 丸ごと | Proxmox Backup Server | 172.16.10.51（別筐体） |

Ceph 内のスナップショットは「操作ミスからの復旧」には有効だが、
**Ceph 自体の障害には無力**である。そのため PBS への VM バックアップと
Velero の外部保存を必ず併用する。
