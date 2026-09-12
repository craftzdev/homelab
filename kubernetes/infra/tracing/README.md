# OpenTelemetry / Tempo

GrafanaのExploreでデータソース **Tempo** を選ぶ。
[Grafana Explore](https://grafana.tailb6c7d.ts.net/explore)

## 構成

- Argo CD Application: `tracing`
- Collector: `logging/alloy-otel` (Alloy chart 1.12.1、Deployment 1台)
- Backend: `logging/tempo` (community chart 3.0.0、Tempo 3.0.3、単一プロセス)
- 保存: MinIO `tempo-traces`、専用S3ユーザー、保持14日
- WAL: `storage-tempo-0`、10Gi、`longhorn-logging-retain`
- UI: 既存Grafana、Tempo/Loki相互リンク
- メトリクス: 既存PrometheusのServiceMonitorで収集

旧Grafana chart 1.24.4はdeprecatedなので後継community repositoryを使う。
Tempo 3では2系のingester/compactor設定を流用せずlive-store/backend設定を使う。
Kafka、metrics-generator、追加のGrafanaは導入しない。
chartにlegacy Serviceポートの定義が残っているが、プロセスの受信は4317と3200のみで、
NetworkPolicyもこの経路だけを許可する。

## アプリから送る

```text
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://alloy-otel.logging.svc.cluster.local:4318/v1/traces
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```

送信を許可するPodには `homelab.craftz.dev/otel-client: "true"` を付ける。
現在の許可Namespaceは `moshitoku-scraper`（旧point-guide-scraper）。
Podラベルだけで別Namespaceに権限は広がらない。
他のアプリを追加するときは送受信双方のpolicyとattribute allowlistをレビューする。

この基盤を導入するだけではscraperの処理は自動計装されない。
アプリ側にはOTel SDK、Spanの作成、終了時flush、上記環境変数とPodラベルが別途必要。
外部サイトへのtraceparent送信、案件単位Span、SQLや認証情報の保存は行わない。

Collectorはtracesだけを受け付け、ログは既存Alloy/Loki経路を維持する。
Span/resource/event attributeにはallowlistを適用し、status messageも削除する。
Span名、event名、許可attributeの値は送信アプリが固定語彙・安全な値に制限する。
Collectorは任意の本文を自動的に安全化する仕組みではない。
送信queueはメモリ上で有界。再起動・長時間障害時の未送信Span消失は許容する。

## Secret・再構築

Keychain service `dev.craftz.homelab.tempo-s3` / account `tempo` が正。
`scripts/bootstrap-cluster-secrets.sh`は専用スクリプトを呼び、logging Namespaceへ
`tempo-s3-credentials`を反映する。初回のみ生成し、既存値をローテーションしない。

個別に再投入するとき:

```bash
KUBECONFIG="$PWD/_out/kubeconfig" bash scripts/bootstrap-tracing-secret.sh
```

tracingの同期ではpolicy/ConfigMapがwave -2、MinIO bootstrap Jobがwave -1、
Tempo/Alloyがwave 0で適用される。Sync hookがbucket/user/policyを冪等に構成する。
MinIOとloggingがHealthyになってからtracingを待つ処理を再構築reconcilerへ追加済み。
既存MinIOビルドはAbortIncompleteMultipartUpload lifecycleを拒否するため未設定。
オブジェクト保持はTempoが管理する。MinIOの独立バックアップを新設する変更ではない。

## 動作検証

```bash
python3 scripts/verify-tracing.py --kubeconfig _out/kubeconfig
```

架空データを持つ短命Jobをscraper Namespaceで起動し、AlloyへHTTP送信する。
Grafana PodからTempo APIを呼び、同じtrace IDの検索と禁止attributeの除去を確認する。
テスト用Jobは終了時に削除する。外部スクレイピングも業務DB書き込みも行わない。
出力されたtrace IDをGrafana Exploreで検索できる。

サンプルTraceQL:

```traceql
{ resource.service.name = "tracing-smoke" }
```

## 運用上の境界

- Alloy/TempoはClusterIPのみ。Tailnet/Cloudflare公開はGrafanaだけ。
- 既存loggingのhost/remote-node probe許可は残るため、node管理者に対する隔離ではない。
- 単一Pod構成はHAではなく、再起動時は一時的に取り込み・検索が止まる。
- Longhorn replicationはバックアップではない。MinIO/PVCをprune・削除しない。
- トレースの有無を業務処理の成功・失敗の唯一の根拠にしない。
- 既存MinIO Longhorn volumeは導入中にdegraded/一時的I/O停止が観測され、その後healthyへ自動復旧した。
  再発を監視し、HealthyなPod表示だけでストレージ健全性を判断しない。
- CPU/memoryは低頻度取り込み向け初期値。実測に応じて調整する。
- ロールバックはアプリのexport停止後、Git revertとArgo CD同期で行う。
  bucket/PVC/資格情報は調査が終わるまで保持する。

## 参照

- [Tempo deployment planning](https://grafana.com/docs/tempo/latest/set-up-for-tracing/setup-tempo/plan/)
- [Alloy transform processor](https://grafana.com/docs/alloy/latest/reference/components/otelcol/otelcol.processor.transform/)
- [Tempo community chart](https://github.com/grafana-community/helm-charts/tree/main/charts/tempo)
