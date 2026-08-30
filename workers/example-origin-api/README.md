# example-origin-api — Workers から自宅サービスを呼ぶサンプル

Cloudflare Workers にデプロイした SaaS から、Cloudflare Access で保護された
自宅 Kubernetes のサービスを安全に呼び出すリファレンス実装。

設計は [docs/40-external-access.md](../../docs/40-external-access.md) を参照。

## セットアップ

```bash
npm install

# Access Service Token を Secret として登録する
# ⚠️ wrangler.jsonc の vars に書かないこと
wrangler secret put CF_ACCESS_CLIENT_ID
wrangler secret put CF_ACCESS_CLIENT_SECRET
```

値は OpenTofu の output から取得する。

```bash
cd ../../tofu/20-cloudflare
tofu output -raw service_token_client_id
tofu output -raw service_token_client_secret
```

`wrangler.jsonc` の `ORIGIN_HOST` を実際の公開ホスト名に変更してからデプロイする。

```bash
wrangler deploy
```

## 動作確認

```bash
# Worker 経由（Service Token が付くので通る）
curl https://example-origin-api.<subdomain>.workers.dev/api/healthz

# 直接アクセス（トークンが無いので 401 が返るはず）
curl -s -o /dev/null -w '%{http_code}\n' https://api.internal.example.com/healthz
# → 401 であること。200 が返ったら Access の設定を見直すこと。
```

## 実装上の要点

| 項目 | 理由 |
| --- | --- |
| **401/403 でリトライしない** | Service Token の失効を示す。再試行しても結果は変わらず、障害の検知が遅れるだけ |
| **8 秒でタイムアウト** | 自宅の回線断や Ceph の遅延で Worker が待ち続けると、SaaS 全体のレスポンスが劣化する |
| **503 で degrade する** | 自宅は SLA を持たない。自宅の障害が SaaS 全体の停止に直結しない設計にする |
| **Secret の未設定を起動時に検出** | 設定漏れがあると全リクエストが 401 になり、原因の切り分けに時間を取られる |

## Service Token のローテーション

Service Token には有効期限がある（既定 90 日）。期限切れの前に更新する。

```bash
cd ../../tofu/20-cloudflare
# terraform.tfvars の service_token_secret_version をインクリメント
tofu apply

# 新しい値を Worker に登録し直す
cd ../../workers/example-origin-api
wrangler secret put CF_ACCESS_CLIENT_SECRET
```

有効期限は `tofu output service_token_expires_at` で確認できる。
