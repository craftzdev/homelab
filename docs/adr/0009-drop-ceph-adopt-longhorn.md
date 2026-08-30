# ADR-0009: Ceph を廃止し、PV を Longhorn へ移す

- **状態**: 承認済み
- **日付**: 2026-08-30
- **決定者**: クラフト
- **影響**: [ADR-0004](0004-ceph-csi.md) を **Superseded**、[ADR-0007](0007-dual-nic-topology.md) を **Superseded**

## 背景

[ADR-0004](0004-ceph-csi.md) では「既存の Proxmox Ceph をそのまま活かす」判断をし、
ceph-csi で Kubernetes へ統合する設計を作った。しかしその後の実測で、
前提が成り立たないことが分かった。

### 実測で判明した事実

| 観測 | 意味 |
| --- | --- |
| OSD のデバイスは `SUNEAST SE800 Lite SSD 1024GB` | コンシューマ向け SATA SSD。PLP なし・DRAM なしの可能性が高い |
| `Wear_Leveling_Count = 100`（摩耗は正常） | **壊れかけているのではなく、元々 Ceph の要求に性能が届いていない** |
| `bdev read stalled read ... timeout is 5.00s` | 読み取りが 5 秒のタイムアウトを超える。SSD コントローラの応答特性が原因 |
| TRIM 有効化後も警告が残る | Ceph 側の設定では解消しきれない |
| 1 物理ノードあたり OSD 1 本（計 3 本） | 1 本の劣化が全体に波及し、復旧の余地が小さい |
| Ceph 上の実データは **28 GiB**（ISO テンプレート 9 GB のみ） | RBD イメージはゼロ。移行対象が実質存在しない |

つまり、**Ceph を維持するために必要なのはエンタープライズ SSD への換装**
（3 本で 10〜20 万円規模）であり、その投資に見合う利用実態が無かった。

## 検討した選択肢

### A. Ceph を維持し、SSD を換装する

- ✅ 共有ストレージが維持され、VM のライブマイグレーションが可能
- ✅ ceph-csi の実装がそのまま使える
- ❌ **10〜20 万円の投資**が必要
- ❌ 3 OSD 構成では冗長性の余地が小さく、投資に見合わない
- ❌ Ceph の運用（mon/osd/mds の管理、PG の調整、アップグレード）が
  ホームラボの規模に対して重い

### B. Ceph を廃止し、Longhorn へ移す ★採用

- ✅ **追加投資ゼロ**。既存の SATA SSD をそのまま使う
- ✅ Proxmox 側の Ceph 運用から完全に解放される
- ✅ Kubernetes 内で完結し、GitOps で管理できる
  （StorageClass もレプリカ数もバックアップ設定も Git に入る）
- ✅ レプリカ 3 で冗長性は維持される
- ✅ Ceph より遥かに軽量。BlueStore/RocksDB の層が無いぶん、
  同じ SSD でも挙動が安定しやすい
- ✅ スナップショットと S3 バックアップが内蔵
- ❌ 共有ストレージではないため、VM のライブマイグレーションができなくなる
- ❌ Kubernetes が壊れると Longhorn も一緒に壊れる（Ceph は独立していた）

### C. Ceph を廃止し、TrueNAS の NFS を使う

- ✅ シンプル。RWX が使える
- ❌ **TrueNAS を新規に構築する必要がある**（現時点で存在しない）
- ❌ 単一障害点になる
- ❌ HDD ベースだと DB 系のワークロードに遅い
- → 将来 TrueNAS を増設したら、**共有ファイル用途に限って併用**する

### D. local-path-provisioner のみ

- ✅ 最もシンプル
- ❌ 冗長性がゼロ。ノード障害でデータが失われる
- ❌ Pod が特定ノードに固定される

## 決定

**Ceph を廃止し、Kubernetes の PV は Longhorn で確保する。**

| 層 | 変更前 | 変更後 |
| --- | --- | --- |
| VM ディスク | Ceph RBD（`cephrdb_k8s`） | 各ノードの **local-ZFS**（SATA SSD 1TB） |
| Kubernetes PV | ceph-csi（RBD / CephFS） | **Longhorn**（3 レプリカ） |
| ISO 置き場 | CephFS（`cephfs01`） | 各ノードの `local` |
| ノード構成 | control-plane 3 + worker 3（6 VM） | **control-plane 兼 worker 3 VM** |
| ノードの NIC | VLAN40 + VLAN20（Ceph public） | **VLAN40 のみ** |

