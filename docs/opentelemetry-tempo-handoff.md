# point-guide-scraper OpenTelemetry / Tempo 設計・実装引き継ぎ

Claude Code は最初にこの文書と関連ファイルを読み、既存クラスタ、MinIO、Loki、Grafana、
point-guide のデータベースを削除・再作成せずに作業すること。

## 1. 目的

`point-guide-scraper` のサイト別CronJobについて、サイト単位の成否だけでなく、
ブラウザ初期化、プロキシ確認、カテゴリ巡回、ページ取得、抽出、検証、DB保存のどこで
遅延・失敗したかをGrafanaから追跡できるようにする。

OpenTelemetryはスクレイピング結果の保存先にはしない。案件名、条件、ポイント、URL等の
業務データの正は引き続きCloudNativePG上のPostgreSQLとする。

## 2. 現在の状態

- `point-guide-scraper` はNode.js / TypeScript / Playwrightで実装されている。
- 17サイトを個別のKubernetes CronJobとして毎日実行する。
- 各ジョブは最大3時間、同一サイトの重複実行は禁止されている。
- `src/harness` はサイト単位の成功、失敗、タイムアウト、試行回数、実行時間を集計する。
- スクレイピング結果は`point-guide/point-guide-postgres-rw:5432`へTLS接続して保存する。
- Namespaceはdefault-denyで、外部通信はWebShare、IP確認、Discord、PostgreSQLだけを許可する。
- ログは`alloy-logs` DaemonSetからLokiへ送り、Grafanaで確認できる。
- 現在の`alloy-logs`は`point-guide-scraper` Namespaceを`application`に分類していないため、
  ログ相関を実装するPRで分類も修正する。
- トレースバックエンドとOTLP受信口はまだ存在しない。
- MinIOは`logging` Namespaceの単一Podで、Longhorn 2 replicaの100GiB PVCを使う。
- GrafanaはTailnet内だけに公開されている。Tempoを直接公開してはならない。

## 3. 採用方針

### 3.1 構成

```text
point-guide-scraper CronJob
  OpenTelemetry JS SDK
          |
          | OTLP/HTTP :4318
          v
alloy-otel Deployment (logging, 1 replica)
  memory limiter -> redaction/filter -> batch
          |
          | OTLP/gRPC :4317
          v
Tempo monolithic StatefulSet (logging, 1 replica)
  WAL: Longhorn PVC
  blocks: MinIO tempo-traces bucket
          |
          | query HTTP :3200
          v
Grafana Tempo datasource (monitoring)
          |
          +---- trace_id ---- Loki logs
```

### 3.2 判断

| 項目 | 採用 | 理由 |
| --- | --- | --- |
| 計装 | OpenTelemetry JS SDK | ベンダーに依存しない標準で、後からバックエンドを変更できる |
| Collector | 専用`alloy-otel` Deployment | 既存Alloyを再利用しつつ、ログ用DaemonSetと障害・負荷を分離する |
| Backend | Tempo monolithic 1 replica | 現在の低い取り込み量に分散構成は過剰。Kafkaも不要 |
| Trace保存 | 既存MinIOの専用bucket | ローカルディスクだけより再配置・復元に強い |
| 可視化 | 既存Grafana | 新しい管理UIや公開URLを増やさない |
| 初期sampling | 100% | 17サイトの日次実行で量が少なく、導入直後は未知の失敗を落とさない |
| metrics-generator | 初期は無効 | Prometheus remote-write追加と余分なメモリ消費を避ける |
| OTel signal | tracesのみ | logsは既存Loki経路、metricsは既存Prometheus経路を維持して重複収集を防ぐ |
| Playwright Trace | 本変更の必須範囲外 | OpenTelemetry導入後に失敗時artifactとして別フェーズで追加する |

TempoチャートはGrafana公式`tempo` chartを使用する。設計時点の安定版はchart `1.24.4`
（Tempo `2.9.0`）だが、実装開始時に公式repositoryで再確認し、具体的なバージョンへ固定する。
`latest`、`*`、バージョン範囲は使用しない。更新はRenovateのPRで行う。

## 4. データ設計

### 4.1 Resource attributes

全Spanへ次を付ける。Kubernetes値はDownward APIから取得し、SDK初期化時にResourceへ設定する。

