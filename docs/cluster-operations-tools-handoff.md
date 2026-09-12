# Hubble UI・Renovate・Gatus 導入設計／Claude Code 引き継ぎ

更新日: 2026-09-12  
対象リポジトリ: `craftzdev/homelab`  
対象クラスタ: Talos Kubernetes（control-plane 3台 + worker 3台）

## 1. 目的

次の3項目を、既存のGitOps・ゼロトラスト・完全再構築方針を崩さず導入する。

1. 既に稼働しているHubble UIをTailscale経由で恒久公開し、Homepageへ追加する
2. Renovateを既存のARCセルフホストRunnerで毎週実行する
3. Gatusを導入し、Cloudflare Tunnel経由の公開サービスを外形監視する

本書はClaude Codeが設計判断をやり直さず、実装・検証・コミットまで進めるための
引き継ぎ資料である。

## 2. 現在の状態

2026-09-12にGit上のdesired stateと稼働中クラスタを確認した結果は次のとおり。

| 項目 | 現状 |
| --- | --- |
| Cilium / Hubble Relay / Hubble UI | 稼働済み。Hubble UI Serviceは`kube-system/hubble-ui:80` |
| Hubble UIの公開 | port-forwardのみ。恒久URLなし |
| Tailscale Operator | 稼働済み。管理UIは`tag:argocd`と`restricted-userspace`を使用 |
| Homepage | `https://portal.tailb6c7d.ts.net/`で稼働中 |
| ARC Runner | `homelab-runner`。ephemeral、`minRunners: 0`、`maxRunners: 1` |
| ARC Runnerのコンテナ実行環境 | Dockerデーモンなし。Podごとに使い捨て |
| Renovate設定 | ルートの`renovate.json`は存在するが、実行Workflowは存在しない |
| 依存関係更新 | 手動。Renovate設定だけではPRは作られない |
| Prometheus / Alertmanager / Grafana | 稼働済み。Alertmanagerの外部通知先は未設定 |
| Gatus / Uptime Kuma | 未導入 |
| Cloudflare公開経路 | Cloudflare Access + Tunnel + Gateway。`gateway.craftz.dev`を使用 |
| Secret復旧元 | 原則macOS Keychain。平文SecretをGitへ置かない |

## 3. 採用方針

### 3.1 全体構成

```text
管理者 ── Tailnet HTTPS ──┬── portal.tailb6c7d.ts.net  (Homepage)
                          ├── hubble.tailb6c7d.ts.net  (Hubble UI)
                          └── status.tailb6c7d.ts.net  (Gatus UI)

GitHub schedule/manual dispatch
        │
        ▼
ARC homelab-runner ── Renovate ── 更新PR ── 既存validate CI

Gatus Pod
   │ 公開FQDNへHTTPS + Cloudflare Access Service Token
   ▼
Cloudflare Edge ── Tunnel ── Gateway / 公開アプリ
   │
   └── 結果をGatus UIとPrometheus/Grafanaへ記録
```

### 3.2 共通原則

- 管理UIはインターネットへ公開せず、Tailscale Ingressだけで公開する。
- 新規リソースはGitを正とし、Argo CDで同期する。
- 適用のための恒久的な手動`kubectl apply`は禁止する。
- SecretはGitへ平文・暗号文とも置かず、既存方針どおりmacOS Keychainから
  `scripts/bootstrap-cluster-secrets.sh`で復元する。
- イメージ、Helm chart、GitHub Action、npm packageは具体的なバージョンまたは
  commit SHAへ固定し、Renovateで更新する。
- `latest`、無制限なRBAC、ServiceAccount tokenの自動マウントを使用しない。
- workerノードへ配置し、control-planeへアプリ負荷を載せない。
- 既存の無関係な未コミット変更を上書き・取り消し・同梱しない。

## 4. Hubble UIのTailscale公開

### 4.1 決定

Hubble UI自体は既にCilium chartからデプロイされているため、新しいHubbleを
導入しない。Tailscale IngressとHomepageリンクだけを追加する。

公開URL:

```text
https://hubble.tailb6c7d.ts.net/
```

### 4.2 Kubernetes設計

`kubernetes/infra/tailscale-operator/hubble-ingress.yaml`を追加する。

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: hubble-ui
  namespace: kube-system
  annotations:
    tailscale.com/tags: tag:argocd
    tailscale.com/proxy-class: restricted-userspace
