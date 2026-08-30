# 監視スタックのセットアップ

## Grafana 管理者パスワードの設定（必須）

**この手順を実施しないと Grafana は起動しません。** これは意図的な設計です。

`kube-prometheus-stack` は `adminPassword` を指定しないと既定値
（`admin` / `prom-operator`）で起動します。宅内 LoadBalancer に公開している以上、
「気づかないうちに既知のパスワードで動いている」状態は避けるべきです。
そのため `admin.existingSecret` を必須にし、**設定漏れが明確な失敗として現れる**
ようにしています。

### 手順

```bash
cd kubernetes/infra/monitoring

# 1) 強いパスワードを生成する
PASSWORD="$(openssl rand -base64 24)"
echo "生成されたパスワード（パスワードマネージャに保存してください）:"
echo "$PASSWORD"

# 2) Secret のマニフェストを作る（一時ファイルは umask で保護）
umask 077
cat > /tmp/grafana-admin.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: grafana-admin
  namespace: monitoring
type: Opaque
stringData:
  admin-user: admin
  admin-password: "${PASSWORD}"
EOF

# 3) SOPS で暗号化する
sops --encrypt --config ../../../.sops.yaml /tmp/grafana-admin.yaml \
  > grafana-admin.sops.yaml

# 4) 一時ファイルを消し、暗号化されたことを確認する
rm -f /tmp/grafana-admin.yaml
grep -q 'ENC\[' grafana-admin.sops.yaml && echo "OK: 暗号化されています"
grep -q "$PASSWORD" grafana-admin.sops.yaml && echo "NG: 平文が残っています！" || echo "OK: 平文は含まれていません"

unset PASSWORD

# 5) コミットする
git add grafana-admin.sops.yaml
git commit -m "feat(monitoring): Grafana の管理者認証情報を追加（SOPS 暗号化済み）"
```

## アクセス方法

| サービス | アクセス | 備考 |
| --- | --- | --- |
| Grafana | http://172.16.40.201/ | 宅内のみ。Cilium の L2 Announcement で払い出し |
| Prometheus | `kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090` | ClusterIP のみ |
| Alertmanager | `kubectl -n monitoring port-forward svc/kube-prometheus-stack-alertmanager 9093:9093` | ClusterIP のみ |

> ⚠️ いずれも**インターネットには公開していません**。管理平面を外部に
> 出さない方針です（[docs/20-security-design.md §4](../../../docs/20-security-design.md)）。

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
