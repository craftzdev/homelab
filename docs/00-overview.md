# 00. 全体像・ゴール・スコープ

## 1. 背景

自宅に以下の環境が既に稼働している。

- **Proxmox VE 9.0.11 クラスタ `homelab`**（3ノード）
  - `sv-proxmox-01` = 172.16.10.11 / 16 vCPU / 58 GiB RAM
  - `sv-proxmox-02` = 172.16.10.12 / 16 vCPU / 58 GiB RAM
  - `sv-proxmox-03` = 172.16.10.13 / 16 vCPU / 62 GiB RAM
- **ストレージ**: 各ノードに NVMe 512GB（Proxmox 本体）+ SATA SSD 1TB
  - 当初は SATA SSD を Ceph OSD として使っていたが、実測で性能不足が判明し
    **Ceph は廃止**（[ADR-0009](adr/0009-drop-ceph-adopt-longhorn.md)）。
    現在は単体 ZFS（`local-zfs`）として VM ディスクに使う
- **Proxmox Backup Server** = 172.16.10.51
- **ネットワーク**: NEC IX2215（L3）+ Aruba 1930、VLAN 10/20/30/40 を敷設済み
- 過去に kubeadm ベースの Kubernetes を構築した資産がリポジトリに存在したが、
  作業中に VM は全て削除された（実測で 0 台を確認）。クリーンな状態から構築する。

## 2. このプロジェクトのゴール

| # | ゴール | 達成条件 |
| --- | --- | --- |
| G1 | Proxmox 上に**セキュアな** Kubernetes クラスタを構築する | HA構成（control-plane 3台 + worker 3台 / etcdクォーラム3）で稼働し、CIS相当のハードニングが既定で効いている |
| G2 | 一部サービスを**外部の SaaS から安全に**呼び出せるようにする | Cloudflare Workers 上の SaaS からのみ到達でき、それ以外からは到達不能 |
| G3 | 永続ストレージを確保する | PVC が動的にプロビジョニングされ、1 ノード障害でデータを失わない |
| G4 | 構築は**常にコード**であり、リポジトリで管理される | 手作業の構築手順が実質ゼロ。クラスタを破棄しても同じものが再現できる |
| G5 | **経緯と意図がドキュメントに残る** | 主要な意思決定が ADR として記録され、代替案と却下理由が読み取れる |

## 3. スコープ

### スコープに含むもの

- Proxmox 上の Kubernetes ノード VM のプロビジョニング（OpenTofu）
- Talos Linux による Kubernetes クラスタ構築とハードニング
- CNI（Cilium）、ストレージ（Longhorn）、Ingress、証明書管理
- Cloudflare Zero Trust による外部公開経路と認可
- GitOps 基盤（ArgoCD）と秘密管理（SOPS + age）
- 可観測性（Prometheus / Grafana / Hubble）とバックアップ（etcd / Velero / PBS）
- Workers 側から Access 経由で叩くリファレンス実装

### スコープに含まないもの（現時点）

- IX2215 / Aruba 1930 のコンフィグ管理（既設のものを前提とする）
- Proxmox VE 自体のインストール（既設）
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
| ストレージ | **Longhorn v1.12.1** | Ceph を廃止し K8s 内で 3 レプリカを持つ。追加投資ゼロで冗長性を維持 | [ADR-0009](adr/0009-drop-ceph-adopt-longhorn.md) |
| スナップショット | **external-snapshotter v8.6.0** | VolumeSnapshot は Kubernetes のコア API ではなく CRD であり、別途導入が必要。Velero の CSI 連携の前提 | [ADR-0008](adr/0008-backup-strategy.md) |
| HTTP ルーティング | **Cilium Gateway API** | ingress-nginx が EOL のため移行。既に Cilium があるためコンポーネントが増えない | [ADR-0010](adr/0010-gateway-api.md) |
| 外部公開 | **Cloudflare Tunnel + Access** | インバウンド開放ゼロ。認可を Cloudflare エッジで完結でき、監査ログも残る | [ADR-0005](adr/0005-cloudflare-zero-trust.md) |
| GitOps | **ArgoCD v3.5.2** | 宣言的同期・差分可視化・自己修復。既存リポジトリの資産とも整合 | [ADR-0006](adr/0006-argocd-sops.md) |
| 秘密管理 | **SOPS v3.13 + age** | 外部の秘密ストアに依存せず、Git だけで完結する。ホームラボの規模に最適 | [ADR-0006](adr/0006-argocd-sops.md) |
| ノード構成 | **control-plane 3 VM + worker 3 VM** | 各Proxmoxホストに両役割を1台ずつ置き、AI実行負荷と永続データを制御系から隔離 | [ADR-0011](adr/0011-dedicated-worker-plane.md) |
| バックアップ | **etcd snapshot + Velero + PBS** | 3階層（クラスタ状態 / アプリ / VM）で復旧点を確保 | [ADR-0008](adr/0008-backup-strategy.md) |

## 6. 現状で判明している注意点

| # | 事象 | 影響 | 対応 |
| --- | --- | --- | --- |
| N1 | Ceph の OSD がコンシューマ SSD で性能不足（実測で判明） | → **Ceph を廃止**し Longhorn へ移行した（[ADR-0009](adr/0009-drop-ceph-adopt-longhorn.md)） | 解決済み |
| N2 | PBS は当初 172.16.10.10 と認識されていたが、実際は **172.16.10.51** | バックアップ連携先の指定ミス | IP を実測値に合わせて記載。`.10` への変更が必要なら別途対応 |
| N3 | Longhorn は Kubernetes の一部であり、クラスタ全損時に PV も失われる | Ceph 時代より外部バックアップの重要性が高い | Longhorn の S3 バックアップ + Velero + PBS を必ず設定する |
| N4 | Proxmox ホストは VLAN40 に IP を持たない | ホストから Kubernetes ノードへの L2 直通経路が無い | 管理は Talos API（VLAN40）経由。運用端末は Tailscale で VLAN40 に到達できることを確認済み |
