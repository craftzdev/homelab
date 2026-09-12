# 監視スタックのセットアップ

## API サーバーの復旧確認

`python3 scripts/check-api-server-health.py` をリポジトリのルートで実行する。
Grafana の既存 Keychain 認証情報をメモリ内だけで使い、APIサーバー3台の稼働、
スクレイプの鮮度、5xx、etcdアクセスのp99、SLOの集計値、アラートルールをJSONで返す。
クラスタへの変更は行わない。終了コードは 0=3台とも健全と確認、
1=問い合わせは成功したが健全と確認できない、2=接続・認証・応答処理の失敗。
定期実行から呼ぶ場合、1 を成功として扱わない。`alerts_cleared` は終了コードに
含めない（原因修正後もバーンレートが自然解除されるまで firing のままになるため）。
`alerts_cleared` は全4ルールがinactiveで、3台の監視とルール評価が新鮮な場合だけtrue。
監視データが無い状態を「復旧」と判断しない。

`KubeAPIErrorBudgetBurn` のwarningは2時間/1日と6時間/3日の組み合わせで評価する。
原因修正後も過去の失敗が集計から外れるまで残るため、直近5分の値と区別する。
しきい値緩和やアラート停止で消さず、自然解除を確認する。

## ホームラボ用ダッシュボード