| Attribute | 値 |
| --- | --- |
| `service.name` | `point-guide-scraper` |
| `service.version` | ビルド元Git SHA。なければイメージdigestをデプロイ時に渡す |
| `deployment.environment.name` | `homelab` |
| `k8s.namespace.name` | `point-guide-scraper` |
| `k8s.pod.name` | 実行Pod名 |
| `k8s.pod.uid` | 実行Pod UID |
| `k8s.node.name` | 実行Node名 |
| `k8s.cronjob.name` | 元のCronJob名 |

Pod名やUIDをPrometheus labelへ変換しない。これらは検索用のTrace attributeとしてのみ使う。

### 4.2 Span階層

```text
scrape.run                         1 CronJob実行に1個
├─ scraper.browser.initialize
├─ scraper.proxy.load
├─ scraper.proxy.validate
├─ scrape.category                カテゴリごと
│  ├─ scrape.page                 ページごと
│  │  ├─ browser.navigate
│  │  ├─ scraper.wait_content
│  │  ├─ scraper.extract
│  │  └─ scraper.validate
│  └─ database.upsert             DB保存単位
└─ scraper.notify                 通知を行った場合のみ
```

既存コードの構造上、一部サイトで抽出と保存の境界が異なる場合がある。Spanを作るためだけに
スクレイピングの挙動を大規模変更しない。まず共通harnessと`BaseScraper`で作れる境界を計装し、
サイト固有の詳細化は後続PRへ分ける。

### 4.3 必須attributes

| Span | 必須attributes |
| --- | --- |
| `scrape.run` | `scraper.run.id`, `scraper.site`, `scraper.trigger`, `scraper.attempt`, `scraper.result`, `scraper.duration_ms` |
| `scrape.category` | `scraper.site`, `scraper.category`, `scraper.category.id` |
| `scrape.page` | `scraper.site`, `scraper.category`, `scraper.page.number`, `scraper.items.found`, `scraper.items.valid`, `scraper.items.rejected` |
| `browser.navigate` | `server.address`, `http.response.status_code`, `scraper.retry.count`, `scraper.proxy.used=true` |
| `database.upsert` | `db.system.name=postgresql`, `db.operation.name=UPSERT`, `scraper.items.written` |

`scraper.run.id`は実行開始時にUUIDを生成し、全ログにも同じ値を含める。
例外時はSpan statusを`ERROR`にし、`recordException`で例外型と安全なメッセージを記録する。
成功、失敗、タイムアウトの最終状態は必ずroot spanへ設定する。

### 4.4 Spanに入れてはいけない情報

- 案件名、成果条件、ポイント、案件URLなどの取得結果
- ページHTML、DOM、スクリーンショット
- Cookie、Authorization header、Discord Webhook URL
- WebShare API key、プロキシのユーザー名・パスワード、完全なproxy URL
- PostgreSQLのパスワード、接続文字列
- SQL bind parameter、案件データを含むSQL全文
- URL query、リクエスト・レスポンス本文
- 個人情報または認証後ページの内容

`server.address`にはホスト名だけを入れ、完全なURLは入れない。Proxyは`used=true`と、必要なら
秘密を含まないprovider名だけを記録する。Collector側にもdenylistによる削除処理を置き、
アプリ側の設定ミスだけに依存しない。

## 5. アプリケーション計装

### 5.1 実装方法

- OpenTelemetryの初期化は、他モジュールより前に読み込まれる専用entrypointへ置く。
- `@opentelemetry/sdk-node`、OTLP HTTP exporter、必要最小限のAPI/Resource packageを使う。
- Playwright操作は自動計装に期待せず、共通wrapperまたはharnessで手動Spanを作る。
- 初期段階ではNode.jsの全HTTP自動計装を有効にしない。外部サイトやproxyへ
  `traceparent`を送信しないためである。
- PostgreSQLも最初は手動Spanとし、SQL本文やbind値を取得しない。
- `BatchSpanProcessor`相当のbatch exportを使い、通常終了とSIGTERMの両方でSDKをflush/shutdownする。
- OTLP送信失敗はスクレイピング本体の終了コードを変えない。Telemetryは補助機能であり、
  対象サイト取得やDB保存を止めてはならない。
- `OTEL_SDK_DISABLED=true`でコード変更なしに停止できるようにする。

### 5.2 環境変数

CronJobへ少なくとも次を追加する。Secretは不要である。

```yaml
OTEL_SERVICE_NAME: point-guide-scraper
OTEL_EXPORTER_OTLP_PROTOCOL: http/protobuf
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: http://alloy-otel.logging.svc.cluster.local:4318/v1/traces
OTEL_TRACES_SAMPLER: parentbased_always_on
OTEL_BSP_EXPORT_TIMEOUT: "10000"
OTEL_SDK_DISABLED: "false"
```

