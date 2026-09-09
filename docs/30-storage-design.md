# 30. ストレージ設計

> **2026-08-30 に方針転換しました。** 当初は既存の Proxmox Ceph を
> ceph-csi で統合する設計でしたが、実測の結果その前提が崩れたため
> **Ceph を廃止し Longhorn へ移行**しました。
> 経緯は [ADR-0009](adr/0009-drop-ceph-adopt-longhorn.md)、
> 旧設計は [ADR-0004](adr/0004-ceph-csi.md)（Superseded）を参照してください。

## 1. 全体像

```
┌─ Proxmox 物理ノード ×3 ────────────────────────────────┐
│                                                        │
│  NVMe 512GB ─── Proxmox VE 本体（システム）             │
│                                                        │
│  SATA SSD 1TB ─ local-zfs                              │
│                  ├─ k8s-N の OS ディスク    (60 GiB)    │
│                  └─ k8s-N の Longhorn 用   (300 GiB)   │
│                                                        │
└────────────────────────────────────────────────────────┘
              │ 1 物理ノード = 1 Kubernetes ノード
              ▼
┌─ Kubernetes (Talos) ───────────────────────────────────┐
│  Longhorn: 3 レプリカ / /var/mnt/longhorn              │
│    StorageClass: longhorn(既定) / -retain / -single     │
└────────────────────────────────────────────────────────┘
```

| 層 | 実装 | 冗長性 |
| --- | --- | --- |
| Proxmox 本体 | NVMe 512GB（単体） | なし。壊れたら Proxmox を再インストール |
| VM ディスク | 各ノードの `local-zfs`（単体 ZFS） | なし。**Talos ノードは作り直せるので不要** |
| Kubernetes PV | **Longhorn（3 レプリカ）** | あり。1 ノード障害に耐える |
| バックアップ | Longhorn → 外部 S3 / Velero / PBS | 別媒体 |

## 2. なぜ Ceph をやめたのか

### 実測で分かったこと

```
# OSD のバッキングデバイス
sda  953.9G  SUNEAST SE800 Lite SSD 1024GB   ← コンシューマ向け SATA SSD

# SMART
Power_On_Hours          29201    （約 3.3 年）
Wear_Leveling_Count     100      （摩耗は正常）

# OSD のログ
bdev read stalled read 0xbb8de6a000~3000 (buffered) since 35252.30s, timeout is 5.00s

# 実際に載っていたデータ
cephrdb_k8s   27 KiB (RBD イメージ 0 個)
cephrdb_vm    5.5 KiB (0 個)
cephfs01      9.3 GiB (ISO テンプレートのみ)
```

**摩耗指標は正常**でした。つまり「壊れかけている」のではなく、
**元々 Ceph の要求に対して性能が届いていない**という状態です。

PLP（電源断保護）を持たないコンシューマ SSD は、Ceph が発行する同期書き込みごとに
実フラッシュ書き込みが発生してレイテンシが跳ねます。また "Lite" 型番は DRAM
キャッシュを省いていることが多く、FTL のアドレス変換テーブルを NAND から
都度読むため、ランダム読み取りが数秒スパイクします。

`DB_DEVICE_STALLED_READ_ALERT` は Ceph 19.2 (Squid) で追加された、
まさにこの種のデバイスを検出するための警告です。

### 試したこと（と、その限界）

TRIM を有効化して OSD を 1 台ずつ再起動しました。

```bash
ceph config set osd bdev_enable_discard true
ceph config set osd bdev_async_discard_threads 1
```

警告は **3 OSD → 1 OSD** に減りましたが、これは根本解決ではありません。
TRIM は書き込みには効きますが、stalled *read* は SSD コントローラの
応答特性に起因するため Ceph 側の設定では解消できないためです。

### 結論

Ceph を維持するには **PLP 付きエンタープライズ SSD への換装（3本で 10〜20 万円）**が
必要でした。一方、実際に載っていたデータは ISO テンプレート 9GB のみ。
**投資に見合う利用実態がありませんでした。**

## 3. Longhorn の構成

### レプリカと配置

| 設定 | 値 | 理由 |
| --- | --- | --- |
| `defaultReplicaCount` | 3 | ノード数と同じ。1 ノード障害でデータを失わない |
| `replicaSoftAntiAffinity` | **false** | レプリカを必ず別ノードへ。true だと同一ノードに複数レプリカが載りうる |
| `defaultDataPath` | `/var/mnt/longhorn` | 既定の `/var/lib/longhorn` は Talos の EPHEMERAL 上にあり、再インストールで消える |

### StorageClass

| StorageClass | レプリカ | reclaim | 用途 |
| --- | --- | --- | --- |
| `longhorn`（**既定**） | 3 | Delete | 一般的なステートフルアプリ |
| `longhorn-retain` | 3 | **Retain** | DB 等、誤削除で消えては困るもの |
| `longhorn-single` | **1** | Delete | キャッシュ等、失っても作り直せるもの。容量を 1/3 に節約できる |

