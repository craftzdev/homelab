# 20. セキュリティ設計

## 1. 前提となる脅威モデル

ホームラボであっても、以下は現実的な脅威として扱う。

| ID | 脅威 | 想定シナリオ |
| --- | --- | --- |
| T1 | **インターネットからの直接侵入** | ポートフォワードや UPnP により公開された管理画面・API が総当たり／既知脆弱性で突破される |
| T2 | **公開サービス経由の侵入** | 外部公開したアプリの脆弱性を突かれ、Pod 内でコード実行される |
| T3 | **Pod からの横展開・権限昇格** | 侵害された Pod がノードやクラスタ全体へ権限昇格する |
| T4 | **ノード OS の侵害** | SSH の弱いクレデンシャル、放置されたパッケージの脆弱性 |
| T5 | **物理媒体の持ち出し** | ディスク／ホストの盗難により保存データが読まれる |
| T6 | **秘密情報の漏洩** | Git リポジトリに平文のトークンやキーがコミットされる |
| T7 | **サプライチェーン攻撃** | `latest` タグのイメージや野良 Helm chart 経由で悪意あるコードが混入する |
| T8 | **管理経路の乗っ取り** | Kubernetes API / Talos API が広いネットワークに露出している |
| T9 | **ストレージへの不正アクセス** | Ceph の admin キーが Kubernetes 側に配られ、全プールが操作可能になる |
| T10 | **可用性の喪失・ランサム** | データが暗号化・削除され、復旧手段が無い |

## 2. 多層防御の全体像

```
 ┌─ L1. 境界 ───────────────────────────────────────────────┐
 │  インバウンドのポート開放ゼロ / Cloudflare Tunnel のみ      │  → T1
 │  Cloudflare WAF・DDoS 保護がエッジで前段処理               │
 └───────────────────────┬──────────────────────────────────┘
 ┌─ L2. 認可 ────────────▼──────────────────────────────────┐
 │  Cloudflare Access Service Token（非人間 ID）             │  → T1, T2
 │  Origin(cloudflared) 側で JWT を再検証（多層防御）         │
 └───────────────────────┬──────────────────────────────────┘
 ┌─ L3. ネットワーク ─────▼──────────────────────────────────┐
 │  Cilium default-deny NetworkPolicy / L7 制御 / Hubble      │  → T2, T3
 │  Talos ingressFirewall で管理ポートを CIDR 制限            │  → T8
 └───────────────────────┬──────────────────────────────────┘
 ┌─ L4. ワークロード ─────▼──────────────────────────────────┐
 │  Pod Security Admission = restricted / RBAC 最小権限       │  → T3
 │  イメージのダイジェスト固定 / Trivy によるスキャン          │  → T7
 └───────────────────────┬──────────────────────────────────┘
 ┌─ L5. ノード OS ────────▼──────────────────────────────────┐
 │  Talos Linux: SSH 無し・シェル無し・read-only rootfs       │  → T4
 │  STATE / EPHEMERAL パーティションを LUKS2 で暗号化          │  → T5
 └───────────────────────┬──────────────────────────────────┘
 ┌─ L6. データ ───────────▼──────────────────────────────────┐
 │  Ceph は最小権限ユーザー（プール限定 cephx）               │  → T9
 │  etcd / Velero / PBS の 3 階層バックアップ                 │  → T10
 │  秘密は SOPS + age で暗号化してから Git へ                 │  → T6
 └──────────────────────────────────────────────────────────┘
```

## 3. レイヤ別の具体策

### L1/L2: 外部公開経路（→ T1, T2）

- **ポート開放を一切行わない。** cloudflared が張る outbound QUIC のみを使う。
  これにより「自宅のグローバル IP を知られても、そこに叩けるものが無い」状態になる。
- Cloudflare Access のポリシーは `decision = "non_identity"` +
  `include = [{ service_token = ... }]`。人間の ID ではなくサービストークンでのみ通す。