spec:
  ingressClassName: tailscale
  defaultBackend:
    service:
      name: hubble-ui
      port:
        name: http
  tls:
    - hosts:
        - hubble
```

`kubernetes/apps/infrastructure.yaml`にあるTailscale Operator Applicationの
`directory.include`へ`hubble-ingress.yaml`を追加する。

既存の管理UIと同じ`tag:argocd`を使う。Tailnet Grantsでは、管理者から
`tag:argocd`のTCP 443だけを許可し、一般端末や他のワーカーからは許可しない。
ACL/Grantsは外部状態のため、既存ルールがこの条件を満たすことを確認する。

Hubble UIのServiceはClusterIPのままとする。Ciliumの`hubble.ui.ingress`は
有効化しない。公開経路を二重化しないためである。

### 4.3 Homepage

`kubernetes/infra/homepage/services.yaml`の`Kubernetes`グループへ追加する。

```yaml
- Hubble:
    icon: cilium.png
    href: https://hubble.tailb6c7d.ts.net/
    description: Cilium network flows and policy verdicts
```

### 4.4 再構築と検証

`scripts/rebuild-talos-cluster.sh`へ次を追加する。

- `TAILSCALE_HUBBLE_FQDN`。既定値は`hubble.tailb6c7d.ts.net`
- 復旧後のHTTPS確認
- Tailscale Operatorが生成したproxy StatefulSetのReady確認

完了条件:

- Argo CDの`tailscale-operator`と`homepage`が`Synced/Healthy`
- `https://hubble.tailb6c7d.ts.net/`を許可されたTailnet端末から表示できる
- Tailnet外から直接到達できない
- Hubble UIでflowが表示される
- Homepageのアイコンとリンクが正しく表示される
- 既存のport-forward手順も障害時の代替経路として残る

## 5. Renovateの週次実行

### 5.1 決定

Renovate専用の常駐PodやCronJobは作らない。GitHubのscheduleを起点に、既存の
ephemeral ARC Runner `homelab-runner`で実行する。GitHub-hosted Runnerの時間は
消費しない。

公式`renovatebot/github-action`は内部でDockerコンテナを起動するため、
Dockerデーモンを持たない現在のRunnerでは使用しない。Node.jsをセットアップし、
バージョン固定したRenovate npm packageを直接実行する。

### 5.2 Workflow設計

追加ファイル:

```text
.github/workflows/renovate.yaml
```

トリガー:

- 毎週土曜日10:00 JST（`01:00 UTC`）
- `workflow_dispatch`
- 手動実行時は`dry_run`入力を用意する

実行条件:

```yaml
runs-on: homelab-runner
timeout-minutes: 30
```

必要な設定:

```text
RENOVATE_PLATFORM=github
RENOVATE_REPOSITORIES=["craftzdev/homelab"]
RENOVATE_ONBOARDING=false
RENOVATE_REQUIRE_CONFIG=required
LOG_LEVEL=info
```

`renovate.json`は対象リポジトリ内のrepository configとしてRenovate自身に検出させる。
ローカルbot configを指す`RENOVATE_CONFIG_FILE`には指定しない。手動dry-runを既存の
更新時間帯以外に行う場合だけ、CLIの高優先度設定で`schedule=at any time`を一時的に
上書きし、通常の週次実行ではrepository configのscheduleをそのまま使う。

実装時点の最新版を確認し、Renovate本体を具体的なバージョンへ固定する。本書作成時に
確認した上流最新版は`44.82.0`だが、実装開始時に再確認すること。固定値自身も
Renovateが更新できるよう、`renovate.json`へWorkflow内バージョン用のregex managerを
追加する。

`renovate.json`の既存スケジュールは土曜日09:00〜18:00 JSTなので、10:00 JSTの
Workflowと一致する。通常更新は自動マージしない。脆弱性更新もPR作成までとし、
既存CIと人間の承認を通す。

### 5.3 GitHub認証

`GITHUB_TOKEN`で作ったPRは後続Workflowの起動に制約があるため、Renovate専用の
GitHub Appを使う。ARC Controller用GitHub Appの秘密鍵は再利用しない。

推奨するGitHub App名:

```text
homelab-renovate
```

対象は`craftzdev/homelab`だけに限定し、必要最小限のRepository permissionを与える。

