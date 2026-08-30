# 00. 全体像・ゴール・スコープ

## 1. 背景

自宅に以下の環境が既に稼働している。

- **Proxmox VE 9.0.11 クラスタ `homelab`**（3ノード）
  - `sv-proxmox-01` = 172.16.10.11 / 16 vCPU / 58 GiB RAM
  - `sv-proxmox-02` = 172.16.10.12 / 16 vCPU / 58 GiB RAM
  - `sv-proxmox-03` = 172.16.10.13 / 16 vCPU / 62 GiB RAM
- **Ceph（Proxmox 統合、Squid 世代）**
  - OSD 3本（各 SSD 約 1 TiB、合計 2.8 TiB / replica 3）
  - `public_network = 172.16.20.0/24`、`cluster_network = 172.16.30.0/24`
  - プール: `.mgr` / `cephfs01_data` / `cephfs01_metadata` / `cephrdb_vm` / `cephrdb_k8s`
  - CephFS: `cephfs01`（MDS 1 active + 2 standby）
- **Proxmox Backup Server** = 172.16.10.51
- **ネットワーク**: NEC IX2215（L3）+ Aruba 1930、VLAN 10/20/30/40 を敷設済み
- 過去に kubeadm ベースの Kubernetes を構築した資産がリポジトリに存在するが、
  現在 VM は全て停止しており実質的に未稼働。

## 2. このプロジェクトのゴール

| # | ゴール | 達成条件 |
| --- | --- | --- |
| G1 | Proxmox 上に**セキュアな** Kubernetes クラスタを構築する | HA 構成（CP 3台）で稼働し、CIS 相当のハードニングが既定で効いている |
| G2 | 一部サービスを**外部の SaaS から安全に**呼び出せるようにする | Cloudflare Workers 上の SaaS からのみ到達でき、それ以外からは到達不能 |
| G3 | 既存の **Ceph を活用**する | PVC が Ceph RBD / CephFS で動的にプロビジョニングされる |
| G4 | 構築は**常にコード**であり、リポジトリで管理される | 手作業の構築手順が実質ゼロ。クラスタを破棄しても同じものが再現できる |
| G5 | **経緯と意図がドキュメントに残る** | 主要な意思決定が ADR として記録され、代替案と却下理由が読み取れる |

## 3. スコープ

### スコープに含むもの

- Proxmox 上の Kubernetes ノード VM のプロビジョニング（OpenTofu）
- Talos Linux による Kubernetes クラスタ構築とハードニング
- CNI（Cilium）、CSI（ceph-csi）、Ingress、証明書管理
- Cloudflare Zero Trust による外部公開経路と認可
- GitOps 基盤（ArgoCD）と秘密管理（SOPS + age）
- 可観測性（Prometheus / Grafana / Hubble）とバックアップ（etcd / Velero / PBS）
- Workers 側から Access 経由で叩くリファレンス実装

### スコープに含まないもの（現時点）

- IX2215 / Aruba 1930 のコンフィグ管理（既設のものを前提とする）
- Proxmox VE 自体のインストールと Ceph クラスタの構築（既設）
- SaaS アプリケーション本体の実装（`workers/example-origin-api` はあくまで疎通の雛形）
- マルチクラスタ / DR サイト

## 4. 設計原則

このリポジトリの全ての判断は、以下の原則の優先順位に従う。

1. **攻撃対象領域を減らすことが、防御を足すことに優先する**
   （SSH を堅牢にするより、SSH が存在しない OS を選ぶ）
2. **境界での遮断より、多層防御を前提とする**
   （Cloudflare Access を通っても、Origin 側でもう一度 JWT を検証する）
3. **宣言的であることが、便利であることに優先する**
   （その場しのぎの手動コマンドは、再現性を壊すので採用しない）
4. **既定値は最も安全な側に倒す**
   （NetworkPolicy は default-deny、Pod Security は restricted から始める）
5. **秘密は Git に置くが、平文では置かない**
   （SOPS で暗号化することで、履歴・レビュー・災害復旧の全てを両立させる）

## 5. 採用技術スタック（要約）

| レイヤ | 採用 | 主な理由 | 詳細 |
| --- | --- | --- | --- |
| ノード OS | **Talos Linux v1.13.9** | SSH / シェル / パッケージ管理が存在せず、攻撃対象領域が極小。設定が全て API + YAML | [ADR-0001](adr/0001-talos-linux.md) |
| VM プロビジョニング | **OpenTofu + bpg/proxmox** | 宣言的・冪等。Proxmox API を直接扱えシェルスクリプトを排除できる | [ADR-0002](adr/0002-opentofu.md) |
| クラスタ構築 | **siderolabs/talos provider** | machine config 生成〜bootstrap〜kubeconfig 取得までコード化できる | [ADR-0002](adr/0002-opentofu.md) |
| CNI | **Cilium v1.20.1**（kube-proxy 置換） | eBPF による高性能・L3-L7 ポリシー・Hubble による可視化・L2 Announcement で MetalLB 不要 | [ADR-0003](adr/0003-cilium.md) |
| ストレージ | **ceph-csi v3.17.1**（外部 Ceph） | 既存 Proxmox Ceph をそのまま利用。Rook を挟まず運用主体を Proxmox 側に一本化 | [ADR-0004](adr/0004-ceph-csi.md) |
| 外部公開 | **Cloudflare Tunnel + Access** | インバウンド開放ゼロ。認可を Cloudflare エッジで完結でき、監査ログも残る | [ADR-0005](adr/0005-cloudflare-zero-trust.md) |
| GitOps | **ArgoCD v3.5.2** | 宣言的同期・差分可視化・自己修復。既存リポジトリの資産とも整合 | [ADR-0006](adr/0006-argocd-sops.md) |
| 秘密管理 | **SOPS v3.13 + age** | 外部の秘密ストアに依存せず、Git だけで完結する。ホームラボの規模に最適 | [ADR-0006](adr/0006-argocd-sops.md) |
| ノード NIC 構成 | **デュアル NIC（VLAN40 + VLAN20）** | Ceph トラフィックを L3 ルータ経由にせず、10GbE の L2 内で完結させる | [ADR-0007](adr/0007-dual-nic-topology.md) |
| バックアップ | **etcd snapshot + Velero + PBS** | 3階層（クラスタ状態 / アプリ / VM）で復旧点を確保 | [ADR-0008](adr/0008-backup-strategy.md) |

## 6. 現状で判明している注意点

| # | 事象 | 影響 | 対応 |
| --- | --- | --- | --- |
| N1 | Ceph が `HEALTH_WARN`（`3 OSD(s) experiencing slow operations in BlueStore`, `2 OSD(s) experiencing stalled read in db device of BlueFS`） | Kubernetes の PV 性能・安定性に直結する | 構築前に `scripts/preflight.sh` で検出し、原因調査を必須とする（[docs/30-storage-design.md](30-storage-design.md) 参照） |
| N2 | PBS は当初 172.16.10.10 と認識されていたが、実際は **172.16.10.51** | バックアップ連携先の指定ミス | IP を実測値に合わせて記載。`.10` への変更が必要なら別途対応 |
| N3 | 各ノードの OSD は 1本のみ | 1 OSD 障害でクラスタが degraded になり、復旧の余地が小さい | replica 3 / min_size 2 を維持し、重要データは Velero + PBS で別媒体へ |
| N4 | Proxmox ホストは VLAN40 に IP を持たない | ホストから Kubernetes ノードへの L2 直通経路が無い | 管理は Talos API（VLAN40）経由。運用端末は Tailscale で VLAN40 に到達できることを確認済み |
