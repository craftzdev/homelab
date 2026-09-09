# 40. 外部アクセス設計（Cloudflare Zero Trust）

## 1. 要件

> Cloudflare Workers にデプロイした SaaS から、自宅 Kubernetes 上の一部サービスへ
> **安全に**アクセスできるようにしたい。

これを次のように分解する。

| # | 要件 | 満たし方 |
| --- | --- | --- |
| R1 | 自宅にインバウンドのポートを開けない | Cloudflare Tunnel（outbound のみ） |
| R2 | 自宅のグローバル IP を露出しない | 同上（DNS は Cloudflare の CNAME を指す） |
| R3 | 「その SaaS だけ」が呼べる | Cloudflare Access + Service Token |
| R4 | 認証情報が漏れた場合に失効・ローテーションできる | Service Token の `duration` と `client_secret_version` |
| R5 | 誰がいつ何を呼んだか記録が残る | Cloudflare Access のログ |
| R6 | Cloudflare の設定事故を単一障害点にしない | Origin 側（cloudflared）で JWT を再検証 |

## 2. 全体フロー

```
┌────────────────────────┐
│ Cloudflare Workers     │  SaaS 本体
│  (SaaS backend)        │
└───────────┬────────────┘
            │ fetch("https://api.internal.example.com/...", {
            │   headers: {
            │     "CF-Access-Client-Id":     env.CF_ACCESS_CLIENT_ID,
            │     "CF-Access-Client-Secret": env.CF_ACCESS_CLIENT_SECRET
            │   }})
            ▼
┌────────────────────────────────────────────┐
│ Cloudflare Access                          │
│  Application: api.internal.example.com     │
│  Policy: decision=non_identity             │
│          include=[service_token(id=...)]   │
│  service_auth_401_redirect = true          │
└───────────┬────────────────────────────────┘
            │ 認可 OK → Cf-Access-Jwt-Assertion を付与
            ▼
┌────────────────────────────────────────────┐
│ Cloudflare Tunnel (edge)                   │
└───────────┬────────────────────────────────┘
            │ cloudflared が張った outbound QUIC/443 上を流れる
            ▼  ══════════ 自宅 NW 境界（ポート開放ゼロ）══════════
┌────────────────────────────────────────────┐
│ cloudflared Deployment (replicas: 2)       │
│  originRequest.access.required = true      │◀── R6: JWT 再検証
│  → http://cilium-gateway-external... │
└───────────┬────────────────────────────────┘
            ▼
     Gateway → HTTPRoute → アプリ Pod
```

## 3. なぜこの構成なのか（代替案との比較）

| 方式 | ポート開放 | GIP 露出 | 認可の強さ | 却下/採用理由 |
| --- | --- | --- | --- | --- |
| ポートフォワード + Nginx + Basic 認証 | **必要** | **する** | 弱 | T1 をそのまま許容してしまう。論外 |
| VPN（WireGuard）を Workers から張る | 必要 | する | 中 | Workers は任意の UDP を出せず、そもそも成立しない |
| Tailscale Funnel | 不要 | しない | 中 | 手軽だが、Workers 側からのアクセス制御が Cloudflare ほど細かくない |
| Cloudflare Spectrum | 不要 | しない | 中 | L4 プロキシ。HTTP の認可を Cloudflare 側でできない。Enterprise 前提 |
| **Tunnel + Access + Service Token** | **不要** | **しない** | **強** | **採用**。R1〜R6 を全て満たす |
| Tunnel + mTLS クライアント証明書 | 不要 | しない | 強 | Workers からの mTLS 送信には Cloudflare 側の mTLS binding が別途必要で、鍵管理の手間が増える。Service Token で要件を満たせるため見送り（[ADR-0005](adr/0005-cloudflare-zero-trust.md)） |

## 4. Cloudflare 側リソース（`tofu/20-cloudflare` が管理）

| リソース | 名前 | 役割 |
| --- | --- | --- |
| `cloudflare_zero_trust_tunnel_cloudflared` | `homelab-k8s` | トンネル本体。`config_src = "local"` |
| `cloudflare_dns_record` | 各公開ホスト名 | `<tunnel-id>.cfargotunnel.com` への CNAME（proxied） |
| `cloudflare_zero_trust_access_service_token` | `saas-worker` | Workers 用の非人間 ID |
| `cloudflare_zero_trust_access_policy` | `allow-saas-worker` | `decision = non_identity`, `include = service_token` |
| `cloudflare_zero_trust_access_application` | 各公開ホスト名 | Access で保護するアプリ定義 |

