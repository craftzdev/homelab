# CloudNativePG 引き継ぎ TODO

Claude Code は最初にこの文書と関連マニフェストを読み、既存クラスタを削除・再作成せずに作業すること。

## 目的

Umami の PostgreSQL を CloudNativePG で高可用化し、MinIO への継続バックアップと復元可能性を維持する。

## 現在の状態（導入済み）

- [x] CloudNativePG Operator 1.30.0 を Argo CD で導入
- [x] Barman Cloud Plugin v0.15.0 を Argo CD で導入
- [x] PostgreSQL 17.11 を3インスタンス構成で導入
- [x] 3台の worker に required anti-affinity で分散
- [x] 各DBインスタンスに `longhorn-cnpg-retain` の10Giボリュームを割り当て
- [x] Umami用DB、所有者、TLS接続を設定
- [x] MinIOの `umami-postgres` バケットへWALを連続保存
- [x] Barman server nameを `umami-postgres-v1` に固定
- [x] 毎日03:30 JSTにstandby優先でベースバックアップ
- [x] バックアップ保持期間を30日に設定
- [x] 初回手動バックアップと日次バックアップの完了を確認
- [x] Umamiとroot Applicationが `Synced / Healthy`
- [x] UmamiのHTTPS、heartbeat、ログインを確認

## 最初に行う現状確認

- [ ] 以下の確認コマンドを実行し、既存状態を記録する

```bash
kubectl --kubeconfig _out/kubeconfig -n argocd \
  get applications cloudnative-pg cloudnative-pg-barman umami

kubectl --kubeconfig _out/kubeconfig -n analytics \
  get clusters.postgresql.cnpg.io,pods,pvc -o wide

kubectl --kubeconfig _out/kubeconfig -n analytics \
  get scheduledbackups.postgresql.cnpg.io,backups.postgresql.cnpg.io
```

完了条件:

- `cloudnative-pg`、`cloudnative-pg-barman`、`umami` が `Synced / Healthy`
- `umami-postgres` が `3/3 Ready`
- `ContinuousArchiving=True`
- 直近のBackupが `completed`

## 優先度P0: 復元可能性を証明する

- [ ] MinIO上の直近バックアップを使う隔離復元手順を設計する
- [ ] 本番の `analytics` namespaceとは別の一時namespaceへ復元する
- [ ] 復元先では別のCluster名、Secret名、サービス名を使用する
- [ ] 復元したDBでUmamiの主要テーブルと行数を確認する
- [ ] 復元所要時間を計測し、RTO/RPOを記録する
- [ ] 検証後は一時クラスタだけを削除し、本番PVCとMinIOデータには触れない
- [ ] 復元手順を `docs/50-operations.md` に追記する

完了条件:

- バックアップから新しいPostgreSQLクラスタを作成できる
- Umamiデータの存在をSQLで確認できる
- 作業者がコピー＆ペーストで再実行できる手順になっている

## 優先度P1: 監視と通知

- [ ] 非推奨の `spec.monitoring.enablePodMonitor` を廃止する
- [ ] CloudNativePG用の明示的なPodMonitorをGit管理する
- [ ] Grafanaに次の情報を表示する
  - クラスタReady数
  - primary/replica状態とレプリケーション遅延
  - WAL archive成功・失敗
  - 最終バックアップ成功時刻
  - PostgreSQL接続数、容量、CPU、メモリ
- [ ] 次のアラートを追加する
  - 3インスタンス未満の状態が5分継続
  - `ContinuousArchiving=False`
  - 直近26時間以内に成功バックアップがない
  - PVC容量が80%超過
- [ ] アラート発火と復旧通知をテストする

## 優先度P1: バックアップ運用

- [ ] MinIO自体のデータがPBS側へ保護されていることを確認する
- [ ] MinIO障害時もPostgreSQL本体が継続稼働することを確認する
- [ ] 30日保持の削除処理が実際に動作していることを確認する
- [ ] 月1回の復元訓練チェックリストを作成する
- [ ] バックアップ失敗時の一次対応手順を作成する

## 優先度P2: セキュリティとSecret運用

- [ ] `umami-db-owner` と `umami-s3-credentials` のローテーション手順を作成する
- [ ] Secretの値をGit、ログ、Issue、PRへ出力しない
- [ ] macOS Keychainを正とする現在のbootstrap方式を維持するか、SOPSへ統一するかADRで決定する
- [ ] NetworkPolicyが次の通信だけを許可していることを再確認する
  - UmamiからPostgreSQL
  - CloudNativePG/BarmanからMinIO:9000
  - 監視基盤からメトリクスendpoint
- [ ] PostgreSQLのsuperuser accessが無効であることを確認する

## 優先度P2: 更新と保守

- [ ] CloudNativePG Operator、Barman Plugin、PostgreSQLイメージの更新手順を作成する
- [ ] 更新前バックアップ、ローリング更新、rollbackの確認項目を定義する
- [ ] Renovate等でHelm chartとimage digestの更新PRを作るか検討する
- [ ] minor/major PostgreSQLアップグレード方針をADRへ記録する

## 変更禁止・注意事項

- 本番Cluster `analytics/umami-postgres` を削除しない
- 本番PVC、PV、Longhorn volumeを削除しない
- `umami-postgres` MinIO bucketの既存オブジェクトを削除しない
- 新規クラスタで既存の `serverName` を再利用しない
- `cnpg.io/skipEmptyWalArchiveCheck` を有効にしない
- CloudNativePG管理PodをDeploymentやStatefulSetへ置き換えない
- Secretの実値をコマンド出力やコミットへ含めない
- 作業ツリーにある他作業者の未コミット変更を編集・stageしない

## 関連ファイル

- `kubernetes/apps/cloudnative-pg.yaml`
- `kubernetes/apps/umami.yaml`
- `kubernetes/infra/cloudnative-pg/operator-values.yaml`
- `kubernetes/infra/cloudnative-pg/barman-values.yaml`
- `kubernetes/infra/cloudnative-pg/networkpolicy.yaml`
- `kubernetes/infra/umami/database.yaml`
- `kubernetes/infra/umami/workload.yaml`
- `kubernetes/infra/umami/networkpolicy.yaml`
- `kubernetes/infra/umami/README.md`
- `kubernetes/infra/logging/storage/minio-umami-bootstrap.yaml`

## 実装ルール

1. 変更前にlive状態とGit差分を確認する。
2. Kubernetesリソースは原則Gitへ追加し、Argo CDに反映させる。
3. 一時的な復元検証リソースには、本番と明確に異なる名前を付ける。
4. マニフェストは `kubectl kustomize` とserver-side dry-runで検証する。
5. Argo CDの `Synced / Healthy` と実サービスの動作確認を両方行う。
6. コミットは `craftz <hi@craftz.dev>` を使用する。
7. 復元テスト完了までは「バックアップ導入完了」ではなく「保存確認済み」と表現する。
