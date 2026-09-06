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
| ~~[0004](adr/0004-ceph-csi.md)~~ | ~~ceph-csi を直接使う~~ | ⛔ **Superseded** — ADR-0009 により置き換え |
| [0005](adr/0005-cloudflare-zero-trust.md) | **Cloudflare Tunnel + Access** で公開する | インバウンド開放ゼロ。認可を自作しない |
| [0006](adr/0006-argocd-sops.md) | **ArgoCD + SOPS/age** | 外部の秘密ストアに依存せず Git だけで完結する |
| ~~[0007](adr/0007-dual-nic-topology.md)~~ | ~~デュアル NIC（VLAN40 + VLAN20）~~ | ⛔ **Superseded** — ADR-0009 により置き換え |
| [0008](adr/0008-backup-strategy.md) | **3 階層バックアップ**（etcd / Velero / PBS） | 冗長化はバックアップではない。障害の種類ごとに手段を分ける |
| [0009](adr/0009-drop-ceph-adopt-longhorn.md) | **Ceph を廃止し Longhorn へ** | 実測で SSD の性能不足が判明。維持には 10〜20 万円の換装が必要で、利用実態に見合わなかった |
| [0010](adr/0010-gateway-api.md) | **ingress-nginx をやめ Cilium Gateway API へ** | ingress-nginx は 2026年3月に EOL。外部公開の入口に修正されないコンポーネントは置けない |
| [0011](adr/0011-dedicated-worker-plane.md) | **control-plane 3台 + worker 3台へ分離** | AI実行負荷とLonghornデータを制御系から隔離し、Talos APIで冪等に管理する |

## コンポーネント別の手順書

一部のコンポーネントは、構築時に個別の準備が必要です。

| ドキュメント | 内容 |
| --- | --- |
| [kubernetes/infra/monitoring/README.md](../kubernetes/infra/monitoring/README.md) | Grafana 管理者パスワードの設定（**必須**。未設定だと Grafana が起動しない） |
| [kubernetes/infra/longhorn/README.md](../kubernetes/infra/longhorn/README.md) | Longhorn のバックアップ先の設定（**重要**）と Talos 側の前提確認 |
| [kubernetes/infra/velero/README.md](../kubernetes/infra/velero/README.md) | バックアップ先（外部オブジェクトストレージ）の設定 |
| [kubernetes/infra/cloudflared/generated/README.md](../kubernetes/infra/cloudflared/generated/README.md) | OpenTofu が生成する ingress 設定の扱い |
| [workers/example-origin-api/README.md](../workers/example-origin-api/README.md) | Workers から Access 経由で呼び出す実装 |

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
| 1 | Longhorn のバックアップ先（外部 S3）の設定 | **未着手**（本番データを載せる前に必須） |
| 2 | Git 履歴に残る平文 SSH パスワードの除去 | **未着手**（公開前に必須） |
