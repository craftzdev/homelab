# ADR-0006: GitOps に ArgoCD、秘密管理に SOPS + age を使う

- **状態**: 承認済み
- **日付**: 2026-08-30

## 背景

「構築は常にコードで実装してリポジトリに管理」という要件を、
Kubernetes 内部のワークロードにも適用する必要がある。
`kubectl apply` を手で叩く運用は、この要件を満たさない。

## GitOps ツールの選択

| 選択肢 | 評価 |
| --- | --- |
| `kubectl apply -k` を CI から | 差分検出・自己修復・ドリフト検知が無い |
| Flux CD | 軽量・Kubernetes ネイティブ。SOPS を**ネイティブサポート**する点は魅力 |
| **ArgoCD** ★採用 | UI による差分可視化、app-of-apps、既存リポジトリの資産と整合 |

**ArgoCD を採用した理由**:

1. 既存リポジトリが ArgoCD ベースで構成されていた（`k8s-manifests/apps/root/apps.yaml`
   の app-of-apps パターン）。知見の連続性がある
2. **差分の可視化**が優れている。「今、宣言と実態がどう違うか」を UI で
   即座に確認できることは、セキュリティ運用（構成ドリフトの検知）で価値がある
3. `ApplicationSet` によるアプリの動的生成が使える

Flux の SOPS ネイティブサポートは魅力だったが、ArgoCD でも
KSOPS / `argocd-vault-plugin` で対応でき、UI の利点が上回ると判断した。

## 秘密管理の選択

| 選択肢 | Git に置ける | 外部依存 | 評価 |
| --- | --- | --- | --- |
| 平文の Secret を Git に | — | 無 | ✗ 脅威 T6 そのもの。論外 |
| Sealed Secrets | ✓ | クラスタ内コントローラ | △ クラスタを作り直すと復号鍵を失う（バックアップ必須） |
| External Secrets Operator + Vault/1Password | ✗ | **有（外部サービス）** | △ ホームラボで別途 Vault を運用する負荷が大きい |
| **SOPS + age** ★採用 | ✓ | **無** | 採用 |

**SOPS + age を採用した理由**:

1. **外部の秘密ストアに依存しない**。ホームラボの規模で Vault を運用するのは
   過剰であり、Vault 自体が新たな単一障害点になる
2. **age 秘密鍵さえあれば、クラスタが全滅しても全て復号できる**。
   災害復旧のシナリオで Sealed Secrets より優れる
3. `encrypted_regex: '^(data|stringData)$'` により、Secret の
   **値だけを暗号化し、メタデータは平文で残せる**。
   これにより PR のレビューで「どの namespace のどんな名前の Secret が
   追加されたか」が読める。全文暗号化より運用しやすい
4. GnuPG より age の方が鍵管理が単純で、事故が起きにくい

### ArgoCD との連携

ArgoCD の repo-server に **KSOPS** を kustomize プラグインとして組み込み、
`age` 秘密鍵を Secret（`sops-age`）としてマウントする。

```
kustomization.yaml
  generators:
    - secret-generator.yaml   # KSOPS が *.sops.yaml を復号して Secret を生成
```

## 運用上の必須ルール

1. **age 秘密鍵（`keys.txt`）はリポジトリに絶対に入れない**（`.gitignore` 済み）
2. **秘密鍵はオフラインでバックアップする**（パスワードマネージャ等）。
   これを失うと全ての暗号化済み秘密が復号不能になる
3. CI で `gitleaks` を実行し、平文の秘密が混入していないか検査する
4. `tofu` のステートファイルには Talos の CA 秘密鍵が**平文で含まれる**。
   これは SOPS の管理外なので、ローカル保管か暗号化バックエンドを使う
   （[ADR-0002](0002-opentofu.md) 参照）

## トレードオフ

- ❌ ArgoCD の repo-server にカスタムイメージ or initContainer が必要になる
  → `kubernetes/bootstrap/argocd/` で宣言的に構成する
- ❌ 暗号化された値は「変更されたこと」しか差分で分からない
  → メタデータを平文にすることで最低限の可読性は確保する

- ❌ **1 本の age 鍵が全ての秘密を復号できる**

  > codex のレビューで指摘された点であり、事実として認める。
  > Kubernetes の Secret も OpenTofu の変数も同じ age recipient を使い、
  > その秘密鍵を ArgoCD の repo-server にマウントしている。
  > **repo-server または argocd namespace の侵害は、Cloudflare の
  > TunnelSecret も Grafana の認証情報も含む全秘密の漏洩を意味する。**
  >
  > 本来は信頼境界ごとに age 鍵を分割し、Application ごとに復号権限を
  > 分けるべきである（別々の repo-server / CMP、あるいは外部の
  > Secrets 管理基盤）。
  >
  > 現時点でそうしていない理由は、3 ノードのホームラボで
  > 「repo-server を複数運用する」複雑さが、得られる分離に見合わないと
  > 判断したためである。ただしこれは**規模に依存する判断**であり、
  > 扱う秘密の重要度が上がったら見直すこと。
  >
  > 当面の緩和策:
  >   - argocd namespace への RBAC を厳格に保つ
  >   - argocd namespace に default-deny を敷く（実装済み）
  >   - ArgoCD UI を外部公開しない（実装済み）
