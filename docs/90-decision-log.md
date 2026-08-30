# 90. 意思決定の経緯（Decision Log）

このドキュメントは、**「なぜこの構成になったのか」を時系列で残す**ためのものである。
個別の技術判断は [ADR](adr/) に、その手前にあった観察・制約・葛藤をここに書く。

---

## 2026-08-30: 着手時点の観察

### リポジトリの状態

作業開始時、リポジトリのワーキングツリーは**空**だった（`.gitignore` のみ）。
Git 履歴には Ubuntu + kubeadm ベースの構成が残っていた。

```
f154b66 feat(k8s-node-setup): ホームラボリポジトリのクローン処理を改善し、taint解除を追加
3fb8240 refactor(k8s): Helmの待機オプションを削除し、tolerationを追加
920398f feat(argocd): ApplicationSetを有効化
...
```

過去の資産:

- `deploy-vm.sh` — Proxmox 上に cloud-init で VM を一括作成する Bash（約 200 行）
- `scripts/k8s-node-setup.sh` — 各 VM 初回起動時に kubeadm / keepalived / haproxy /
  Cilium / ArgoCD / MetalLB を導入するスクリプト
- `ansible/` — SSH 鍵配布、kubeadm join などのロール
- `k8s-manifests/` — ArgoCD の app-of-apps、MetalLB、kube-prometheus-stack、Velero

**そして、ここに致命的な問題が 1 つあった。**

```ini
# ansible/hosts/k8s-servers/inventory
[k8s-servers:vars]
ansible_ssh_pass=<8文字の平文パスワード>   # ← 平文の SSH パスワードがコミットされていた
```

これは脅威 T6（秘密情報の漏洩）の実例であり、
**「秘密は Git に置くが、平文では置かない」という設計原則を立てる直接のきっかけ**になった。
新構成では SOPS + age による暗号化を必須とし、CI に `gitleaks` を組み込む。

> なお、この認証情報は Git 履歴に残っているため、
> **リポジトリを公開する前に履歴の書き換え（`git filter-repo`）が必要**である。
> [docs/50-operations.md](50-operations.md) に手順を記載する。

### 実環境の観察

SSH で Proxmox に接続し、実測した。

| 観察したこと | 設計への影響 |
| --- | --- |
| Proxmox VE 9.0.11 / 3 ノードクラスタ `homelab` が Quorate | HA 前提の設計が可能 |
| 各ノード 16 vCPU / 58-62 GiB RAM | CP 3 + WK 3 の 6 VM を余裕をもって収容できる |
| Ceph 3 OSD / 2.8 TiB / **HEALTH_WARN** | ⚠️ 構築前に解決すべき課題として preflight に組み込む |
| `cephrdb_k8s` プールが既に存在し 186 GiB 使用済み | 旧クラスタの残骸の可能性。preflight で孤児 RBD を検出 |
| Proxmox ホストは VLAN20/30 に IP を持つが **VLAN40 には持たない** | ノード管理は Talos API 経由に一本化する必要がある |
| VLAN40 GW (172.16.40.1) には Proxmox からも Mac からも疎通する | IX2215 が L3 ルーティングしている。ただしストレージを通すには非力 → [ADR-0007](adr/0007-dual-nic-topology.md) |
| Mac から Tailscale (utun4) 経由で 172.16.10.0/24 と 172.16.40.0/24 に到達 | 運用端末からの管理経路が確保されている。ingressFirewall で CGNAT 帯を許可する |
| **PBS は 172.16.10.10 ではなく 172.16.10.51 にいた** | 当初の認識と実態が異なっていた。`.10` は ARP FAILED（不在） |

### PBS の所在について

作業依頼では「172.16.10.10 に pbs」とされていたが、実測の結果:

```
$ ip neigh show dev vmbr0
172.16.10.10 FAILED                                    ← 不在
172.16.10.51 lladdr e8:ff:1e:da:46:ee REACHABLE        ← ここにいた

$ (echo > /dev/tcp/172.16.10.51/8007) && echo OPEN
OPEN                                                    ← PBS の Web UI ポート
```

