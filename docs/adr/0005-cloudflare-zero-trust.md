# ADR-0005: 外部公開に Cloudflare Tunnel + Access（Service Token）を使う

- **状態**: 承認済み
- **日付**: 2026-08-30

## 背景

要件は「Cloudflare Worker にデプロイした SaaS から安全にアクセスできるように
したい」である。呼び出し元が Cloudflare Workers であることが確定しているため、
Cloudflare のエコシステム内で完結させられる。

## 検討した選択肢

| 方式 | インバウンド開放 | グローバル IP 露出 | 認可 | 監査ログ | 判定 |
| --- | --- | --- | --- | --- | --- |
| ポートフォワード + リバースプロキシ + Basic 認証 | **必要** | **する** | 弱（総当たり可能） | 自前 | ✗ 論外 |
| ポートフォワード + mTLS | 必要 | する | 強 | 自前 | ✗ 開放が残る |
| WireGuard VPN を Workers から | — | — | — | — | ✗ Workers は任意 UDP を出せず**技術的に不可能** |
| Tailscale Funnel | 不要 | しない | 中 | 限定的 | △ Workers 側からの細かい認可制御が弱い |
| Cloudflare Spectrum | 不要 | しない | 中（L4 のみ） | あり | ✗ HTTP 層の認可ができない。Enterprise 前提 |
| **Tunnel + Access + Service Token** | **不要** | **しない** | **強** | **あり** | ★採用 |
| Tunnel + Access + mTLS クライアント証明書 | 不要 | しない | 強 | あり | △ 後述 |

## 決定

**Cloudflare Tunnel + Cloudflare Access（Service Token 認可）** を採用する。

### 決め手

1. **インバウンドのポート開放が完全にゼロになる**
   cloudflared は自宅から Cloudflare エッジへ outbound の QUIC/443 を張る。
   自宅のグローバル IP に対して外部から叩けるものが何も存在しない状態になる。
   これは脅威 T1（インターネットからの直接侵入）を**構造的に消す**。

2. **Service Token が「非人間 ID」として設計されている**
   `decision = "non_identity"` のポリシーは、人間のログインを前提としない
   マシン間通信のために用意されている。Workers から
   `CF-Access-Client-Id` / `CF-Access-Client-Secret` ヘッダを付けるだけで、
   Cloudflare エッジが認可を判定する。**自前で認証機構を実装しない**ことが
   最大の利点である（自作認証はバグの温床）。

3. **`service_auth_401_redirect = true`**
   認可失敗時に IdP のログイン画面へ 302 するのではなく 401 を返す。
   API クライアントにとって正しい挙動であり、リダイレクトループを防ぐ。

4. **ローテーションが宣言的にできる**
   `client_secret_version` をインクリメントして `tofu apply` するだけで
   シークレットが更新される。`duration` で有効期限も設定できる。

5. **監査ログが Cloudflare 側に残る**
   誰（どの Service Token）がいつどのアプリにアクセスしたかが記録される。
   自前で用意すると相応の実装量になる。

### mTLS クライアント証明書を採用しなかった理由

Access の mTLS は強力だが、**Workers から mTLS で送信するには
Cloudflare の mTLS binding（`mtls_certificate` バインディング）を別途構成する
必要があり**、証明書のライフサイクル管理（発行・更新・失効）が増える。

Service Token でも「その SaaS だけが呼べる」という要件（R3）は満たせるため、
運用複雑度に見合わないと判断した。

ただし、**将来的に扱うデータの機密度が上がった場合は mTLS への移行を検討する**。
その際は本 ADR を Superseded とし、新しい ADR を起こす。

## 多層防御: Origin 側での JWT 再検証

Cloudflare Access を通過したリクエストには `Cf-Access-Jwt-Assertion` ヘッダが
付与される。cloudflared の `originRequest.access` を有効にすると、
**cloudflared 自身がこの JWT の署名と `aud` を検証**する。

```yaml
originRequest:
  access:
    required: true
    teamName: <team>
    audTag: [<aud>]
```

これは「Cloudflare の設定を誤って削除した」「Access アプリケーションの
ドメイン指定を間違えた」といった**自分自身の設定事故**に対する保険である。
設計原則 2「境界での遮断より、多層防御を前提とする」に従って有効化する。

## Tunnel の設定管理を `local` にする理由

[docs/40-external-access.md](../40-external-access.md) §4 に詳述。要約すると、
ingress ルール（ホスト名 → Service のマッピング）は Kubernetes 側の関心事であり、
ArgoCD 管理下の ConfigMap に置くことで「アプリを 1 つ増やす」変更が
1 つの PR で完結する。

## 受け入れるリスク

| リスク | 評価 |
| --- | --- |
| Cloudflare への依存 | 経路全体が Cloudflare に依存する。ただし呼び出し元も Workers であり、既に依存している。追加の依存は生じない |
| Cloudflare の障害 | 自宅サービスに到達できなくなる。SaaS 側でタイムアウトとフォールバックを実装することを設計要件とする |
| Service Token の漏洩 | Workers の secret として保管し、`wrangler secret put` で登録する。漏洩時は `tofu apply` で即座にローテーション可能 |
