# ログ基盤（Loki / Alloy / MinIO）

## 構成

```text
Kubernetes Pod logs ─ Alloy DaemonSet ─┐
Kubernetes Events   ─ Alloy Deployment ├─ Loki SingleBinary ─ MinIO
API audit files     ─ Alloy DaemonSet ─┘                     └─ Longhorn x2
```

- `logging`: Loki、通常ログ/Event用Alloy、MinIO
- `logging-audit`: control-planeの`/var/log/audit/kube`だけを読むAlloy
- MinIO: 1 Pod、100GiB、`longhorn-logging-retain`（2 replica）
- Loki WAL/cache: 20GiB、同StorageClass
- 保持期間: アプリ14日、基盤30日、Kubernetes監査90日
- Grafana datasource: `Loki`として自動登録

MinIOはGrafana Loki chart 7.3が採用するメンテナンス版
`pgsty/minio`を固定バージョンで使用する。MinIO自身を分散構成にせず、
Longhornとの二重レプリケーションを避ける。

## Secret

SecretはGitに置かない。次を実行すると、初回だけmacOS Keychainへ生成して
クラスタへ投入する。再実行は同じ値を使うため冪等である。

```bash
./scripts/bootstrap-cluster-secrets.sh
```

| Kubernetes Secret | Keychain service / account | 用途 |
|---|---|---|
| `minio-root-credentials` | `dev.craftz.homelab.minio-root` / `minio-root` | MinIO管理者 |
| `loki-s3-credentials` | `dev.craftz.homelab.loki-s3` / `loki` | Loki専用S3ユーザー |

Lokiへ管理者資格情報は渡さない。`minio-bucket-bootstrap` CronJobがLoki専用の
バケット、ユーザー、最小権限policyを冪等に管理する。

## 確認

```bash
export KUBECONFIG="$PWD/_out/kubeconfig"
kubectl -n logging get pods,pvc
kubectl -n logging create job --from=cronjob/minio-bucket-bootstrap \
  minio-bucket-bootstrap-manual
kubectl -n logging wait --for=condition=complete \
  job/minio-bucket-bootstrap-manual --timeout=5m

# Loki ready
kubectl -n logging port-forward svc/loki 3100:3100
curl -fsS http://127.0.0.1:3100/ready

# Grafana Exploreで確認するラベル
# {cluster="homelab"}
# {log_class="audit"}
```

MinIO Console、MinIO API、Loki APIは外部公開しない。緊急調査時だけ
`kubectl port-forward`を使う。

## バックアップ境界

Longhorn replicaは冗長化でありバックアップではない。PBSによる6ノードVMの
バックアップにはLonghornディスクとMinIOデータも含まれる。実測したPBSの保存先は
`172.16.10.51`のdatastore `gateway-backup`（Proxmox側ID `pbs-gateway`）である。
`scripts/reconcile-pbs-kubernetes-backup.sh --apply`が6 VMを毎日02:30 JSTにsnapshotし、
daily 7 / weekly 4 / monthly 3世代を保持する。バックアップ負荷を抑えるため50MiB/s、
I/O idle priorityに制限する。

これはクラスタ全損時の復旧点であり、MinIOの独立したS3コピーではない。
PBS datastore内部へ通常ファイルを直接書くとdatastoreを破損しうるため禁止する。
オブジェクト単位の復旧が必要になった場合は、PBS本体へ別MinIOを同居させず、
別NASまたはオフサイトS3へ`mc mirror`する。