| ダッシュボード | 用途 |
| --- | --- |
| [Homelab / Overview](https://grafana.tailb6c7d.ts.net/d/homelab-overview) | ノード・Pod・アラートの概要、ノード別CPU/メモリ/通信、監視ターゲット、API |
| [Homelab / Workloads & Storage](https://grafana.tailb6c7d.ts.net/d/homelab-workloads) | Namespace別リソース、再起動、コンテナ待機理由、Deployment不足、PVC容量 |
| [Homelab / Logs & Events](https://grafana.tailb6c7d.ts.net/d/homelab-logs) | Podログ、Warning Events、通常Events、監査ログ |
| [Homelab / AI Worker & Gateway](https://grafana.tailb6c7d.ts.net/d/homelab-ai-platform) | AI workerの稼働・CPU・メモリ・PVC・ログ、Gateway VM/API/DB/トンネル、workerノード3台 |
| [Homelab / PBS & Backups](https://grafana.tailb6c7d.ts.net/d/homelab-pbs) | PBS CPU・メモリ・容量、VM別バックアップ保存日時、検証・GC、Proxmoxジョブ結果と定期予定 |

`dashboards/*.json` を `kustomization.yaml` の ConfigMap generator から配信する。
既存の Grafana sidecar が `grafana_dashboard: "1"` の ConfigMap を読み込み、
固定 UID でプロビジョニングする。変更は JSON を編集して monitoring を同期する。
UI からの編集は保存できない。標準ダッシュボードはそのまま利用できる。
`grafana.ini.dashboards.default_home_dashboard_path` で Overview をホーム画面にする。
個人・組織のホーム画面設定がある場合はそちらが優先される。

- 時刻は Asia/Tokyo、概要とワークロードは直近6時間・30秒更新、ログは直近1時間・1分更新。
- Overview の上段は常にクラスタ全体。Node フィルターはノード別グラフに適用する。
- 再起動回数は選択期間の `increase` による推定値（端点の補間で小数になる場合がある）。
- PVC は kubelet のファイルシステム使用量。Longhorn の物理容量やレプリカ状態とは異なる。
  未マウントPVCは容量グラフに出ない場合がある。
- ログはパネルごとに最新200行。Podログは Namespace/Pod/文字列検索、Events は
  Namespace/文字列検索、監査ログはクラスタ全体を文字列検索する。
- 「エラー候補」は文字列での抽出であり、構造化された severity 判定ではない。
- 空の異常一覧は該当なしを示すが、メトリクス未収集でも空になるため、Overview の
  ターゲット稼働率と合わせて確認する。アラート通知先の設定とは独立している。

JSON を手動インポートする場合は、非表示のデータソース変数 `prometheus` / `loki`
をインポート先のデータソースに合わせる。UID は `prometheus` と
`P8E80F9AEF21F6940` を初期値にし、データソース変数で参照する。

### AI Worker / Gateway の監視

- Workerは既存のkube-state-metrics、kubelet、Lokiから取得する。Pod選択は
  `ai-worker` namespace（WorkerとAgent Edge）、Worker Node選択はworkerノード3台。
  上段サマリーはPod選択に依存しない。Jobの成否やモデル使用量はこの画面の対象外。
- Gateway VMは `ai-gateway.yaml` のselectorなしService / EndpointSliceと
  ServiceMonitorで `172.16.40.30:9100` を30秒ごとに収集する。jobは `ai-gateway`。
  ホスト側の導入・復元は [Gateway監視](../../../ai-business-platform/infra/monitoring/README.md) を参照。
- Gateway API / DBの状態はDocker healthcheckとVM内の `/ready` の組み合わせ。
  収集が90秒以上古い場合やexporterが停止した場合、上段は正常表示にしない。
  データが欠損した場合は「データなし」。30秒周期のため、短い瞬断を必ず捕捉するものではない。
- 公開APIのリクエスト・HTTP応答・接続エラーはcloudflaredのメトリクス。
  Tailnet callbackやWorkerへの内部リクエストは含まない。Gatewayの生ログは収集しない。
  `/ready` の所要時間は利用者リクエストのレイテンシではない。
- GatewayコンテナCPUは100%で1コア。再起動カウンターはコンテナの再作成でリセットする。
  Gatewayのグラフ履歴は監視開始後から蓄積される。

### PBS の監視

`portal/pbs-observer` が専用のProxmox監査トークン `grafana@pve!monitor` で保存先 `pbs-gateway`
の容量、PBS宛てスケジュール、各Proxmoxノードの直近バックアップジョブを取得する。
収集は1分間隔。`pbs.yaml` のServiceMonitorから既存Prometheusへ配信する。
PBS本体のCPU・メモリ・容量、保存済みスナップショット、検証記録・定期検証設定、GCは
専用の監査用トークン `grafana@pbs!monitor` で取得する。observerだけに
`172.16.10.51:8007` を許可し、AI等の他のワークロードへの禁止は維持する。
PBSの書き込み用認証情報は使わず、証明書とホスト名を検証する。
実装と運用は [Homepage / PBS observer](../homepage/README.md) を参照。

ジョブ欄はProxmoxのvzdumpタスクで、保存先を問わない直近100件/Nodeが対象。
完了したタスクの成否を表示するもので、PBSスナップショットの検証結果ではない。
保存済みバックアップは `gateway-backup` のルート名前空間が対象。
未検証・不明と検証失敗を区別し、定期検証が0件なら「未設定」と表示する。
VM別の経過時間は48時間で注意、72時間で要確認という表示上の目安であり、
個別のバックアップ運用予定や復元可否を判定しない。GC成功も全件検証済みを意味しない。
監視開始時点の直近ジョブは3ノードとも `job errors` であり、正常表示に置き換えない。

外部ターゲット（Gateway VM / PBS observer）は、ServiceMonitorの設定やCRDの差で
エラーを出さずに対象ごと消えることがある。`alerts.yaml` の `homelab.monitoring` で
`AIGatewayTargetMissing` / `PBSObserverTargetMissing` / `BackupTelemetryCollectionFailing`
として検知する。グラフが空であることと、監視対象が消えていることを区別する。

## Grafana 管理者パスワードの設定（必須）

**この手順を実施しないと Grafana は起動しません。** これは意図的な設計です。

`kube-prometheus-stack` は `adminPassword` を指定しないと既定値
（`admin` / `prom-operator`）で起動します。Tailnetへ公開している以上、
「気づかないうちに既知のパスワードで動いている」状態は避けるべきです。
そのため `admin.existingSecret` を必須にし、**設定漏れが明確な失敗として現れる**
ようにしています。

再構築スクリプトは Keychain service
`dev.craftz.homelab.grafana-admin`（account: `admin`）を参照します。値が無い
初回だけ強い乱数を生成してKeychainへ保存し、Kubernetes Secretへ投入します。
パスワードを標準出力やGitへ出さないため、通常は手作業不要です。

### パスワードの確認・ローテーション

```bash
# 確認（端末上に表示されるため必要なときだけ実行）
security find-generic-password \
  -s dev.craftz.homelab.grafana-admin -a admin -w

# ローテーション（次の再構築または bootstrap-cluster-secrets.sh で反映）
security add-generic-password -U \
  -s dev.craftz.homelab.grafana-admin -a admin \
  -w "$(openssl rand -base64 32)"
./scripts/bootstrap-cluster-secrets.sh
```

## アクセス方法

| サービス | アクセス | 備考 |
| --- | --- | --- |
| Grafana | https://grafana.tailb6c7d.ts.net/ | Tailnet管理者のみ。Tailscale HTTPS |
| Prometheus | `kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090` | ClusterIP のみ |
| Alertmanager | `kubectl -n monitoring port-forward svc/kube-prometheus-stack-alertmanager 9093:9093` | ClusterIP のみ |

Grafanaで左メニューの **Explore** を開き、データソースに **Loki** を選ぶ。
Podログは `{cluster="homelab"}`、監査ログは `{log_class="audit"}`、Kubernetes
Eventは `{job="kubernetes/events"}` で検索できる。

> ⚠️ いずれも**インターネットには公開していません**。GrafanaはTailnet管理者、
> それ以外はport-forwardだけに制限する。

## Talos 特有の注意

`kubeEtcd` / `kubeControllerManager` / `kubeScheduler` の監視を**無効化**しています。

Talos ではこれらが static pod としてホストネットワーク上で動き、既定では
`127.0.0.1` にしかバインドしません。有効にすると「ターゲットが落ちている」
アラートが鳴り続けます。

取得したい場合は Talos の machine config で各コンポーネントの
`--bind-address` を変更する必要がありますが、**管理コンポーネントを
外部にバインドすることになる**ため、セキュリティとのトレードオフを
検討した上で判断してください。

## アラート通知先の設定（未設定）

現状、Alertmanager の通知先は `null`（UI でしか見えない）です。
実運用では必ず通知先を設定してください。Webhook の URL は機密なので、
Grafana のパスワードと同様に SOPS で暗号化した `AlertmanagerConfig` を
使ってください。
