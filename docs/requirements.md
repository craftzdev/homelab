やったね！では、いただいたリポジトリを軸に、あなたのネットワーク設計（VLAN10/20/30/40）とCeph前提で動く\*\*“Proxmox上にKubernetesクラスタを構築するための計画書”\*\*をまとめました。
（要所は公式・元リポジトリの根拠付きで示します）

---

# 構築計画書：Proxmox クラスタ上に Kubernetes クラスタ

## 0. 目的とゴール

* Proxmox クラスタ上に **HA 構成の Kubernetes（kubeadmベース）** を短時間で展開／破棄できる環境を用意する。展開自動化はリポジトリ `unchama/kube-cluster-on-proxmox` の `deploy-vm.sh` を利用（後述）([GitHub][1])
* CNI は **Cilium**、LB は **MetalLB(L2)**、CD は **ArgoCD** を導入（リポジトリの既定）([GitHub][1])
* ストレージは **既存Ceph** を **Ceph-CSI(RBD)** で K8s に統合（Synology CSI の代替）([Ceph Documentation][2])

---

## 1. 既存・前提（あなたの環境に合わせて確定）

* 物理：Proxmox 3台（Deskmeet X300 / Ryzen 5700G / 128GB RAM / NVMe 512GB + SATA SSD 1TB）
* ネットワーク：

  * VLAN10=管理（172.16.10.0/24）
  * VLAN20=Ceph Public（172.16.20.0/24）
  * VLAN30=Ceph Cluster（172.16.30.0/24）
  * VLAN40=VM/K8s（172.16.40.0/24）
* IX2215 と Aruba1930 の VLAN は既設（Port1=厳格トランク / PVE10GbE=Tagged20,30,40 / PVE1GbE=Untagged10）

**Proxmox 側の前提（リポジトリ要件）**

* Proxmox **クラスタ済み**で、**root相互SSH**が動作していること（deploy-vm.sh が前提にするため）([GitHub][1])
* **cloud-init テンプレート**利用（Ubuntu 22.04 LTS cloud image）([GitHub][1])
* **スニペット保管用の共有ストレージ**（Proxmoxの“snippet”タイプ）と **VM Disk 用の共有ストレージ**があること（Dir/NFSやCephFS/共有ディレクトリ等で満たす）([GitHub][1])
* Proxmox の Cloud-Init ベストプラクティス（SSH鍵推奨）に従う([Proxmox VE][3])

---

## 2. 論理設計（IP/ネーミング/役割）

### 2.1 Kubernetes ノード（VM）命名とIP

| 役割            | ホスト名（例）       |  VLAN40 Node IP | 備考             |
| ------------- | ------------- | --------------: | -------------- |
| Control Plane | `sv-k8s-cp-1` | 172.16.40.11/24 | 3台（11,12,13）推奨 |
| 〃             | `sv-k8s-cp-2` | 172.16.40.12/24 |                |
| 〃             | `sv-k8s-cp-3` | 172.16.40.13/24 |                |
| Worker        | `sv-k8s-wk-1` | 172.16.40.21/24 | 必要に応じて増減       |
| 〃             | `sv-k8s-wk-2` | 172.16.40.22/24 |                |
| 〃             | `sv-k8s-wk-3` | 172.16.40.23/24 |                |

* **デフォゲートウェイ**：172.16.40.1（IX2215）
* **API VIP（HAProxy/Keepalived）**：172.16.40.100（リポジトリではAPIエンドポイントをVIP化する方針）([GitHub][1])
* **MetalLB プール（L2）**：172.16.40.200–172.16.40.250（ノードと同一サブネットに置くのが要件）([metallb.universe.tf][4])

> メモ：MetalLB L2 モードは、同一 L2 セグメントで ARP/NDP 広告により VIP を提供。特別なプロトコル設定は不要で、プールの確保が主タスクです。([metallb.universe.tf][4])

### 2.2 Ceph との接続方針