Mac からも `172.16.10.51` へ ICMP 到達可能であり、**すでにアクセスできる状態**だった。
本リポジトリでは実測値 `172.16.10.51` を正とする。
`.10` への変更を希望する場合は PBS 側のネットワーク設定変更が必要になる。

---

## 検討の流れ

### 問い 1: 既存の kubeadm 資産を活かすべきか？

**結論: 活かさない。**

「セキュアに構築する」が最優先要件である以上、ノード OS の選定が最大の分岐点になる。
Ubuntu を CIS 準拠にハードニングするには、

- SSH の設定（鍵のみ、root ログイン禁止、ポート変更…）
- カーネルパラメータ（`sysctl`）
- auditd のルール
- ファイル権限の是正
- 不要なパッケージ・サービスの削除
- 継続的なパッチ適用

これらを**自前で書き、レビューし、劣化しないよう維持し続ける**必要がある。
書いたコードの分だけ、バグと設定漏れの余地が生まれる。

Talos Linux は、これらの大半を「そもそもその機能が存在しない」ことで解決する。
SSH をハードニングする最善の方法は、SSH を持たないことである。

→ [ADR-0001](adr/0001-talos-linux.md)

**これは既存資産を捨てる決断であり、軽く下したものではない。**
学習コスト（SSH が使えないデバッグ）を受け入れる代わりに、
維持すべきハードニングコードがゼロになる利益を取った。

### 問い 2: Ceph をどう繋ぐか？ — 見落としかけた重要な点

当初、「K8s ノードは VLAN40 のままで、IX2215 が L3 で VLAN20 と疎通するから
問題ない」と考えていた（旧 `docs/requirements.md` にもそう書かれていた）。

しかし、これは**ストレージ I/O の全てが家庭用ルータを通過する**ことを意味する。
IX2215 のルーティング性能は数百 Mbps 〜 1 Gbps 程度であり、
**10GbE の Ceph が全く活かせない上に、ルータがストレージの単一障害点になる。**

Proxmox ホスト自身が `vmbr1.20` で VLAN20 に直結していることに気づき、
Kubernetes ノードにも同じ経路を持たせるべきだと判断した。

→ [ADR-0007](adr/0007-dual-nic-topology.md)

代償として「K8s ノードが Ceph public network に直結する」という攻撃面が増える。
これは cephx の最小権限（`client.admin` を渡さない）と
Talos の ingressFirewall で相殺する設計とした。

### 問い 3: 「安全に外部からアクセス」をどう定義するか？

要件は「Cloudflare Worker にデプロイした SaaS から安全にアクセス」である。
「安全に」を次のように具体化した。

1. 自宅にインバウンドのポートを開けない
2. 自宅のグローバル IP を露出しない
3. その SaaS **だけ**が呼べる（他の誰も呼べない）
4. 認証情報が漏れたら失効・ローテーションできる
5. アクセスログが残る
6. Cloudflare 側の設定事故が単一障害点にならない

1〜5 は Cloudflare Tunnel + Access + Service Token で満たせる。
**6 が重要な追加要件**であり、これに対して cloudflared 側で
`originRequest.access.required = true` を設定し、
Origin 手前でもう一度 JWT を検証する多層防御を入れることにした。

→ [ADR-0005](adr/0005-cloudflare-zero-trust.md)

### 問い 4: バックアップ先をどこにするか？ — 旧構成の欠陥

旧リポジトリには `k8s-manifests/apps/cluster-wide-app-resources/minio-for-velero/`
があり、**クラスタ内の MinIO を Velero のバックアップ先にしていた。**

この MinIO の PVC は Ceph 上にある。つまり:

> **Ceph が壊れたら、バックアップも一緒に消える。**

これは「バックアップ」の定義を満たしていない。誤削除（S3）からは復旧できるが、
ストレージ層の障害（S5）やランサムウェア（S6）には無力である。

新構成では、Velero のバックアップ先を**クラスタ外**に置くことを既定とし、
さらに PBS（別筐体、172.16.10.51）による VM バックアップを併用する
3 階層構成とした。

→ [ADR-0008](adr/0008-backup-strategy.md)

---

## 意図的に「やらなかったこと」

