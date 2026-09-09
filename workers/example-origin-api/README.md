# example-origin-api — Workers から自宅サービスを呼ぶサンプル

Cloudflare Workers にデプロイした SaaS から、Cloudflare Access で保護された
自宅 Kubernetes のサービスを安全に呼び出すリファレンス実装。

設計は [docs/40-external-access.md](../../docs/40-external-access.md) を参照。

## ⚠️ 最重要: この Worker は「Access の代理」になりうる

Cloudflare Access が認証しているのは **この Worker** であって、
**Worker の利用者** ではありません。

つまり、Worker 側で呼び出し元を認証しなければ、
`https://<name>.<subdomain>.workers.dev/api/...` を知っている
**インターネット上の任意の第三者が Access を通過できます**。
自宅のサービスが実質的に無認証で公開されるのと同じです。

この実装では以下で対処しています。

| 対策 | 内容 |
| --- | --- |
| 呼び出し元の認証 | `Authorization: Bearer <CLIENT_API_KEY>` を検証。タイミング攻撃に耐える比較を使用 |
| メソッドの allowlist | GET / HEAD / POST のみ転送。それ以外は 405 |
| 設定漏れの検出 | Secret が未設定なら 500 を返して即座に気づけるようにする |

> **本番では共有シークレットで十分か検討してください。** 利用者ごとの
> JWT 検証、mTLS、レート制限などが必要な場合があります。
> ここでは「認証を入れる場所」を示す最小実装に留めています。

## セットアップ

```bash
npm install

# 1) 呼び出し元認証用のキーを生成して登録する（必須）
openssl rand -base64 32          # 生成した値を控える
wrangler secret put CLIENT_API_KEY

# 2) Access Service Token を Secret として登録する
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
WORKER="https://example-origin-api.<subdomain>.workers.dev"

# 1) 呼び出し元キー無し → 401 であること
#    ⚠️ ここで 200 が返るなら、この Worker は誰でも使える
#       Access の代理になっている。直ちに修正すること。
curl -s -o /dev/null -w '%{http_code}\n' "$WORKER/api/healthz"

# 2) 正しいキー付き → 通ること
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $CLIENT_API_KEY" "$WORKER/api/healthz"

# 3) 許可していないメソッド → 405 であること
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE \
  -H "Authorization: Bearer $CLIENT_API_KEY" "$WORKER/api/healthz"

# 4) オリジンへ直接アクセス → 401 であること
#    200 が返ったら Access の設定を見直すこと。
curl -s -o /dev/null -w '%{http_code}\n' https://api.internal.example.com/healthz
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