* **K8sノードは VLAN40 のままでOK**（IX2215がL3で20/30と疎通）。
* RBD/CSI 用にノード→Ceph Public(172.16.20.0/24) へ到達可能ならよい（同一VLAN20に直NICを足す必要はない）。
* ストレージクラスは **Ceph-CSI(RBD)** を採用（公式推奨）([Ceph Documentation][2])

---

## 3. リソース設計（VMスペック目安）

* **Control Plane VM**：vCPU 4 / RAM 8–12GB / Disk 40–60GB
* **Worker VM**：vCPU 4–8 / RAM 8–16GB / Disk 60–100GB
* Pod CIDR/Service CIDR はリポジトリのデフォ（例：10.128.0.0/16, 10.96.0.0/16）でOK ([GitHub][1])
* kubeadm の最小要件・手順は公式に準拠（後述）([Kubernetes][5])

---

## 4. 展開フロー（大枠）

### 4.1 Proxmox 側の事前準備

1. **Cloud-Init テンプレート**作成（Ubuntu 22.04 LTS cloud image）

   * SSH鍵認証でテンプレ化（Proxmox Cloud-Init ドキュメント）([Proxmox VE][3])
2. **共有ストレージ**

   * `snippet` 格納可能な共有Dir/NFS等と、VMディスク用共有（Ceph/RBD でも可）を用意（リポジトリ前提事項）([GitHub][1])
3. **Proxmox クラスタのSSH**（root間）動作確認（必須）([GitHub][1])

### 4.2 自動展開（リポジトリのスクリプト）

* Proxmox ホストのコンソールで以下（ブランチは `main` など環境に合わせる）：

```bash
export TARGET_BRANCH=main
/bin/bash <(curl -s https://raw.githubusercontent.com/unchama/kube-cluster-on-proxmox/${TARGET_BRANCH}/deploy-vm.sh) ${TARGET_BRANCH}
```

`deploy-vm.sh` が VM作成→cloud-config注入→起動→各VMで `scripts/k8s-node-setup.sh` を実行し、
**kubeadm / keepalived / haproxy / Cilium / ArgoCD / MetalLB など**を導入する流れです。([GitHub][1])

> 参考：kubeadm フロー（control-plane 初期化～join）は公式手順に沿う思想です。([Kubernetes][5])

### 4.3 完了確認と初期アクセス

* ノード・Podの状態確認（READMEの例コマンド参照）
* ArgoCD への初回ログイン手順（初期PWの取得→SSHポートフォワード）も README に手順あり。([GitHub][1])

---

## 5. Ceph-CSI（RBD）切替プラン

※リポジトリは Synology CSI を例示しているので、ここだけ **Ceph-CSI** に差し替え

1. **Ceph 側準備**（Mon が待ち受ける Public=172.16.20.0/24）

   * k8s 用 Pool（例 `k8s-rbd`）作成＆初期化
   * k8s 用の認証ユーザー作成（`client.k8s` 等）
   * 公式ガイドに準拠（RBDをK8sで使うには ceph-csi を用いる）([Ceph Documentation][2])
2. **K8s 側**

   * `ceph/ceph-csi` のマニフェスト or Helm で **rbd-csi** をデプロイ([GitHub][6])
   * Cephの `mon_host` / `pool` / `user` / `key` を Secret に登録
   * `StorageClass`（`provisioner: rbd.csi.ceph.com`）を作成
3. **動作確認**：`ReadWriteOnce` の PVC を作り、Pod が RBD をアタッチできること

> 注意：`LoadBalancer` を使うアプリは **MetalLB(L2)** のプールからVIPが払い出されます。**プールはノードと同一サブネット**に置く必要があります（本設計では VLAN40）。([metallb.universe.tf][4])

---

## 6. ネットワーク要点（PVE/VM側）

* **VM NIC**

  * Bridge=`vmbr1`（VLAN-aware） / **VLAN Tag=40**（K8sノードの“ユーザ面”）
* **Pod/Service CIDR** は kubeadm 既定（またはリポジトリ指定）でOK。
* **Cilium** はリポジトリで Helm インストール（`k8s-node-setup.sh` で導入）([GitHub][1])
* **MetalLB(L2)** は ArgoCD 経由で導入されるため、`IPAddressPool` に `172.16.40.200-250` を設定（L2広告の `L2Advertisement` を紐づける）([metallb.universe.tf][4])