正直に記録する。

| やらなかったこと | 理由 |
| --- | --- |
| Secure Boot + TPM でのディスク暗号化 | Talos アップグレード時の鍵再封印の運用負荷が、想定する脅威（ディスク単体の持ち出し）に見合わない。`nodeID` 方式を採用し、この限界を [docs/20-security-design.md](20-security-design.md) に明記した |
| Kubernetes API の外部公開 | 管理平面をインターネットに近づける利益が、リスクに見合わない。Tailscale 経由に限定 |
| ArgoCD UI の外部公開 | 同上 |
| サービスメッシュ（Istio 等）の導入 | Cilium の機能で要件を満たせる。運用複雑度に見合わない |
| Falco 等のランタイム検知 | 常時のリソース消費に対し、まず Hubble と Trivy で必要性を見極める |
| 旧 kubeadm 構成との並行運用 | 「全て削除して作り直す」方針を選択（依頼者の判断） |

---

## この先の宿題

| # | 項目 | 優先度 |
| --- | --- | --- |
| 1 | **Ceph の OSD をエンタープライズ SSD へ換装する**（下記参照） | **高** |
| 2 | Git 履歴に残る平文 SSH パスワードの除去（`git filter-repo`） | **高**（公開前に必須） |
| 3 | `cephrdb_k8s` の既存 186 GiB の内容確認と整理 | 中 |
| 4 | Velero のバックアップ先を外部 S3（R2 等）へ | 中 |
| 5 | OpenTofu ステートのリモートバックエンド移行 | 中 |
| 6 | PBS の IP を `.10` に統一するか、`.51` を正とするか決定 | 低 |

---

## 2026-08-30 追記: Ceph の HEALTH_WARN を調査した

構築前の必須課題としていた Ceph の警告について、実機を調査した。

### 分かったこと

OSD が載っているのは **`SUNEAST SE800 Lite SSD 1024GB`**（コンシューマ向け SATA SSD）だった。

```
Power_On_Hours          29201    （約 3.3 年稼働）
Wear_Leveling_Count     100      （摩耗は正常）

# osd.1 のログ
bdev(...) read stalled read 0xbb8de6a000~3000 (buffered) since 35252.30s, timeout is 5.00s
```

摩耗指標は正常である。つまり **「壊れかけている」のではなく、
「元々 Ceph の要求に対して性能が足りない」**。

PLP（電源断保護）を持たないコンシューマ SSD は、Ceph が発行する同期書き込みごとに
実フラッシュ書き込みが発生してレイテンシが跳ねる。また "Lite" 型番は DRAM キャッシュを
省いていることが多く、FTL のアドレス変換テーブルを NAND から都度読むため、
ランダム読み取りが数秒スパイクする。`DB_DEVICE_STALLED_READ_ALERT` は Ceph 19.2 (Squid) で
追加された、まさにこの種のデバイスを検出するための警告である。

### やったこと

TRIM を有効化し、OSD を 1 台ずつ再起動して反映した。

```bash
ceph config set osd bdev_enable_discard true
ceph config set osd bdev_async_discard_threads 1
```

警告の対象は **3 OSD → 1 OSD** に減り、全 PG が `active+clean` を維持している。

### やらなかったこと（と、その正直な評価）

**警告の閾値を上げて黙らせることはしなかった。** それは問題を隠すだけだからである。

また、**この対処は根本解決ではない**。TRIM は書き込み性能には効くが、
stalled *read* は SSD コントローラの応答特性に起因するため、Ceph 側の設定では
解消しきれない。警告が減ったのも、OSD の再起動で観測カウンタがリセットされた
側面がある（時間経過で再発しうる）。

根本解決は **PLP 付きエンタープライズ SSD への換装** である。
そのため上の宿題 #1 を「原因究明」から「換装」に書き換えた。

現状のまま Kubernetes を載せることは可能だが、
[docs/30-storage-design.md §2](30-storage-design.md) に記載したリスクを
許容することになる。まず軽量なワークロードから載せ、
`ceph_osd_op_r_latency` / `ceph_osd_op_w_latency` を監視しながら
段階的に負荷を上げることを推奨する。