| 権限 | 設定 |
| --- | --- |
| Metadata | Read-only |
| Contents | Read and write |
| Pull requests | Read and write |
| Issues | Read and write（Dependency Dashboard用） |
| Workflows | Read and write（Actions更新PR用） |
| Commit statuses / Checks | Read-only |

Repository Actions secrets:

```text
RENOVATE_APP_ID
RENOVATE_APP_PRIVATE_KEY
```

Workflow内で`actions/create-github-app-token`をcommit SHAまたは検証済みmajorへ固定して
installation tokenを生成し、その短期tokenを`RENOVATE_TOKEN`へ渡す。秘密鍵やtokenを
ログ出力しない。GitHub Appを直ちに用意できない場合だけfine-grained PATを暫定利用し、
恒久設計にはしない。

### 5.4 同時実行と負荷制御

- Workflowの`concurrency.group`は`renovate-homelab`
- `cancel-in-progress: false`
- `homelab-runner`は`maxRunners: 1`を維持する
- Renovate実行中に通常CIが待つことは許容する
- PR同時数は既存`renovate.json`へ上限を追加する
  - `prConcurrentLimit: 5`
  - `branchConcurrentLimit: 5`
- patch groupingを維持し、PR乱立を避ける
- major、Talos、Kubernetes、Cilium、Longhornは個別PR・自動マージ禁止

既存設定の`ceph-csi`は廃止済みなので、CNI/CSIルールを`cilium`と`longhorn`へ
更新する。TalosとKubernetesの互換性確認ラベルは維持する。

### 5.5 検証

1. 最初は手動`dry_run`で対象dependencyと予定branchだけを確認する
2. 通常モードを手動実行する
3. Dependency Dashboard Issueが作成または更新されることを確認する
4. 更新PRが作成されることを確認する
5. そのPRで既存`validate` Workflowが起動することを確認する
6. Runner Podが処理後に消え、idle Podが0へ戻ることを確認する
7. token、秘密鍵、認証ヘッダがログへ出ていないことを確認する
8. 次回scheduleが土曜日10:00 JSTになっていることをGitHub UIで確認する

## 6. GatusによるCloudflare公開経路の監視

### 6.1 決定

Gatusを採用する。設定をYAMLでGit管理でき、現在のArgo CDによる完全再現方針と
整合するためである。Gatusの管理画面自体はインターネットへ公開しない。

公開URL:

```text
https://status.tailb6c7d.ts.net/
```

Gatusはクラスタ内から公開FQDNへアクセスする。これにより、単なるServiceの生存ではなく
次の利用者経路を一括して検査する。

```text
DNS → Cloudflare Edge → Access → Tunnel → Gateway → アプリ
```

### 6.2 配置

| 項目 | 設計値 |
| --- | --- |
| Namespace | `status` |
| Argo CD Application | `gatus`、sync-wave `7` |
| Helm repository | `https://twin.github.io/helm-charts` |
| Helm chart | `gatus`。実装時の最新版へ固定 |
| Replica | 1 |
| 配置先 | `homelab.craftz.dev/workload-plane: "true"` |
| Service | ClusterIP |
| UI公開 | Tailscale Ingress、`tag:argocd` |
| 状態保存 | SQLite、Longhorn RWO PVC 1Gi |
| Deployment strategy | `Recreate`（SQLiteのRWO multi-attach回避） |
| ServiceAccount token | 自動マウント禁止 |
| Prometheus | `/metrics`をServiceMonitorで収集 |

追加・変更する主なファイル:

```text
kubernetes/infra/gatus/values.yaml
kubernetes/infra/gatus/namespace-and-policy.yaml
kubernetes/infra/gatus/README.md
kubernetes/apps/infrastructure.yaml
kubernetes/apps/project.yaml
kubernetes/infra/homepage/services.yaml
scripts/bootstrap-cluster-secrets.sh
scripts/rebuild-talos-cluster.sh
```

AppProjectへ次を追加する。

- source repo `https://twin.github.io/helm-charts`
- destination namespace `status`

### 6.3 Podセキュリティ

NamespaceにはPod Security `restricted`を強制する。Helm valuesは少なくとも次を満たす。

```yaml
serviceAccount:
  create: false
  autoMount: false

podSecurityContext:
  fsGroup: 65534

securityContext:
  runAsNonRoot: true
  runAsUser: 65534
  runAsGroup: 65534
  readOnlyRootFilesystem: true
```

リソース初期値:

