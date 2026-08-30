# homelab — Proxmox VE 上に構築するセキュアな Kubernetes 基盤

自宅の Proxmox VE クラスタ（3ノード）の上に、**Talos Linux による
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
  │ (Workers)│ token│  │ (ServiceTk)│ JWT  │  (cloudflared)│   │
  └──────────┘      │  └────────────┘      └───────┬───────┘   │
                    └──────────────────────────────┼───────────┘
                                                   │ outbound only (QUIC/443)
  ═══════════════════════════════════════════════ │ ═══════════ 自宅 NW 境界
                                                   ▼
                              ┌─────────────────────────────────────┐
                              │  Kubernetes (Talos Linux) VLAN40    │
                              │  cloudflared × 2 → Ingress → App    │
                              │  CNI: Cilium / PV: Longhorn ×3      │
                              └───────────────┬─────────────────────┘
                                              │
                              ┌───────────────▼─────────────────────┐
                              │  Proxmox VE 3ノード（local-ZFS）     │
                              │  1物理ノード = 1 K8sノード           │
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
| [docs/30-storage-design.md](docs/30-storage-design.md) | ストレージ設計（Longhorn / local-ZFS） |
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
├── talos/patches/             Talos machine config パッチ（ハードニング）
├── kubernetes/
│   ├── bootstrap/argocd/      ArgoCD 初期導入（1度だけ手で apply）
│   ├── apps/                  app-of-apps（ArgoCD Application 定義）
│   └── infra/                 各基盤コンポーネントのマニフェスト
├── scripts/                   前提チェック・Ceph 廃止・bootstrap などの補助
└── workers/example-origin-api/ Workers から Access 経由で叩く実装サンプル
```

---

## クイックスタート

前提ツールの導入と各手順の詳細は [docs/50-operations.md](docs/50-operations.md) を参照。
**順序に意味があります**（CNI が無いとノードが Ready にならない、
Ceph を廃止しないと SSD が解放されない、等）。

```bash
# 0. 前提チェック — ストレージ / ネットワーク / IP 重複 / VMID 衝突
./scripts/preflight.sh

# 1. 旧クラスタの VM を削除（dry-run で確認してから --yes）
./scripts/destroy-legacy-vms.sh
./scripts/destroy-legacy-vms.sh --yes

# 1b. Ceph を廃止し、SATA SSD を local-zfs 化する
#     ⚠️ Ceph の全データが失われる。先に dry-run で内容を確認すること
./scripts/decommission-ceph.sh
./scripts/decommission-ceph.sh --yes

# 2. Proxmox 上に Talos VM を作成し、Kubernetes を bootstrap
cd tofu/10-proxmox-talos
cp terraform.tfvars.example terraform.tfvars   # 環境に合わせて編集
tofu init && tofu plan && tofu apply
cd ../..
#    → この時点ではまだ CNI が無いので全ノードが NotReady

# 3. Cilium を導入してノードを Ready にする
./scripts/bootstrap-cluster.sh

# 4. Grafana の管理者パスワードを SOPS で用意する
#    （未設定だと Grafana は起動しない ＝ 既定パスワードで動く事故を防ぐ）
#    手順: kubernetes/infra/monitoring/README.md

# 5. ArgoCD を導入し、以降は GitOps で収束させる
./scripts/bootstrap-argocd.sh

# 6. Cloudflare 側のリソース（Tunnel / Access / Service Token）を作成
cd tofu/20-cloudflare
cp terraform.tfvars.example terraform.tfvars
export TF_VAR_cloudflare_api_token='...'
tofu init && tofu apply
cd ../..

# 7. Tunnel の認証情報を SOPS 暗号化して Git へ入れる
./scripts/sync-cloudflare-secrets.sh
```

---

## 設計の要点（3行）

1. **OS を攻撃対象から外す** — Talos Linux には SSH もシェルもパッケージマネージャも無い。設定は全て署名付き gRPC API 経由の YAML であり、構成ドリフトが原理的に起きない。
2. **内向きポートを 1 つも開けない** — 外部公開は Cloudflare Tunnel の outbound 接続のみで成立させ、認可は Access の Service Token（+ Origin 側での JWT 再検証）で行う。
3. **状態は全て Git に置く** — VM も Kubernetes も Cloudflare も宣言的に定義し、機密情報は SOPS + age で暗号化してコミットする。手作業の余地を残さない。

> **2026-08-30 に Ceph を廃止しました。** 実測で OSD のコンシューマ SSD が
> Ceph の要求性能に届いていないことが判明し、維持には 10〜20 万円の換装が
> 必要でした。一方で実際に載っていたデータは ISO 9GB のみ。
> PV は Longhorn（K8s 内 3 レプリカ）へ移し、SATA SSD は単体 ZFS として
> VM ディスクに使います。経緯は [ADR-0009](docs/adr/0009-drop-ceph-adopt-longhorn.md)。
