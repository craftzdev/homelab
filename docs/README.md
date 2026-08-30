# ドキュメント索引

## 読む順序

初めて読む場合は、この順序を推奨します。

1. **[00-overview.md](00-overview.md)** — 何を作るのか、なぜ作るのか
2. **[90-decision-log.md](90-decision-log.md)** — どういう経緯でこの設計になったか
3. **[10-network-design.md](10-network-design.md)** — VLAN / IP の設計
4. **[20-security-design.md](20-security-design.md)** — 脅威モデルと多層防御
5. **[30-storage-design.md](30-storage-design.md)** — 既存 Ceph の統合
6. **[40-external-access.md](40-external-access.md)** — Cloudflare 経由の外部公開
7. **[50-operations.md](50-operations.md)** — 構築手順と日常の運用

## ADR（Architecture Decision Record）

個別の技術判断とその理由。**「なぜ他の選択肢を採らなかったか」**を重視して書いています。

| ADR | 決定 | 主な理由 |
| --- | --- | --- |
| [0001](adr/0001-talos-linux.md) | ノード OS に **Talos Linux** を使う | SSH もシェルも存在しない = ハードニングすべき対象が無い |
| [0002](adr/0002-opentofu.md) | **OpenTofu** でプロビジョニングする | 宣言的・plan による事前確認・冪等性が言語機能として得られる |
| [0003](adr/0003-cilium.md) | CNI に **Cilium** を使い kube-proxy を置換する | L7 ポリシー・Hubble による可視化・MetalLB が不要になる |
| [0004](adr/0004-ceph-csi.md) | **ceph-csi** を直接使う（Rook を挟まない） | Ceph の運用主体は Proxmox 側。抽象層を増やすと切り分けが困難になる |
| [0005](adr/0005-cloudflare-zero-trust.md) | **Cloudflare Tunnel + Access** で公開する | インバウンド開放ゼロ。認可を自作しない |
| [0006](adr/0006-argocd-sops.md) | **ArgoCD + SOPS/age** | 外部の秘密ストアに依存せず Git だけで完結する |
| [0007](adr/0007-dual-nic-topology.md) | ノードに **デュアル NIC**（VLAN40 + VLAN20） | ストレージ I/O を家庭用ルータ経由にしない |
| [0008](adr/0008-backup-strategy.md) | **3 階層バックアップ**（etcd / Velero / PBS） | 冗長化はバックアップではない。障害の種類ごとに手段を分ける |

## この構成で「やっていないこと」

セキュリティ設計において、**採用しなかった対策を明示すること**は
採用した対策を説明するのと同じくらい重要です。

- [20-security-design.md §4](20-security-design.md) — 採用しなかった対策と理由
- [90-decision-log.md](90-decision-log.md) — 意図的にやらなかったこと

## 未解決の課題

[90-decision-log.md の「この先の宿題」](90-decision-log.md) に一覧があります。
特に以下は優先度が高い状態です。

| # | 項目 | 状態 |
| --- | --- | --- |
| 1 | Ceph の `HEALTH_WARN` の原因究明 | **未着手**（構築前に必須） |
| 2 | Git 履歴に残る平文 SSH パスワードの除去 | **未着手**（公開前に必須） |