> `longhorn-single` を用意した理由: 3 レプリカは容量を 3 倍消費します。
> 「失っても再生成できるデータ」にまで冗長性を持たせるのは無駄です。
> 用途に応じて選べるようにしています。

## 4. Talos 側で必要な設定

Longhorn は Talos の標準構成では動きません。以下が必要です。

| 項目 | 設定 | これが無いとどうなるか |
| --- | --- | --- |
| system extension | `siderolabs/iscsi-tools` | Longhorn が PV を iSCSI でアタッチできず、Pod が永久に `ContainerCreating` |
| system extension | `siderolabs/util-linux-tools` | `fstrim` が無く、削除済みブロックが解放されない |
| データ領域 | `UserVolumeConfig` で 2 本目のディスクを `/var/mnt/longhorn` へ | ノードを作り直すたびに全レプリカが再同期される |
| kubelet | `extraMounts` で `/var/mnt/longhorn` を **rshared** bind mount | Longhorn が作ったマウントが kubelet へ伝播せず、Pod がボリュームを見つけられない |
| Pod Security | `longhorn-system` のみ `privileged` | Longhorn の Pod が起動できない |

いずれも `tofu/10-proxmox-talos` と `talos/patches/` で設定済みです。
`talosctl validate --strict` が通ることを確認しています。

### なぜ専用ディスクを切り出すのか

Talos の `/var` は **EPHEMERAL パーティション**上にあり、
再インストールや `talosctl reset` で初期化されます。

そこに Longhorn のレプリカを置くと、ノードを 1 台作り直すたびに
数百 GB の再同期が走ります。VM に 2 本目のディスクを付けて
`UserVolumeConfig` で切り出すことで、**OS のライフサイクルと
データのライフサイクルが分離**されます。

## 5. ⚠️ バックアップの重要性が上がった

Ceph は Kubernetes と独立した基盤でした。Kubernetes が壊れても
Ceph 上のデータは無事でした。

**Longhorn は Kubernetes の一部です。** クラスタが壊れれば
PV へのアクセスも同時に失われます。

したがって**外部へのバックアップが Ceph 時代より重要**になります。

| 手段 | 対象 | 保存先 | 状態 |
| --- | --- | --- | --- |
| Longhorn バックアップ | ボリューム単位 | **外部 S3（R2 等）** | ⚠️ 未設定 |
| Velero | namespace + PVC | 外部 S3 | ⚠️ 未設定 |
| PBS | VM 丸ごと | 172.16.10.51 | 要設定 |

> Longhorn の **スナップショット**（`type: snap`）は同じディスク上にしか
> 存在しません。ディスクやノードが壊れれば一緒に失われます。
> 災害復旧には **バックアップ**（`type: bak`、外部オブジェクトストレージへ転送）が
> 必要です。設定手順は
> [kubernetes/infra/longhorn/README.md](../kubernetes/infra/longhorn/README.md) を参照。

## 6. 容量計画

| 項目 | 値 |
| --- | --- |
| SATA SSD | 1 TB × 3 ノード |
| VM の OS ディスク | 60 GiB × 3 = 180 GiB |
| Longhorn 用ディスク | 300 GiB × 3 = 900 GiB |
| ノードあたりの使用 | 360 GiB / 1 TB（余裕あり） |
| **Longhorn の実効容量**（3 レプリカ） | **約 300 GiB** |

3 レプリカなので、900 GiB の raw 容量に対して実際に使えるのは約 300 GiB です。
`longhorn-single`（レプリカ 1）を使えばその分は 1:1 で使えます。

Prometheus の `longhorn_node_storage_usage_bytes` を監視し、
80% でアラートを出します。

## 7. 将来 TrueNAS を増設した場合

TrueNAS は Longhorn を**置き換えるものではなく、用途を分けます**。

| 用途 | 置き場所 | 理由 |
| --- | --- | --- |
| DB・アプリの永続データ（RWO） | **Longhorn** | ブロックストレージで低レイテンシ |
| 共有ファイル・写真・動画（RWX） | **TrueNAS の NFS** | 複数 Pod からの同時読み書き。HDD で容量単価が安い |
| ISO・テンプレート | TrueNAS または各ノードの `local` | |
| Longhorn / Velero のバックアップ先 | 外部 S3 **または** TrueNAS 上の MinIO | Kubernetes の外にあることが重要 |

NFS StorageClass を追加する場合は `csi-driver-nfs` を導入し、
`kubernetes/infra/` に同じ構成で追加してください。

## 8. この設計を見直すべき条件

- PV の総容量が Longhorn の実効容量（約 300 GiB）を恒常的に超える
- Longhorn のレプリカ同期がノード間ネットワークを飽和させる
- 物理ノードを 4 台以上に増やす（Ceph の冗長性が意味を持ち始める）
- エンタープライズ SSD へ換装する予算がついた