- `service_auth_401_redirect = true` を設定し、認可失敗時に IdP へリダイレクトせず
  **401 を返す**。API クライアント（Workers）にとってリダイレクトは誤動作の元になる。
- **多層防御**: cloudflared の `originRequest.access` を有効にし、cloudflared 自身が
  `Cf-Access-Jwt-Assertion` の署名と `aud` を検証する。仮に Access アプリケーション
  の設定が事故で外れても、Origin 手前でリクエストが落ちる。
- Service Token は `duration` を有効期限付きにし、`client_secret_version` の
  インクリメントでローテーションできるようにする。

### L3: ネットワーク（→ T2, T3, T8）

- **Cilium の default-deny**: 各 namespace に「同一 namespace 内と DNS 以外は拒否」の
  `CiliumNetworkPolicy` を敷き、必要な通信だけを明示的に許可する。

  > **実装状況を正確に記す**: `policyEnforcementMode` は `default`（ポリシーが
  > 紐づいていない Endpoint は許可）であり、**自動的に全 namespace が
  > 守られるわけではない**。`kubernetes/infra/policies/namespace-default-deny.yaml`
  > と各コンポーネントの `networkpolicy.yaml` で、namespace ごとに明示的に
  > 定義している。
  >
  > `always` にしない理由は、ポリシーの書き漏れが即座にクラスタ全体の停止に
  > つながることと、bootstrap 中に CoreDNS / apiserver への通信が落ちて
  > 詰むことを避けるため。代わりに「どの namespace が保護されているか」が
  > Git 上で読める形にしている。
  >
  > ⚠️ **新しい namespace を作ったら default-deny の追加を忘れないこと。**
  > 追加を忘れるとその namespace は無防備なままになる。
- **Talos ingressFirewall**: `NetworkDefaultActionConfig.ingress = block` を既定とし、
  Talos API(50000) / kube-apiserver(6443) / etcd(2379-2380) / kubelet(10250) を
  それぞれ必要最小の送信元 CIDR にのみ開ける（[docs/10-network-design.md](10-network-design.md) §5）。
- **egress の制御**: cloudflared 以外の Pod がインターネットへ出る必要は基本的に無い。
  外部通信が必要なコンポーネント（cert-manager の ACME、cloudflared、
  Longhorn のバックアップ、Trivy の DB 取得）に限り明示的に egress を許可する。

- ⚠️ **hostNetwork の Pod には CiliumNetworkPolicy が適用されない**

  > codex のレビューで指摘された、この構成の**構造的な限界**である。正確に記す。
  >
  > `hostNetwork: true` で動く Pod（Longhorn の instance-manager、
  > node-exporter、Cilium 自身など）は Pod のネットワーク名前空間を使わないため、
  > 通常の `CiliumNetworkPolicy` / `CiliumClusterwideNetworkPolicy` の
  > 対象外になる。**これらは NetworkPolicy を迂回してノードの
  > 到達範囲へ通信できる。**
  >
  > 本構成でこの層を守っているのは NetworkPolicy ではなく、
  >
  > 1. **Talos の ingressFirewall** — VLAN40 以外からの inbound を既定で遮断
  > 2. **VLAN40 に Kubernetes ノードしか置かない**というネットワーク設計
  > 3. **Cilium の WireGuard** によるノード間通信の暗号化
  >
  > である。
  >
  > **より厳密にする方法**: Cilium の Host Firewall（`hostFirewall.enabled: true`）を
  > 有効にすると、`CiliumClusterwideNetworkPolicy` を hostNetwork の
  > ワークロードにも適用できる。
  >
  > 現時点で有効化していない理由は、**設定を誤るとノードへの到達性を失い、
  > SSH の無い Talos では復旧が困難**なためである。導入する場合は
  > Audit Mode（`policyAuditMode`）で実際の通信を数日収集し、
  > 必要な許可を洗い出してから 1 ノードずつ適用すること。

### L4: ワークロード（→ T3, T7）

