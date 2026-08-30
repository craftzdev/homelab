# homelab — Proxmox VE + Ceph 上に構築するセキュアな Kubernetes 基盤

自宅の Proxmox VE クラスタ（3ノード / Ceph 統合済み）の上に、**Talos Linux による
イミュータブルな Kubernetes クラスタ**を構築し、その一部サービスを
**Cloudflare Zero Trust（Tunnel + Access）経由で Cloudflare Workers 上の SaaS から
のみ安全に呼び出せる**ようにするための、Infrastructure as Code リポジトリです。

インバウンドのポート開放は **ゼロ** です。自宅のグローバル IP は一切公開しません。

---

## 何を作るのか（1枚で）

```
                    ┌──────────────────────────────────────────┐
   インターネット    │        Cloudflare Global Network         │
                    │                                          │
  ┌──────────┐      │  ┌────────────┐      ┌───────────────┐   │
  │  SaaS    │─────▶│  │   Access   │─────▶│    Tunnel     │   │
  │ (Workers)│ mTLS │  │ (ServiceTk)│ JWT  │  (cloudflared)│   │
  └──────────┘      │  └────────────┘      └───────┬───────┘   │
                    └──────────────────────────────┼───────────┘
                                                   │ outbound only (QUIC/443)
  ═══════════════════════════════════════════════ │ ═══════════ 自宅 NW 境界
                                                   ▼
                              ┌─────────────────────────────────────┐
                              │  Kubernetes (Talos Linux) VLAN40    │
                              │  cloudflared × 2 → Ingress → App    │
                              │  CNI: Cilium (eBPF, default-deny)   │
                              └───────────────┬─────────────────────┘
                                              │ VLAN20 (Ceph public)
                              ┌───────────────▼─────────────────────┐
                              │  Proxmox VE 3ノード + Ceph (3 OSD)  │
                              │  ceph-csi: RBD (Block) / CephFS(RWX)│
                              └─────────────────────────────────────┘
```

---

## ドキュメント

**設計の「なぜ」を知りたい場合は必ず [`docs/`](docs/) を読んでください。**

| ドキュメント | 内容 |
| --- | --- |
| [docs/00-overview.md](docs/00-overview.md) | 全体像・ゴール・スコープ・前提 |
| [docs/10-network-design.md](docs/10-network-design.md) | VLAN / IP アドレス設計 |
| [docs/20-security-design.md](docs/20-security-design.md) | 脅威モデルと多層防御の設計 |
| [docs/30-storage-design.md](docs/30-storage-design.md) | 既存 Ceph の Kubernetes 統合 |
| [docs/40-external-access.md](docs/40-external-access.md) | Cloudflare Tunnel + Access による外部公開 |
| [docs/50-operations.md](docs/50-operations.md) | 構築手順・運用・アップグレード・DR |
| [docs/90-decision-log.md](docs/90-decision-log.md) | 検討の経緯（何を比較して何故そう決めたか） |
| [docs/adr/](docs/adr/) | 個別の意思決定記録（ADR） |

---

## リポジトリ構成

```
.
├── docs/                      設計ドキュメントと ADR
├── tofu/
│   ├── 10-proxmox-talos/      Proxmox VM 作成 + Talos クラスタ構築
│   ├── 20-cloudflare/         Cloudflare Tunnel / Access / Service Token
│   └── modules/talos-node/    VM 定義の再利用モジュール
├── talos/patches/             Talos machine config パッチ（ハードニング）
├── kubernetes/
│   ├── bootstrap/argocd/      ArgoCD 初期導入（1度だけ手で apply）
│   ├── apps/                  app-of-apps（ArgoCD Application 定義）
│   └── infra/                 各基盤コンポーネントのマニフェスト
├── scripts/                   前提チェック・Ceph ユーザー作成などの補助
└── workers/example-origin-api/ Workers から Access 経由で叩く実装サンプル
```

---

## クイックスタート

前提ツールの導入と全手順は [docs/50-operations.md](docs/50-operations.md) を参照。

```bash
# 0. 前提チェック（Proxmox / Ceph / ネットワークの健全性を検証）
./scripts/preflight.sh

# 1. Ceph に Kubernetes 用の最小権限ユーザーを作成
./scripts/ceph-create-k8s-user.sh

# 2. Proxmox 上に Talos VM を作成し、Kubernetes を bootstrap
cd tofu/10-proxmox-talos
cp terraform.tfvars.example terraform.tfvars   # 環境に合わせて編集
tofu init && tofu apply

# 3. Cloudflare 側のリソース（Tunnel / Access / Service Token）を作成
cd ../20-cloudflare
cp terraform.tfvars.example terraform.tfvars
tofu init && tofu apply

# 4. ArgoCD を導入し、以降は GitOps で収束させる
./scripts/bootstrap-argocd.sh
```

---

## 設計の要点（3行）

1. **OS を攻撃対象から外す** — Talos Linux には SSH もシェルもパッケージマネージャも無い。設定は全て署名付き gRPC API 経由の YAML であり、構成ドリフトが原理的に起きない。
2. **内向きポートを 1 つも開けない** — 外部公開は Cloudflare Tunnel の outbound 接続のみで成立させ、認可は Access の Service Token（+ Origin 側での JWT 再検証）で行う。
3. **状態は全て Git に置く** — VM も Kubernetes も Cloudflare も宣言的に定義し、機密情報は SOPS + age で暗号化してコミットする。手作業の余地を残さない。
