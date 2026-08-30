# ADR-0004: 既存 Ceph の統合に ceph-csi を直接使う（Rook を挟まない）

- **状態**: ⛔ **Superseded**（[ADR-0009](0009-drop-ceph-adopt-longhorn.md) により置き換え）

> **この ADR はもう有効ではありません。**
> Ceph 自体を廃止したため、ここに書かれた判断は現在の構成には適用されません。
> 「当時どう考えたか」の記録として残しています。判断の前提が実測で
> 覆った経緯は [ADR-0009](0009-drop-ceph-adopt-longhorn.md) を参照してください。

- **当時の状態**: 承認済み
- **日付**: 2026-08-30

## 背景

Proxmox VE が管理する Ceph クラスタが既に稼働している
（mon 3 / osd 3 / mds 3、プール `cephrdb_k8s` は作成済み）。
これを Kubernetes の永続ストレージとして使う方法を決める必要がある。

## 検討した選択肢

### A. Rook（external cluster mode）

- ✅ CRD で宣言的に管理できる
- ✅ `CephBlockPool` / `CephFilesystem` 等の抽象が使える
- ❌ **外部 Ceph に対しては結局 ceph-csi をデプロイするだけ**。
  Rook のオペレータという抽象が 1 層増えるだけで、実際の制御は行わない
- ❌ 障害時の切り分けが「Rook のバグか、ceph-csi のバグか、Ceph 本体か」の
  3 択になる。層が増えるほど原因特定が遅れる
- ❌ external mode のセットアップに `create-external-cluster-resources.py` という
  Python スクリプトを Ceph 側で実行する手順が入り、IaC の外に手作業が漏れる

### B. ceph-csi を Helm chart で直接デプロイ ★採用

- ✅ Helm values が設定の全てであり、ArgoCD の差分がそのまま「何が変わるか」を意味する
- ✅ 障害の切り分けが「ceph-csi か Ceph 本体か」の 2 択で済む
- ✅ Ceph 側で必要なのは `ceph auth get-or-create` のみ（スクリプト 1 本で完結）
- ❌ `CephBlockPool` のような Kubernetes ネイティブな抽象は使えない
  → ただしプール管理は Proxmox 側の責務なので、むしろ正しい役割分担

### C. NFS（CephFS を NFS でエクスポート）

- ❌ 単一障害点（NFS ゲートウェイ）が増える
- ❌ ブロックデバイスとしての性能が出ない
- 却下

## 決定

**ceph-csi v3.17.1 を Helm chart で直接デプロイする。**

- `ceph-csi-rbd` chart → `ceph-rbd` / `ceph-rbd-retain` StorageClass
- `ceph-csi-cephfs` chart → `ceph-fs` StorageClass（RWX 用）

### 役割分担の明確化

| 責務 | 担当 |
| --- | --- |
| Ceph クラスタの構築・拡張・OSD 管理・プール作成 | **Proxmox VE**（既存） |
| Kubernetes からのボリューム動的プロビジョニング | **ceph-csi**（本リポジトリ） |
| Ceph の認証情報の発行 | `scripts/ceph-create-k8s-user.sh`（本リポジトリ） |

この分離により、「Kubernetes を作り直しても Ceph は無傷」
「Ceph を触っても Kubernetes のマニフェストは変わらない」という
独立性が保たれる。

## セキュリティ上の必須事項

**`client.admin` を Kubernetes に渡してはならない。**

多くのホームラボ記事は `client.admin` の keyring をそのまま Secret にしているが、
これは Kubernetes の侵害が即座に **Proxmox VM ディスク（`cephrdb_vm`）の破壊**に
つながることを意味する。本構成では用途別の最小権限ユーザーを作る
（[docs/30-storage-design.md](../30-storage-design.md) §4）。

## Talos 特有の考慮

| 項目 | 対応 |
| --- | --- |
| カーネルモジュール `rbd` | `machine.kernel.modules` で明示ロード |
| nodeplugin が privileged を要求 | `ceph-csi` namespace のみ PSA を `privileged` に緩和 |
| `mountPropagation: Bidirectional` | Talos の kubelet は既定でサポート |
| Ceph public network への到達 | ノードに VLAN20 の NIC を持たせる（[ADR-0007](0007-dual-nic-topology.md)） |