- **Pod Security Admission**: クラスタ全体の既定を `restricted` にする
  （Talos の `cluster.apiServer.admissionControl` で `PodSecurity` の
  `defaults` を設定）。特権が必要な namespace（`kube-system`, `ceph-csi`,
  `cilium-system` 等）のみ、ラベルで明示的に例外化する。
- **RBAC**: ArgoCD / 監視 / CSI などのサービスアカウントは、必要な verb と
  resource のみに絞る。`cluster-admin` の付与は ArgoCD の Application コントローラ
  以外に行わない。
- **kube-apiserver / kubelet の基本的なハードニング**
  （`anonymous-auth=false`、kubelet の `readOnlyPort=0`、
  `authorization-mode=Webhook`、`--profiling=false` 等）。

  > **正確に記す**: このうち `anonymous-auth` / `readOnlyPort` /
  > `authorization-mode` は **Talos が既定で設定している**ものであり、
  > 本リポジトリでは上書きしていない。これが Talos を選んだ理由そのもの
  > （[ADR-0001](adr/0001-talos-linux.md)）である。同じ値を `extraArgs` に
  > 重複して書くと、Talos 側の設定と衝突する可能性があるため書いていない。
  >
  > 本リポジトリが明示的に追加しているのは、Talos の既定に含まれない
  > `profiling=false` / `service-account-lookup=true` / 監査ポリシー /
  > Pod Security Admission の `restricted` 化である
  > （`talos/patches/controlplane.yaml.tftpl`）。
  >
  > 実際の設定値は次のコマンドで確認できる。
  > ```bash
  > talosctl -n 172.16.40.11 get machineconfig -o yaml
  > ```
- **監査ログ**: kube-apiserver の audit policy を有効化し、
  Secret へのアクセスや RBAC の変更を Metadata レベル以上で記録する。
- **イメージ**: Helm chart のバージョンと OCI イメージの**タグを固定**し、
  Renovate で更新を PR として受ける（`latest` は使わない）。
  クラスタ内では Trivy Operator が稼働イメージを継続スキャンする。

  > **現状の限界を明記する**: 現時点ではタグ固定であり、**ダイジェスト固定
  > （`@sha256:...`）には至っていない**。タグは再割り当てが可能なため、
  > 供給元が改竄された場合に同じタグで別のイメージを引く余地が残る。
  > Renovate はダイジェストの付与・更新に対応しているため、
  > 運用が安定した段階で `pinDigests: true` を有効化して移行する。

### L5: ノード OS（→ T4, T5）

Talos Linux を選んだ最大の理由がここにある（[ADR-0001](adr/0001-talos-linux.md)）。

- **SSH デーモンが存在しない**。シェルも `bash` も `apt` も無い。
  そもそも「ログインして何かする」という操作が定義されていない。
- **root ファイルシステムは read-only かつ squashfs**。実行可能ファイルの
  差し替えという典型的な永続化手法が成立しない。
- **API は全て mTLS**。`talosconfig` のクライアント証明書を持たない限り、
  ポート 50000 に到達できても何もできない。
- **ディスク暗号化**: `STATE`（machine config・シークレット格納）と
  `EPHEMERAL`（コンテナイメージ・emptyDir）を LUKS2 で暗号化する。
  鍵は `nodeID`（ノードの UUID から導出）を使用する。

  > **設計上の注意（正直に記載する）**: `nodeID` 方式は「ディスク単体を抜き取られた
  > 場合」には有効だが、VM ごと（= UUID ごと）コピーされた場合は防げない。
  > より強い保証が必要なら TPM を VM に割り当てて `tpm` 方式に切り替える。
  > 本構成では、脅威 T5 を「物理ディスクの持ち出し」と定義し `nodeID` を採用した。

- **Talos の管理 API アクセス**は `talosconfig` に依存する。これはリポジトリには
  コミットせず、ローカルまたは SOPS 暗号化して保管する。

### L6: データと秘密（→ T6, T9, T10）