Downward APIでPod名、UID、Namespace、Node名を渡す。CronJob名はサイト別overlayから明示するか、
Job owner referenceを無理にKubernetes APIで取得せず、既存ラベル・環境変数から決定する。
ServiceAccount tokenのautomount禁止は維持する。

### 5.3 ログ相関

アプリの構造化ログへ、active spanがある場合だけ次を追加する。

```json
{
  "trace_id": "32桁の16進数",
  "span_id": "16桁の16進数",
  "scraper_run_id": "UUID"
}
```

GrafanaのLoki datasourceに`trace_id` derived fieldを設定し、ログからTempo traceへ移動できるようにする。
Tempo datasource側にもTraceからLokiログへ移動する設定を追加する。既存Loki datasource UIDを
実際の値で参照し、UI上だけの手設定にしない。

## 6. Collector設計

`logging` NamespaceへGrafana Alloy chartの別release `alloy-otel`をDeployment、1 replicaで追加する。
既存`alloy-logs`、`alloy-events`、`alloy-audit`の設定は変更しない。

処理順序は次とする。

1. `otelcol.receiver.otlp`でOTLP/HTTP `4318`を受信する。
2. memory limiterを通してOOM時の無制限な蓄積を防ぐ。
3. transform/attributes processorで禁止attributeを削除する。
4. batch processorでまとめる。
5. `otelcol.exporter.otlp`でTempo `4317`へ送る。

CollectorのログへSpan payloadを出すdebug exporterは本番では有効にしない。OTLP receiverのServiceは
ClusterIPとし、Ingress、Gateway、LoadBalancer、NodePort、Tailscale公開を作らない。

初期resource目安:

```yaml
requests:
  cpu: 50m
  memory: 128Mi
limits:
  cpu: 500m
  memory: 512Mi
```

実測後に調整する。メモリ使用率、拒否Span、export失敗をServiceMonitorで収集する。

## 7. Tempo設計

### 7.1 Deployment

- Namespace: `logging`
- Helm chart: Grafana公式`tempo`
- mode: monolithic / single process
- replica: 1
- multitenancy: disabled
- anonymous reporting: disabled
- OTLP receiver: gRPC `4317`だけを有効化
- Jaeger、Zipkin、OpenCensus receiver: 無効
- query API: ClusterIP `3200`
- retention: 14日
- metrics-generator: 初期は無効
- nodeSelector: worker plane
- Pod Security: restricted、non-root、capabilities drop ALL、seccomp RuntimeDefault
- ServiceAccount token: automount無効

WALは`longhorn-logging-retain`の10GiB PVCへ置く。trace blockはMinIOへ保存する。
PVC保持方針は削除・縮退時とも`Retain`とし、Argo CD pruneでPVCを消さない。

初期resource目安:

```yaml
requests:
  cpu: 250m
  memory: 512Mi
limits:
  cpu: "2"
  memory: 2Gi
```

chart既定の大きなmemory ballastをそのまま使わない。設定可能なchartでは無効化または小さくし、
2Gi limit内でOOMしないことを負荷試験する。上記はこの低頻度ワークロードの開始値であり、
一般的な本番サイジングではない。

### 7.2 MinIO

- bucket: `tempo-traces`
- user/access key: `tempo`
- Secret: `logging/tempo-s3-credentials`
- endpoint: `minio.logging.svc.cluster.local:9000`
- S3 path-styleを有効化
- Namespace内HTTPを使用し、Cilium policyでTempo以外の資格情報利用を防ぐ
- bucket public accessは無効
- incomplete multipart uploadは1日で削除

TempoにはMinIO root資格情報を渡さない。既存`minio-bucket-bootstrap`を拡張して、専用user、bucket、
最小権限policyを冪等に作成する。Tempo policyは次だけを許可する。

- bucket location取得、list、multipart upload一覧
- object get/put/delete、object tagging
- multipart part一覧と中断

対象Resourceは`tempo-traces`と`tempo-traces/*`だけに限定する。

Secretの正は既存方針どおりmacOS Keychainとする。

```text
service: dev.craftz.homelab.tempo-s3
account: tempo
```

`scripts/bootstrap-cluster-secrets.sh`は初回だけ強い乱数を生成し、以降は同じ値を
`logging/tempo-s3-credentials`へ冪等に反映する。値を標準出力、Git、ログへ出さない。
Tempo chartではこのSecretを環境変数として読み込み、Tempoの設定値には
`${AWS_ACCESS_KEY_ID}`と`${AWS_SECRET_ACCESS_KEY}`を参照させる。
`-config.expand-env=true`を明示し、render済みConfigMapへSecret実値を埋め込まない。

