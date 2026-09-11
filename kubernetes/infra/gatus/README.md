# Gatus — 公開経路の外形監視

## 何を解決しているのか

Prometheus はクラスタの内側から見た状態を測る。Pod は Ready、Service は
Endpoint を持っている——それでも利用者からは落ちて見えることがある。
Tunnel が切れている、Access の設定を壊した、証明書が切れた、DNS が変わった。
どれも「クラスタの中から見る限り正常」なまま起きる。

Gatus はクラスタの中から**外の URL** を叩き、利用者と同じ経路を通す。

```text
DNS → Cloudflare Edge → Access → Tunnel → Gateway → アプリ
```

これにより、経路のどこが壊れても 1 箇所で表面化する。

## ⚠️ これは完全な外部監視ではない

Gatus はこのクラスタの中で動いている。したがって次の場合は**検出も通知も
できない**。Gatus 自身が止まっているからである。

- 自宅の停電
- 回線断
- Kubernetes の全停止

検出できるのは「クラスタは生きているが公開経路が壊れている」場合に限られる。
本当の外部監視が必要になったら、別拠点または外部 VPS に第 2 の
Gatus / heartbeat を置き、最低でも `gateway.craftz.dev` と
`status.tailb6c7d.ts.net` の到達性を確認する。今回の構成には含めていない。

## 構成

| 項目 | 値 |
| --- | --- |
| Namespace | `status`（Pod Security `restricted`） |
| Argo CD Application | `gatus`、sync-wave `7` |
| Helm chart | `twin/gatus` |
| UI | `https://status.tailb6c7d.ts.net/`（Tailscale Ingress、`tag:argocd`） |
| 状態保存 | SQLite on Longhorn RWO PVC 1Gi（`longhorn-retain`） |
| Deployment strategy | `Recreate` |
| メトリクス | `/metrics` を ServiceMonitor で収集 |

UI はインターネットへ公開しない。どのサービスが落ちているかは攻撃者にとって
有用な情報であり、管理 UI と同じ扱いにする。

`Recreate` なのは SQLite が RWO ボリュームにあるためである。`RollingUpdate`
だと新旧 2 つの Pod が同じボリュームを同時に要求し、multi-attach で更新が
止まる。

## Cloudflare Access の認証

Gatus は専用の Service Token `gatus-monitor` を使う。`saas-worker` の
トークンは共有しない。理由は 3 つある。

- 用途が違う。片方を失効させたいときにもう片方を巻き込む。
- Access ログ上で監視の定期アクセスと実トラフィックが区別できなくなる。
- 監視 Pod が侵害されたときに漏れるのが業務 API を叩けるトークンでは困る。

### ⚠️ トークンは health endpoint だけに効くようにする

監視用トークンを既存の Access アプリケーションのポリシーへ足してはならない。
Access アプリケーションは**ホスト名単位**で効くため、それをすると監視トークンが
health endpoint 以外の全パスへ到達できるようになる。監視 Pod が侵害されたとき
に漏れるのは「health endpoint にだけ到達できるトークン」であるべきである。

Access はより具体的なパスのアプリケーションを優先する。したがって
health endpoint だけを対象にした 2 つ目のアプリケーションを作り、そちらに
Gatus のポリシーを置く。

```text
gateway.craftz.dev/ready  → Access app A：gatus-monitor のみ許可
gateway.craftz.dev        → Access app B：業務トークンのみ許可
```

`tofu/20-cloudflare` では `published_services` の `health_path` を指定すると
この 2 つが自動的に作られる（`cloudflare_zero_trust_access_application.gatus_health`）。

ダッシュボードで管理しているホスト名に対して手で設定する場合も、同じ形にすること。

⚠️ cloudflared 側の `originRequest.access.audTag` に **両方の aud** を入れる。
health endpoint へのリクエストは app A が発行した JWT を持つため、片方だけだと
cloudflared がそこだけ弾き、「エッジは通ったのに監視だけ失敗する」状態になる。

なお `gateway.craftz.dev` の `/health` と `/ready` はアプリ側の認証を通らない
（`ai-business-platform/gateway/app/main.py` に `Depends` が無い）。この 2 つを
守っているのは Cloudflare Access だけである。パスを絞る価値はそこにもある。

トークンは `tofu/20-cloudflare` が作る。値の復旧元は macOS Keychain である。

| 値 | Keychain service | account |
| --- | --- | --- |
| Client ID | `dev.craftz.homelab.gatus-cloudflare-access-client-id` | `gatus` |
| Client Secret | `dev.craftz.homelab.gatus-cloudflare-access-client-secret` | `gatus` |

```bash
cd tofu/20-cloudflare
tofu apply

tofu output -raw gatus_service_token_client_id \
  | security add-generic-password -U \
      -s dev.craftz.homelab.gatus-cloudflare-access-client-id -a gatus -w
tofu output -raw gatus_service_token_client_secret \
  | security add-generic-password -U \
      -s dev.craftz.homelab.gatus-cloudflare-access-client-secret -a gatus -w
```