### VM ディスクをローカルに置いてよい理由

共有ストレージを失うと VM はノードに固定される。通常これは可用性の低下だが、
**Talos ノードに限ってはほぼ問題にならない**。

Talos ノードはステートレスに近く、壊れたら `tofu apply` で作り直せばよい。
実際、[docs/50-operations.md](../50-operations.md) の復旧手順も
「ノード 1 台の障害 → 作り直すのが最も速い」としている。
ライブマイグレーションで延命する価値が小さい。

**作り直せないのは PV のデータだけ**であり、そこは Longhorn が
3 レプリカで保護する。守るべきものと守り方が対応している。

### 3 ノードへ集約した理由

Ceph という共有基盤が無くなったことで、「1 物理ノード = 1 Kubernetes ノード」
の方が障害ドメインが明確になる。

- etcd のクォーラムは 3 で成立（1 ノード障害に耐える）
- Longhorn も 3 レプリカなので、同じ障害ドメインに揃う
- control-plane と worker を分けても、物理ノードが 3 台しかない以上
  障害耐性は変わらない
- CPU（Ryzen 5700G）が先に不足するため、VM 数を増やすより
  1 VM あたりの割当を増やす方が効率的

`cluster.allowSchedulingOnControlPlanes: true` にしてワークロードを載せる。
「control-plane を分離する」原則より、この規模では
「ノードを遊ばせない」ことを優先した。

## Talos 側で必要になったこと

| 項目 | 内容 |
| --- | --- |
| system extension | `siderolabs/iscsi-tools`（Longhorn の iSCSI アタッチ）、`siderolabs/util-linux-tools`（fstrim） |
| データ領域 | VM に 2 本目のディスクを付け、`UserVolumeConfig` で `/var/mnt/longhorn` として切り出す |
| kubelet | `/var/mnt/longhorn` を `rshared` で bind mount |
| Pod Security | `longhorn-system` namespace のみ `privileged` |

**`/var/lib/longhorn`（既定）を使わない理由**: Talos の `/var` は EPHEMERAL
パーティション上にあり、再インストールや `talosctl reset` で初期化される。
そこにレプリカを置くと、ノードを 1 台作り直すたびに全レプリカの再同期が走る。
専用ディスクに分けることで、OS とデータのライフサイクルが分離される。

## 受け入れるトレードオフ

| 失うもの | 評価 |
| --- | --- |
| VM のライブマイグレーション | Talos ノードは作り直せるので実害は小さい。ただし将来 DB 等の singleton VM を建てる場合は、Proxmox Storage Replication か PBS からの復旧で対応する |
| ストレージ層の独立性 | Ceph は Kubernetes と独立していたが、Longhorn は Kubernetes の一部。**クラスタが壊れると PV へのアクセスも失う**。外部へのバックアップ（Longhorn の S3 バックアップ + Velero）が Ceph 時代より重要になる |
| RWX（複数 Pod からの同時書き込み） | Longhorn も RWX に対応するが NFS 経由で性能が落ちる。本格的に必要になったら TrueNAS を増設して NFS StorageClass を併用する |
| Ceph の運用知識 | 失うというより、**運用対象が減る**という利点として評価している |

## 将来 TrueNAS を増設した場合

TrueNAS は Longhorn を置き換えるものではなく、**用途を分ける**。

| 用途 | 置き場所 |
| --- | --- |
| DB・アプリの永続データ（RWO） | Longhorn |
| 共有ファイル・写真・動画（RWX） | TrueNAS の NFS |
| ISO・テンプレート | TrueNAS または各ノードの local |
| Longhorn / Velero のバックアップ先 | 外部 S3（R2 等）**または** TrueNAS 上の MinIO |

## この判断を見直すべき条件

- PV の総容量が SSD 1 本（1TB）の 1/3 を恒常的に超えるようになった
- Longhorn のレプリカ同期がノードのネットワークを飽和させるようになった
- 物理ノードを 4 台以上に増やした（Ceph の冗長性が意味を持ち始める）