```yaml
resources:
  requests:
    cpu: 25m
    memory: 64Mi
  limits:
    cpu: 250m
    memory: 256Mi
```

本番値は稼働後のPrometheus実測で調整する。

### 6.4 NetworkPolicy

`status` namespaceはdefault-denyとし、次だけを許可する。

| 方向 | 許可 |
| --- | --- |
| Ingress | `tailscale` namespaceのproxyからGatus TCP 8080 |
| Ingress | `monitoring` namespaceのPrometheusからTCP 8080 |
| Ingress | host / remote-nodeからliveness/readiness TCP 8080 |
| Egress | kube-system CoreDNS TCP/UDP 53 |
| Egress | 監視対象の公開FQDNへTCP 443 |
| Egress | 採用後の外部通知先FQDNへTCP 443 |

自宅LAN、Proxmox管理API、Kubernetes API、他namespaceへの汎用egressは許可しない。
監視対象を増やす際はGatus configとFQDN egress許可を同じPRで更新する。

### 6.5 Cloudflare Access用の認証

既存`saas-worker` Service TokenをGatusと共有しない。Cloudflare上に専用の
Service TokenをOpenTofuで作る。

```text
名前: gatus-monitor
用途: Access配下のhealth endpointをGETするだけ
```

`tofu/20-cloudflare`で以下を管理する。

- `cloudflare_zero_trust_access_service_token.gatus_monitor`
- Gatus tokenだけを許可する`non_identity` policy
- 各`published_services` Access Applicationへのpolicy追加
- client ID / secret / expiryのoutput（secretは`sensitive = true`）

Secretの復旧元:

| 値 | Keychain service | account |
| --- | --- | --- |
| Client ID | `dev.craftz.homelab.gatus-cloudflare-access-client-id` | `gatus` |
| Client Secret | `dev.craftz.homelab.gatus-cloudflare-access-client-secret` | `gatus` |

`scripts/bootstrap-cluster-secrets.sh`はKeychainから読み、標準出力や一時ファイルを介さず
次のSecretを作る。

```text
namespace: status
name: gatus-cloudflare-access
keys:
  CF_ACCESS_CLIENT_ID
  CF_ACCESS_CLIENT_SECRET
```

Gatus Podへ`envFrom.secretRef`で注入し、configでは環境変数を参照する。

```yaml
headers:
  CF-Access-Client-Id: "${CF_ACCESS_CLIENT_ID}"
  CF-Access-Client-Secret: "${CF_ACCESS_CLIENT_SECRET}"
```

### 6.6 初期監視項目

最初の監視対象は`gateway.craftz.dev`とする。health endpointの正確なパスとJSONは
Gateway実装を確認し、単にトップページの`200`を見るだけにしない。

基本条件:

```yaml
- "[STATUS] == 200"
- "[RESPONSE_TIME] < 3000"
- "[CERTIFICATE_EXPIRATION] > 168h"
```

health endpointがJSONを返す場合は、サービス固有の健全性フィールドも検証する。
認証なしのチェックも別に行い、Accessが`401`を返すことを確認する。ただし正常時に
常時401を発生させる監視を入れるとノイズになるため、これはデプロイ検証用とする。

公開サービス追加時のルール:

- `published_services`: Access Service Tokenヘッダ付きで監視
- `public_services`: 認証ヘッダなしで監視
- 書き込みを伴うAPIは通常endpointにしない
- 業務フロー監視が必要なら、破棄可能なテストデータだけを使うGatus suiteとして
  別設計する

### 6.7 保存、メトリクス、通知

- `metrics: true`
- SQLiteを`/data/data.db`へ保存
- PVCは`longhorn-retain` 1Gi
- 結果保持数は無制限にせず、既定値または明示した上限を使う
- ServiceMonitorへ`release: kube-prometheus-stack`ラベルを付ける
- Homepageの`Kubernetes`または`Monitoring`グループへGatusを追加する

Alertmanagerは現在外部receiverが未設定なので、今回の実装で架空の通知先を設定しない。
まずGatus UIとGrafanaで結果を確認できる状態までを必須範囲とする。通知先が決まったら
Webhook等をKeychain Secretとして追加する。

### 6.8 重要な制約

クラスタ内GatusはTunnel、Access、アプリ単位の障害を検出できるが、自宅全体の停電、
回線断、Kubernetes全停止ではGatus自身も止まり通知できない。したがってこれは完全な
外部監視ではない。