### `config_src = "local"` を選んだ理由

Tunnel の ingress ルール（どのホスト名をどの Service に流すか）は、
**Kubernetes 側の関心事**である。これを Cloudflare ダッシュボード / API 側
（`config_src = "cloudflare"`）に置くと、

- 「ホスト名を追加する」変更が `tofu/` と `kubernetes/` の 2 箇所に分散する
- ArgoCD の同期対象外になり、GitOps の一貫性が崩れる

`local` にすることで、ingress ルールは `kubernetes/infra/cloudflared/` の
ConfigMap として ArgoCD 管理下に入り、**アプリを 1 つ増やす変更が 1 つの PR で完結**する。

代償として、トンネルの認証情報を `credentials.json` の形で Kubernetes Secret に
渡す必要がある。これは OpenTofu が生成し、SOPS で暗号化してコミットする。

> ⚠️ **cloudflared は config.yaml のホットリロードに対応していない。**
> ingress ルールを変更して ArgoCD が ConfigMap を更新しても、それだけでは
> 反映されない。以下で明示的に再起動すること
> （`maxUnavailable: 0` のローリング更新なので無停止で切り替わる）。
>
> ```bash
> kubectl -n cloudflared rollout restart deployment/cloudflared
> kubectl -n cloudflared rollout status deployment/cloudflared
> ```

## 5. Origin 側の JWT 再検証（多層防御）

cloudflared 自身が Access の JWT を検証できる。`config.yaml` にこう書く。

```yaml
ingress:
  - hostname: api.internal.example.com
    service: http://cilium-gateway-external.gateway.svc.cluster.local:80
    originRequest:
      access:
        required: true
        teamName: <your-team-name>
        audTag:
          - <access-application-aud>
```

これにより、**Access アプリケーションの設定が誤って削除された場合でも**、
`Cf-Access-Jwt-Assertion` を持たないリクエストは cloudflared の時点で拒否される。

> Cloudflare Tunnel を経由しないアクセス自体が不可能なので、これは
> 「Cloudflare 側の設定事故」に対する保険である。安全側に倒すために入れる。

## 6. Workers 側の実装

`workers/example-origin-api/` にリファレンス実装を置く。要点のみ。

```ts
const res = await fetch(`https://${env.ORIGIN_HOST}/v1/items`, {
  headers: {
    "CF-Access-Client-Id":     env.CF_ACCESS_CLIENT_ID,
    "CF-Access-Client-Secret": env.CF_ACCESS_CLIENT_SECRET,
  },
});
```

- `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` は
  **`wrangler secret put` で登録する**。`wrangler.toml` の `[vars]` に書かない
  （`[vars]` は平文でダッシュボードに表示され、デプロイ成果物にも含まれる）。
- Workers から自ドメインへの `fetch` はエッジ内で完結するため低レイテンシ。
- **401 が返ったら即座にリトライしない**。Service Token の失効を示すため、
  アラートを上げるべき状態である。

## 7. 公開するサービスの選定基準

「一部のサービス」を公開するにあたり、以下を満たすもののみを対象とする。

1. **認可が必要ないほど無害ではない**（= Access で守る意味がある）
2. **管理平面ではない**（Kubernetes API, Talos API, ArgoCD UI, Proxmox は公開しない）
3. **Gateway の HTTPRoute で公開範囲を明示的に制御できる**
4. **障害時に外部 SaaS 側で degrade できる**（自宅が落ちても SaaS が死なない）

初期構成では `api.internal.example.com` の 1 本のみを公開する。
増やす場合は `kubernetes/infra/cloudflared/configmap.yaml` の `ingress` と
`tofu/20-cloudflare/main.tf` の `var.published_services` に追記する。

## 8. 失敗モードと対処

| 事象 | 症状 | 対処 |
| --- | --- | --- |
| Service Token 失効 | Workers 側で 401 | `tofu apply` で `client_secret_version` をインクリメント → Workers の secret を更新 |
| cloudflared が全レプリカ停止 | Workers 側で 502 | Deployment は 2 レプリカ + PodDisruptionBudget。ノード分散を `topologySpreadConstraints` で強制 |
| Cloudflare 側の設定を誤削除 | 認可なしで到達 | `originRequest.access.required` により cloudflared が拒否（§5） |
| トンネル認証情報の漏洩 | 第三者がトンネルを張れる | `tofu apply` で tunnel を再作成（`tunnel_secret` をローテーション） |
| 自宅の回線断 | Workers 側で 5xx | SaaS 側でタイムアウトとフォールバックを実装する（§6 の設計要件） |