### 7.3 Grafana

`kubernetes/infra/monitoring/values.yaml`へprovisioned Tempo datasourceを追加する。

- name / uid: `Tempo` / `tempo`
- URL: `http://tempo.logging.svc.cluster.local:3200`
- editable: false
- TraceQL検索を利用可能にする
- Tempo -> LokiとLoki -> Tempoの相互リンクを設定する
- Grafana以外からTempo query APIへ接続させない

Tempo専用UI、Ingress、Tailscale hostnameは作らない。利用者は既存GrafanaのExploreから参照する。

## 8. NetworkPolicy

default-denyを維持し、次の通信だけを追加する。

| 送信元 | 送信先 | Port | 用途 |
| --- | --- | --- | --- |
| `point-guide-scraper` Pod | `logging/alloy-otel` | TCP 4318 | OTLP/HTTP ingest |
| `logging/alloy-otel` | `logging/tempo` | TCP 4317 | OTLP/gRPC export |
| `logging/tempo` | `logging/minio` | TCP 9000 | trace block保存 |
| `monitoring/grafana` | `logging/tempo` | TCP 3200 | TraceQL/query |
| `monitoring/prometheus` | Alloy / Tempo | metrics ports | 稼働監視 |
| Alloy / Tempo | kube-dns | TCP/UDP 53 | Service名解決 |

`point-guide-scraper`側のCiliumNetworkPolicyにも4318 egressを追加する。
外部internet、LAN、Cloudflare Tunnel、TailnetからAlloy/Tempoへ到達する経路は作らない。

## 9. Sampling・保持・容量

Phase 1は100% sampling、Tempo 14日保持とする。対象は日次17ジョブであり、案件1件をSpan化しなければ
小さい。最初の7日間で実際の取り込み量、MinIO増加量、Tempoメモリを測定する。

次のいずれかになった場合だけsamplingを見直す。

- `tempo-traces`の増加が1日1GiBを超える
- TempoまたはAlloyが継続的にmemory limitの70%を超える
- 1回の実行でSpan数が1万を超える
- Grafana検索時間が運用上問題になる

削減時は成功traceをhead samplingし、エラーを残せない単純な設定にはしない。必要であれば
Alloyのtail samplingを別設計し、成功10〜25%、ERROR/timeout 100%を目標にする。

## 10. Playwright Traceとの境界

OpenTelemetryは処理経路を、Playwright Traceはブラウザ内部の操作・DOM・networkを調べるものとする。
Phase 2で次を実装できるが、この設計の完了条件には含めない。

- browser context tracingを各試行で開始する
- 成功時は保存せず破棄する
- 最終失敗またはtimeout時だけ`trace.zip`、screenshot、必要ならsanitize済みHTMLを保存する
- artifactはTempoではなくMinIOの別bucketへ保存する
- artifact URIは短命な署名URLを都度発行し、公開URLをSpanへ保存しない
- Cookie、認証情報、個人情報を含む可能性があるためTailnet外へ公開しない

Playwright公式もLibrary利用時は`browserContext.tracing`を使うとしている。常時保存は負荷と
機密データ量が大きいため採用しない。

## 11. 監視・アラート

TempoとAlloyの実際の公開metrics名を導入バージョンで確認してからPrometheusRuleを作る。
存在しないmetric名を推測でコミットしない。

最低限、次を検知する。

- Tempoまたは`alloy-otel`のscrape targetが5分down
- AlloyでSpan拒否またはexport失敗が継続
- Tempoでingest失敗またはdiscardが継続
- Tempo Podの再起動、OOMKilled
- Tempo WAL PVC 80%超過
- MinIO `tempo-traces`への書き込み失敗
- 24時間以上、全サイトからtraceが1件も届かない

Telemetry停止をスクレイピング失敗として扱わないが、監視アラートは別に出す。

## 12. 実装TODO

### P0: 変更前確認

- [ ] `homelab`と`point-guide-scraper`両repositoryのstatus、branch、未コミット差分を記録する
- [ ] Argo CDの`monitoring`、`logging-storage`、`logging`、`point-guide-scraper`がHealthyか確認する
- [ ] MinIO、Loki、Grafana、17 CronJobの現状を記録する
- [ ] Worker Nodeのallocatableと実使用メモリを確認し、Tempoの配置余力を確認する
- [ ] 公式chart indexでTempo chart/app versionを再確認する