将来、別拠点または外部VPSに第2のGatus/heartbeat監視を置き、最低でも
`gateway.craftz.dev`と`status.tailb6c7d.ts.net`の到達性を確認する。これは今回の
必須範囲外とする。

### 6.9 Gatus完了条件

- Argo CDの`gatus`が`Synced/Healthy`
- Gatus Podがworkerノードで稼働する
- `https://status.tailb6c7d.ts.net/`がTailnet内だけで表示できる
- `gateway.craftz.dev`がGatus上でhealthyになる
- Gatusが専用Service TokenでCloudflare Accessを通過している
- tokenなしの外部リクエストは`401`となる
- Prometheus TargetsでGatusがUPになる
- GatusのメトリクスをGrafana Exploreから確認できる
- NetworkPolicy適用後もDNS、公開HTTPS、probe、Prometheus scrapeだけが成功する
- HomepageからGatusへ遷移できる
- Gatusを削除・再作成してもGitとKeychainから復元できる

## 7. 実装順序

依存関係と切り戻しやすさを考慮し、次の順に分ける。

### Phase 1: Hubble UI

- [ ] Tailscale Ingressを追加
- [ ] Tailscale Operator Applicationのincludeを更新
- [ ] Homepageへリンクを追加
- [ ] Tailnet内外の到達性を検証
- [ ] 再構築スモークテストへ追加

### Phase 2: Renovate

- [ ] Renovate専用GitHub Appを作成・対象repoへinstall
- [ ] Actions secretsを登録
- [ ] `.github/workflows/renovate.yaml`を追加
- [ ] `renovate.json`の上限、Longhornルール、自己更新managerを追加
- [ ] dry-runを確認
- [ ] 実PRと既存CI起動を確認
- [ ] scheduleを確認

### Phase 3: Gatus

- [ ] OpenTofuへGatus専用Cloudflare Access Service Tokenとpolicyを追加
- [ ] tokenをKeychainへ保存する安全な同期手順を追加
- [ ] `status` namespaceとNetworkPolicyを追加
- [ ] Gatus Helm valuesとArgo CD Applicationを追加
- [ ] `bootstrap-cluster-secrets.sh`へSecret復元を追加
- [ ] Gateway health checkを設定
- [ ] Prometheus ServiceMonitorを有効化
- [ ] Tailscale Ingressを有効化
- [ ] Homepageへ追加
- [ ] 正常・認証失敗・経路障害を検証
- [ ] 再構築スクリプトと運用手順を更新

## 8. CIと静的検証

実装PRでは少なくとも次を通す。

```text
yamllint
shellcheck
kustomize build（KSOPS対象を除く既存ルール）
Helm templateによるGatus chart描画
tofu fmt -check
tofu validate
gitleaks
```

`.github/scripts/check-helm-charts.py`の対象へGatusを加える。AppProjectの許可リソースを
追加する必要がある場合は、chartを描画して実際に必要なkindだけを列挙する。ワイルド
カード許可へ変更してはならない。

## 9. ロールバック

### Hubble UI

IngressとHomepageリンクを戻す。Hubble UI本体はCiliumの可視化に使うため削除しない。

### Renovate

Workflowを無効化または削除し、GitHub Appをsuspendする。既に作られたPRやDependency
Dashboardは自動では消えないため、必要に応じて手動でcloseする。

### Gatus

Argo CD Application、Homepageリンク、Access policyを戻す。SQLite PVCは
`longhorn-retain`により残るため、データ削除が必要な場合だけ対象PVCを明示して手動削除
する。Gatus tokenはCloudflare側でrevokeし、Keychainコピーも削除する。

## 10. Claude Codeへの最終指示

- 3 Phaseを別々の論理コミットにする。
- 各Phaseは実装、静的検証、実環境反映、疎通確認まで完了してから次へ進む。
- GitHub App作成、Tailnet Grants変更、通知先選択など、人間の操作が必要な箇所だけを
  明示して停止する。それ以外はGitOpsで自動化する。
- 秘密値をコマンド引数、ログ、Git diff、一時ファイルへ出さない。
- Argo CDの`Synced`だけで完了扱いにせず、`Healthy`と利用者経路のHTTPS確認を行う。
- 最後に`rebuild-talos-cluster.sh`の復旧後検証へHubbleとGatusを含め、クラスタ全体の
  ワンコマンド再現性を維持する。