`scripts/bootstrap-cluster-secrets.sh` が Keychain から
`status/gatus-cloudflare-access` Secret を作る。値は標準出力にも一時ファイルにも
出ない。Gatus には `envFrom` で渡り、config 側では `${CF_ACCESS_CLIENT_ID}` /
`${CF_ACCESS_CLIENT_SECRET}` として参照される。Git には平文も暗号文も置かない。

トークンの有効期限は 90 日である。期限切れは監視の停止ではなく
**監視の誤検知**として現れる（Access が 403 を返し、Gatus は「落ちている」と
報告する）ので、`gatus_service_token_secret_version` を上げて
ローテーションしたら Keychain と Secret も更新すること。

## 監視項目

`values.yaml` の `config.endpoints` が唯一の定義である。

現在の対象は `gateway.craftz.dev` の `/ready`。`/health` ではないのは、
`/health` が「プロセスが生きている」だけを返すのに対し、`/ready` は
PostgreSQL へ実際にクエリを投げるためである。DB が落ちた Gateway を
healthy と報告してしまっては監視の意味が無い。

条件はステータスコードだけでなく応答 JSON の中身まで見る。

```yaml
- "[STATUS] == 200"
- "[BODY].status == ready"
- "[RESPONSE_TIME] < 3000"
- "[CERTIFICATE_EXPIRATION] > 168h"
```

### 監視対象を増やすとき

`config.endpoints` と `namespace-and-policy.yaml` の `toFQDNs` を
**同じ PR で** 更新する。片方だけだと NetworkPolicy に阻まれ、監視は
「対象が落ちている」と無言で誤報し続ける。

- `published_services`（Access あり）: Service Token ヘッダを付けて監視する
- `public_services`（Access なし）: ヘッダを付けずに監視する
- 書き込みを伴う API を通常の endpoint にしない
- 業務フローの監視が必要なら、破棄可能なテストデータだけを使う Gatus suite
  として別に設計する

## NetworkPolicy

`status` namespace は default-deny で、許可は次だけである。

| 方向 | 許可 |
| --- | --- |
| Ingress | `tailscale` namespace の proxy から TCP 8080 |
| Ingress | `monitoring` namespace の Prometheus から TCP 8080 |
| Ingress | host / remote-node から liveness/readiness TCP 8080 |
| Egress | kube-system CoreDNS TCP/UDP 53（DNS proxy 経由） |
| Egress | 監視対象の公開 FQDN へ TCP 443 |

⚠️ `status` は `clusterwide-egress-deny.yaml` の `endpointSelector` から
**除外してある**。あの CCNP は宅内 CIDR を deny する一方、副作用として
`toEntities: all`（それ以外どこへでも許可）を全対象 namespace に与える。
対象のままだと、上の egress 許可リストは公開 IP に対して意味を持たない。
除外した結果、宅内 CIDR への deny という保険もこの namespace には無いので、
egress ルールを足すときは宛先が宅内やクラスタ内を指していないことを
必ず確認すること。

`toFQDNs` は Cilium の DNS proxy が DNS 応答を観測して初めて機能する。
CoreDNS への許可に `rules.dns` が付いているのはそのためで、これを外すと
名前解決はできても HTTPS が通らなくなる。

## 通知

Alertmanager の外部 receiver は未設定である。存在しない通知先を設定しても
「送ったつもりで届いていない」状態を作るだけなので、今回は入れていない。
現状は Gatus UI と Grafana で結果を確認する。通知先が決まったら Webhook を
Keychain Secret として追加し、`config.alerting` を設定する。

## 確認

```bash
kubectl -n status get pods,pvc,ingress
kubectl -n status logs deploy/gatus --tail=50

# UI（Tailnet 内から）
curl -fsS https://status.tailb6c7d.ts.net/health

# Prometheus が収集できているか
kubectl -n monitoring exec sts/prometheus-kube-prometheus-stack-prometheus -c prometheus -- \
  wget -qO- 'http://localhost:9090/api/v1/targets?state=active' \
  | grep -o '"job":"gatus"[^}]*"health":"[a-z]*"'
```

`envFrom` に chart の ConfigMap が含まれるため、起動時に
`Keys [config.yaml] ... were skipped since they are considered invalid
environment variable names` という Event が出る。chart 側の仕様であり、
設定は `/config/config.yaml` としてボリュームから読まれている。動作に影響は無い。

## ロールバック

Argo CD Application、Homepage のリンク、Access policy を戻す。PVC は
`longhorn-retain` のため残る。データを消す必要がある場合だけ、対象 PVC を
明示して手動で削除する。Cloudflare 側ではトークンを revoke し、Keychain の
コピーも削除する。