---

## 7. 運用：観測・CD・アップグレード

* **metrics-server** はリポジトリで導入済み。ダッシュボードや kube-prometheus-stack を ArgoCD 管理に乗せるのも可。([GitHub][1])
* **ArgoCD** による GitOps（アプリ配備の継続運用）([GitHub][1])
* **kubeadm** のバージョンアップは公式手順に従う（ControlPlane→Worker順）。([Kubernetes][5])

---

## 8. 受け入れ基準（完成の判定）

1. `kubectl get nodes -o wide` で全ノード Ready（CP×3、Worker×N）([GitHub][1])
2. `LoadBalancer` サービスに **MetalLB** がIPを割り当て、VLAN40内クライアントから到達できる（L2モード要件クリア）([metallb.universe.tf][4])
3. `PersistentVolumeClaim`（RBD/CSI）でPodが正常にPVCをBound→マウント（Ceph-CSI要件クリア）([Ceph Documentation][2])
4. ArgoCDにログインでき、サンプルアプリを同期・デプロイできる（README手順）([GitHub][1])

---

## 9. ロールバック／破棄

* 本リポジトリは「**サクッと作ってサクッと壊す**」思想。VM を削除し、必要なら MetalLB のIP確保範囲を開放、ArgoCDのアプリもクリアする。必要に応じて `known_hosts` 再生成の手順も README にあり。([GitHub][1])

---

## 10. 参考（根拠）

* 公式 kubeadm（初期化/設計/運用）([Kubernetes][5])
* 公式 MetalLB（L2モードとアドレスプールの要件）([metallb.universe.tf][4])
* 公式 Ceph（RBD を K8s で使うなら ceph-csi）([Ceph Documentation][2])
* Ceph-CSI リポジトリ（デプロイ/構成）([GitHub][6])
* Proxmox Cloud-Init（鍵認証推奨）([Proxmox VE][3])
* 元リポジトリ：構成要素（Cilium/ArgoCD/MetalLB）・API VIP(HAProxy+Keepalived)・自動化の流れ、前提（クラスタ済みPVE/共有snippet/storage）([GitHub][1])

---

### このあと一緒にやる作業（推奨の具体ステップ）

1. Proxmox の **snippet/共有ディスク** 準備と Cloud-Init テンプレの確認
2. `TARGET_BRANCH` を `main` にして **deploy-vm.sh** を実行（PVEノードのrootコンソール）([GitHub][1])
3. 完了後、**MetalLB の IPAddressPool** を VLAN40 の `172.16.40.200-250` に調整
4. **Ceph-CSI(RBD)** を投入（Pool/Secret/StorageClass）
5. サンプル `LoadBalancer` と `PVC` を作って受け入れ確認

必要なら、**MetalLB のサンプル YAML** と **Ceph-CSI(RBD) の StorageClass/Secret テンプレ**を、あなたのIPとPool名に合わせてそのまま貼れる形で作ります。

[1]: https://github.com/unchama/kube-cluster-on-proxmox "GitHub - unchama/kube-cluster-on-proxmox: Proxmox環境でサクッと作ってサクっと壊せる高可用性なkubernetesクラスタを作ってみる"
[2]: https://docs.ceph.com/en/latest/rbd/rbd-kubernetes/?utm_source=chatgpt.com "Block Devices and Kubernetes - Ceph Documentation"
[3]: https://pve.proxmox.com/wiki/Cloud-Init_Support?utm_source=chatgpt.com "Cloud-Init Support"
[4]: https://metallb.universe.tf/configuration/?utm_source=chatgpt.com "Configuration :: MetalLB, bare metal load-balancer for ..."
[5]: https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/create-cluster-kubeadm/?utm_source=chatgpt.com "Creating a cluster with kubeadm"
[6]: https://github.com/ceph/ceph-csi?utm_source=chatgpt.com "CSI driver for Ceph"