### P0: homelab infrastructure

- [ ] `kubernetes/infra/logging/tempo-values.yaml`を追加する
- [ ] `kubernetes/infra/logging/alloy-otel-values.yaml`を追加する
- [ ] `kubernetes/infra/logging/stack/`へ必要なService、NetworkPolicy、ServiceMonitor、PrometheusRuleを追加する
- [ ] 既存MinIO bootstrapへ`tempo-traces` bucketと専用policy/userを追加する
- [ ] MinIO NetworkPolicyへTempoから9000だけを許可する
- [ ] `scripts/bootstrap-cluster-secrets.sh`へTempo S3資格情報のKeychain生成・復元を追加する
- [ ] `scripts/reconcile-cluster-platform.sh`へ`logging-storage`、bucket bootstrap、`logging`の順序付き待機を追加する
- [ ] `kubernetes/apps/infrastructure.yaml`のlogging ApplicationへTempoと`alloy-otel` chartを追加する
- [ ] MinIO Ready後にbucket bootstrap JobをCronJobから起動して完了を待ち、その後TempoをReady判定する
- [ ] sync waveだけではCronJob実行完了を保証できないため、完全再構築スクリプトでも上記順序を検証する
- [ ] GrafanaへTempo datasourceとLoki相互リンクをprovisionする
- [ ] chart version、container imageを具体値で固定する

### P0: point-guide-scraper instrumentation

- [ ] OTel SDK初期化専用moduleを追加し、他moduleより先にロードする
- [ ] OTLP/HTTP exporterとbatch processorを設定する
- [ ] harnessのrun/task/attemptをSpan化する
- [ ] `BaseScraper`のブラウザ、proxy、ページ、抽出、検証、DB保存を手動Span化する
- [ ] 全成功・全例外・timeout・SIGTERMでroot Spanを必ず終了する
- [ ] 正常終了とSIGTERMでflush/shutdownする
- [ ] trace_id、span_id、scraper_run_idを既存loggerへ追加する
- [ ] `alloy-logs`の`log_class=application`対象へ`point-guide-scraper`を追加する
- [ ] CronJobへOTel環境変数とDownward API項目を追加する
- [ ] `point-guide-scraper` NetworkPolicyへAlloy:4318だけを追加する
- [ ] package-lockを更新し、依存versionを固定する

### P1: test and dashboard

- [ ] In-memory exporterを使い、Span名・階層・status・attributesをunit testする
- [ ] 禁止情報がSpanに含まれないことをtestする
- [ ] OTLP送信不能でもスクレイピング結果と終了コードが変わらないことをtestする
- [ ] timeoutとretryでSpanが重複・未終了にならないことをtestする
- [ ] Grafanaに`Point Guide Scraper Traces` dashboardをGit管理で追加する
- [ ] サイト、結果、所要時間、失敗stage、直近traceへのリンクを表示する
- [ ] Tempo/Loki相互リンクを確認する

### P2: optional Playwright artifacts

- [ ] 失敗時だけPlaywright Traceを保持する設計を別文書またはADRにする
- [ ] 専用MinIO bucket、保持期間、閲覧認可、sanitize方法を決める
- [ ] OpenTelemetry traceから安全にartifactを特定する方法を決める

## 13. 検証手順と完了条件

### Static validation

- 両repositoryでlint、typecheck、unit testが成功する。
- `kubectl kustomize`が成功する。
- Helm templateを含むArgo CDのrenderが成功する。
- server-side dry-runが成功する。
- Secret値、仮のdigest、`latest` tagがGit差分に含まれない。

### Cluster validation

- Argo CDの`logging-storage`、`logging`、`monitoring`、`point-guide-scraper`がSynced / Healthy。
- Tempo、`alloy-otel`がReadyで再起動を繰り返していない。
- `tempo-traces` bucketが非公開で、Tempo専用userだけが必要操作を行える。
- Alloy/Tempo ServiceがClusterIPで、Ingress、LoadBalancer、NodePortがない。
- 許可した経路だけconnectでき、別Namespaceから4317/4318/3200へ接続できない。
- Tempo再起動後も再起動前のtraceをGrafanaで検索できる。
- 完全再構築コマンドでMinIO、bucket/user、Tempo、Alloyの順に自動収束し、手作業を要求しない。

### End-to-end validation

