# ADR-0010: ingress-nginx をやめ、Cilium の Gateway API を使う

- **状態**: 承認済み
- **日付**: 2026-08-30
- **決定者**: クラフト（codex のレビュー指摘を受けて）

## 背景

当初は HTTP のルーティングに ingress-nginx（chart 4.15.1 / controller v1.15.1）を
採用していた。しかし codex による批判的レビューで、次の指摘を受けた。

> **[HIGH] EOL 済みの ingress-nginx を新規配備している**
> 2026年3月で保守終了した ingress-nginx を配備している。
> 現在は新規リリース、bugfix、脆弱性修正が提供されない。

公式サイトで事実を確認した。

> "Best-effort maintenance will continue until **March 2026**. Afterward,
>  there will be **no further releases, no bugfixes, and no updates to
>  resolve any security vulnerabilities** that may be discovered."
> — https://kubernetes.github.io/ingress-nginx/

現在は 2026 年 8 月であり、**既に EOL 済み**である。

## なぜこれが本構成にとって重大なのか

ingress-nginx は「Tunnel 越しの全 HTTP リクエストを最初に処理する場所」である。
外部公開経路の入口そのものだ。

そこに **今後セキュリティ修正が出ないコンポーネント**を置くことは、
本リポジトリの最優先要件「セキュアに構築する」に真っ向から反する。
既存デプロイは動き続けるが、新しい脆弱性が見つかっても誰も直さない。

「動くから良い」で済ませられる場所ではない。

## 検討した選択肢

| 選択肢 | 新規コンポーネント | 評価 |
| --- | --- | --- |
| ingress-nginx を使い続ける | なし | ❌ EOL。要件に反する |
| **Cilium Gateway API** ★採用 | **なし**（Cilium に同梱） | ✅ 後述 |
| Envoy Gateway | 増える | ○ 高機能だが、Cilium の Envoy と二重になる |
| Traefik | 増える | ○ 実績十分。ただしコンポーネントが 1 つ増える |
| HAProxy Ingress | 増える | △ Ingress API のまま。Gateway API への移行が別途必要になる |

## 決定

**Cilium の Gateway API を使う。**

### 決め手: 新しいコンポーネントが増えない

既に Cilium を CNI として使っており、その中には **Envoy が L7 プロキシとして
同梱されている**（L7 NetworkPolicy のために既に動いている）。
`gatewayAPI.enabled: true` を設定するだけで、追加の Deployment も
追加のイメージも増えない。

運用対象が増えないことは、ホームラボにおいて非常に大きな利点である。
アップグレード対象も、監視対象も、脆弱性を追いかける対象も増えない。

### Gateway API を選ぶ副次的な利点: 権限分離

Ingress API は「1 つのリソースにルーティングも TLS も annotation による
独自拡張も全部詰め込む」設計だった。誰が何を変更してよいかが曖昧で、
annotation 経由の設定注入が脆弱性の温床にもなっていた
（ingress-nginx の snippet annotation 関連の CVE 群）。

Gateway API は役割を分離する。

| リソース | 誰が管理するか | 何を決めるか |
| --- | --- | --- |
| `GatewayClass` | 基盤（Cilium が提供） | 実装 |
| `Gateway` | 基盤チーム | どのポートを開くか、**どの namespace からルートを受け付けるか** |
| `HTTPRoute` | アプリチーム | 自分の namespace 内のパスとバックエンド |

本構成では `Gateway` の `allowedRoutes` をラベルセレクタで制限しており、
`homelab.io/gateway-access: external` を付けた namespace からしか
ルートを受け付けない。

**「Ingress を作れる権限」が「任意の内部サービスを公開できる権限」に
なっていた問題が、構造的に解消される。**

## 前提条件（いずれも本構成で満たしている）

| 項目 | 状態 |
| --- | --- |
| `kubeProxyReplacement: true` | ✅ 既に設定済み（[ADR-0003](0003-cilium.md)） |
| `l7Proxy: true` | ✅ 既定で有効。明示的にも設定 |
| Gateway API CRD v1.6.1 | ✅ `kubernetes/infra/gateway-api/` で導入 |
| Cilium 1.20 の対応状況 | ✅ Gateway API v1.6.1 で全 Core conformance テストに合格 |

## Service を ClusterIP にする

Gateway は既定で LoadBalancer Service を作る。しかしそれは VLAN40 へ
L2 公開されることを意味し、**Cloudflare Access を迂回できる第 2 の入口**になる。

これは旧 ingress-nginx 構成で実際に作ってしまっていた穴であり、
同じレビューで別途指摘された（`[HIGH] Cloudflare Access を通らない L2 公開経路が存在する`）。

`io.cilium/lb-mode: "clusterip"` アノテーションで ClusterIP にし、
cloudflared からのみ到達できるようにしている。

## 受け入れるトレードオフ

| 項目 | 評価 |
| --- | --- |
| Ingress API の資産が使えない | 本構成にはまだ公開アプリが無いため、移行コストは実質ゼロ |
| Gateway API の情報量が Ingress より少ない | Kubernetes 標準として今後増えていく。今から使う方が長期的に有利 |
| Cilium の Gateway 実装は Pod として見えない | NetworkPolicy で選択できず、`host`/`remote-node` エンティティとして扱う必要がある。ポリシーの書き方が直感的でない点は `kubernetes/infra/gateway/networkpolicy.yaml` に明記した |
| rewrite / redirect 等の高度な機能 | Gateway API の Core / Extended でカバーされる範囲を使う。ingress-nginx の annotation に依存した独自機能は使わない（そもそも使うべきでなかった） |

## 移行後の構成

```
cloudflared (Pod)
  └─ http://cilium-gateway-external.gateway.svc.cluster.local:80
       └─ Gateway "external" (namespace: gateway, ClusterIP)
            └─ HTTPRoute（各アプリの namespace）
                 └─ Service → Pod
```

アプリを公開する手順:

1. アプリの namespace に `homelab.io/gateway-access: external` ラベルを付ける
2. その namespace に `HTTPRoute` を作り、`parentRefs` で Gateway を指す
3. `tofu/20-cloudflare` の `published_services` にホスト名を追加して apply
4. cloudflared を再起動する

## この判断を見直すべき条件

- Cilium の Gateway API 実装に、本構成で必要な機能が不足していると判明した
- Cilium 自体を別の CNI へ置き換えることになった
- ingress-nginx の後継プロジェクトが公式に立ち上がり、広く採用された
