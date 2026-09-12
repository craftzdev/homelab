# AI Gateway monitoring

`ai-gateway-01` のホストとComposeサービスを既存のPrometheus/Grafanaへ追加する。
APIやDBの再起動、アプリケーションの設定変更は不要。

## 導入・更新

このディレクトリ全体をGatewayへコピーし、Gateway上で `sudo bash install.sh` を実行する。
既存の管理経路では `ssh -J root@172.16.10.11 craftz@172.16.40.30` を利用できる。
Ubuntu公式リポジトリの `prometheus-node-exporter` を使用する。

- Node exporter: `172.16.40.30:9100` にだけbind。UFWはKubernetesの6ノードIPから
  このポートへの通信だけを追加許可する。既存の禁止ルールは変更しない。
- `ai-gateway-metrics.timer`: 30秒ごとに専用collectorを実行。
- CollectorはDockerの指定3サービスの稼働・healthcheck・CPU・メモリ、
  localhostのAPI readiness、cloudflaredの4種類のメトリクスだけを取得する。
  Docker環境変数、healthcheck出力、ログ、APIトークン、リクエスト内容は出力しない。
- `/var/lib/prometheus/node-exporter/ai-gateway.prom` をatomic replaceする。
  収集結果と更新時刻も公開し、停止したcollectorの古い値を正常扱いしない。
  想定外の例外で途中終了した場合もファイルを書き直し、
  `ai_gateway_collection_success 0` を出す（古い値を残さない）。
- unitは `ProtectSystem=strict` で動くため、`ReadWritePaths` に
  `/run/docker.sock` を含める。これが無いとdocker CLIがdaemonへ接続できず、
  コンテナ関連のメトリクスが常に0になる。
- Kubernetes側は `kubernetes/infra/monitoring/ai-gateway.yaml` を反映する。
  取得元を変更するときはEndpointSlice、bind先、UFW許可を一緒に更新する。

## 確認

`systemctl is-active prometheus-node-exporter ai-gateway-metrics.timer` がactive、
Prometheusで `up{job="ai-gateway"}` が1、
`time() - ai_gateway_collection_timestamp_seconds{job="ai-gateway"}` が90未満、
`ai_gateway_docker_collection_success` / `ai_gateway_tunnel_collection_success` /
`ai_gateway_collection_success` が1を確認する。
`node_textfile_scrape_error{job="ai-gateway"}` は0であること。

Dashboard: [Homelab / AI Worker & Gateway](https://grafana.tailb6c7d.ts.net/d/homelab-ai-platform)

無効化する場合はServiceMonitorを先に外し、Gateway上の
`ai-gateway-metrics.timer` と `prometheus-node-exporter` を停止する。
必要に応じて導入した6本のUFW許可と専用systemd unit/drop-inを削除する。
既存Composeサービスやデータには触れない。

参考: [Node exporter / textfile collector](https://github.com/prometheus/node_exporter#textfile-collector)、
[Prometheus Operator API](https://prometheus-operator.dev/docs/api-reference/api/)。