1. 既存CronJobから負荷の軽い1サイトを一時Jobとして起動する。
2. Grafana ExploreのTempoで`service.name=point-guide-scraper`を検索する。
3. root traceからbrowser、category/page、validate、DB保存Spanを開く。
4. site、件数、試行回数、所要時間、最終statusを確認する。
5. Traceから同じtrace_idのLokiログへ移動できることを確認する。
6. 実案件名、URL query、Cookie、API key、DB passwordがSpan/ログにないことを確認する。
7. 一時的にOTLP endpointを到達不能にしたtestで、scraper本体が通常どおり終了できることを確認する。
8. test用Jobだけを削除し、CronJob、本番DB、既存MinIO objectには触れない。

これらを満たすまで「導入完了」としない。

## 14. Rollback

1. `point-guide-scraper`で`OTEL_SDK_DISABLED=true`にしてexportを停止する。
2. 問題がアプリ計装にある場合は計装PRだけをrevertする。
3. 問題がCollector/Tempoにある場合はArgo CDの該当commitへ戻す。
4. `tempo-traces` bucket、Tempo WAL PVC、Tempo資格情報は障害調査が終わるまで削除しない。
5. Loki、Prometheus、Grafanaの既存datasourceとログ収集を巻き戻さない。

Telemetryは補助経路なので、停止・rollback中も既存スクレイピングとDB保存は継続できなければならない。

## 15. 変更禁止・注意事項

- `point-guide-postgres`、MinIO、Loki、Grafanaを削除・再作成しない。
- MinIOの既存bucket/objectを削除しない。
- TempoへMinIO root資格情報を渡さない。
- Tempo、Alloy OTLP receiver、MinIO APIをインターネット、LAN、Tailnetへ公開しない。
- 外部サイトへ`traceparent`、内部run ID、認証情報を送信しない。
- 取得案件1件ごとにSpanを作らない。
- 高cardinality値をPrometheus labelにしない。
- Telemetry送信失敗でCronJobを失敗させない。
- UIでだけ設定を変更しない。desired stateはGitに置く。
- 他作業者の未コミット変更を編集・stageしない。
- コミットは`craftz <hi@craftz.dev>`を使用する。

## 16. 関連ファイル

### homelab

- `kubernetes/apps/infrastructure.yaml`
- `kubernetes/apps/point-guide-scraper.yaml`
- `kubernetes/infra/logging/alloy-logs-values.yaml`
- `kubernetes/infra/logging/loki-values.yaml`
- `kubernetes/infra/logging/stack/kustomization.yaml`
- `kubernetes/infra/logging/storage/minio.yaml`
- `kubernetes/infra/logging/storage/minio-bootstrap.yaml`
- `kubernetes/infra/logging/storage/networkpolicy.yaml`
- `kubernetes/infra/monitoring/values.yaml`
- `scripts/bootstrap-cluster-secrets.sh`

### point-guide-scraper

- `src/index.ts`
- `src/harness/runner/scraper-runner.ts`
- `src/scrapers/base.scraper.ts`
- `src/interfaces/scraper.interface.ts`
- `deploy/kubernetes/cronjob/cronjob.yaml`
- `deploy/kubernetes/networkpolicy.yaml`
- `deploy/kubernetes/point-guide-scraper.env`

## 17. 公式資料

- [OpenTelemetry JavaScript instrumentation](https://opentelemetry.io/docs/languages/js/instrumentation/)
- [OpenTelemetry JavaScript libraries](https://opentelemetry.io/docs/languages/js/libraries/)
- [Grafana Alloy OTLP receiver](https://grafana.com/docs/alloy/latest/reference/components/otelcol/otelcol.receiver.otlp/)
- [Grafana Tempo configuration](https://grafana.com/docs/tempo/latest/configuration/)
- [Tempo S3-compatible storage](https://grafana.com/docs/tempo/latest/configuration/hosted-storage/s3/)
- [Playwright Trace Viewer](https://playwright.dev/docs/trace-viewer)

## 18. 実装ルール

1. 変更前にlive状態と両repositoryのGit差分を確認する。
2. 1つ目のPRはhomelab側のTempo/Alloy/MinIO/Grafana、2つ目のPRはscraper計装とする。
3. infrastructureを先に同期し、OTLP endpointがReadyになってからscraperを切り替える。
4. 既存コードを大規模refactorせず、共通境界から段階的に計装する。
5. 各PRで秘密情報検査、render、test、live確認結果を記録する。
6. 実測値と公式schemaを優先し、この文書の例を無検証でコピーしない。
7. 完了時は変更ファイル、テスト結果、Grafana上の確認方法、残課題を報告する。