- **Ceph の最小権限**: Kubernetes には `client.admin` を**絶対に渡さない**。
  用途ごとに専用ユーザーを作り、caps をプール／ファイルシステム単位に限定する。

  ```
  client.k8s-rbd     mon 'profile rbd'
                     osd 'profile rbd pool=cephrdb_k8s'
                     mgr 'profile rbd pool=cephrdb_k8s'

  client.k8s-cephfs  mon 'allow r fsname=cephfs01'
                     mds 'allow rw fsname=cephfs01 path=/volumes/csi'
                     osd 'allow rw tag cephfs data=cephfs01'
                     mgr 'allow rw'
  ```

  これにより、Kubernetes 側が侵害されても `cephrdb_vm`（Proxmox VM の実体）や
  他プールには手が出せない。

- **秘密管理**: SOPS + age。`encrypted_regex` により Secret の `data` /
  `stringData` のみを暗号化し、それ以外は平文で残すことで差分レビューを可能にする。
  age 秘密鍵はリポジトリ外（`~/.config/sops/age/keys.txt`）に保管し、
  クラスタ側には Secret として 1 度だけ投入する。
- **バックアップ**: 3 階層で復旧点を確保する（[ADR-0008](adr/0008-backup-strategy.md)）。
  1. **etcd スナップショット** — クラスタ状態（Talos が定期取得可能）
  2. **Velero** — namespace 単位のアプリ + PVC（ceph-csi のスナップショット連携）
  3. **PBS** — VM 丸ごと（172.16.10.51）

## 4. 意図的に採用しなかった対策と、その理由

正直に記録しておく。「やっていないこと」を明示することもセキュリティ設計の一部である。

| 採用しなかったもの | 理由 |
| --- | --- |
| Secure Boot + TPM ベースのディスク暗号化 | Proxmox VM への TPM/UEFI 追加は可能だが、Talos のアップグレード時に鍵の再封印が必要になり、ホームラボの運用負荷に見合わない。脅威 T5 の想定を「ディスク単体の持ち出し」に限定し `nodeID` を採用した。要件が上がれば ADR を追加して移行する |
| Falco 等のランタイム脅威検知 | 常時 CPU/メモリを消費する。まず Cilium の Hubble による通信可視化と Trivy の脆弱性検知を先に入れ、必要性が確認できた段階で追加する |
| サービスメッシュ（Istio / Linkerd）による mTLS | Cilium の透過暗号化（WireGuard）で代替可能であり、運用複雑度に見合わない。宅内 L2 内の通信であることも考慮した |
| Kubernetes API のインターネット公開（Cloudflare Tunnel 経由含む） | 管理平面をインターネットに近づける利益より、リスクが上回る。管理は Tailscale 経由に限定する |
| Cilium Host Firewall（hostNetwork Pod へのポリシー適用） | 設定を誤るとノードへの到達性を失い、SSH の無い Talos では復旧が困難。まず Audit Mode で実通信を収集し、必要な許可を洗い出してから 1 ノードずつ適用すべきもの。現状は Talos の ingressFirewall + VLAN 分離で代替している（§3 L3 に限界を明記） |
| age 鍵の分割（信頼境界ごとの鍵） | 3 ノードのホームラボで repo-server を複数運用する複雑さが、得られる分離に見合わない。ただし 1 本の鍵が全秘密を復号できることは事実であり、[ADR-0006](adr/0006-argocd-sops.md) に明記した |
| イメージ署名の強制（Sigstore / Kyverno verifyImages） | 有効だが、まずダイジェスト固定と Trivy を優先。導入時は Kyverno のポリシーとして追加する |

## 5. 継続的な検証

| 項目 | 手段 | 頻度 |
| --- | --- | --- |
| 構成のドリフト | ArgoCD の self-heal + `tofu plan` の差分確認 | 常時 / 週次 |
| 稼働イメージの脆弱性 | Trivy Operator | 常時 |
| ネットワークポリシーの実効性 | Hubble で drop されている通信を確認 | 随時 |
| バックアップからの復旧 | Velero restore のリハーサル | 四半期 |
| 秘密の平文混入 | `gitleaks`（CI） | PR 毎 |
