# Longhorn のセットアップ

## ⚠️ バックアップ先の設定（重要）

**Longhorn は Kubernetes の一部です。** Ceph と違い、クラスタが壊れれば
PV へのアクセスも同時に失われます。外部へのバックアップが必須です。

### スナップショットとバックアップの違い

| | 保存先 | ノード障害 | クラスタ全損 |
| --- | --- | --- | --- |
| **スナップショット**（`snap`） | 同じディスク上 | ✗ 失われる | ✗ 失われる |
| **バックアップ**（`bak`） | 外部オブジェクトストレージ | ✅ 復旧できる | ✅ 復旧できる |

Longhorn UI で「スナップショットを取ったから安心」と思いがちですが、
それは同じディスク上にしかありません。**誤削除からの復旧には使えても、
ディスク障害やクラスタ全損には無力**です。

### Cloudflare R2 を使う場合

```bash
# 1) R2 バケットを作成する
wrangler r2 bucket create homelab-longhorn

# 2) R2 の API トークンを作成する（対象バケットのみに権限を限定）
#    Cloudflare ダッシュボード → R2 → Manage R2 API Tokens

# 3) 認証情報を SOPS で暗号化して配置する
umask 077
cat > /tmp/longhorn-backup.yaml <<'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: longhorn-backup-credential
  namespace: longhorn-system
type: Opaque
stringData:
  AWS_ACCESS_KEY_ID: "<R2 の Access Key ID>"
  AWS_SECRET_ACCESS_KEY: "<R2 の Secret Access Key>"
  AWS_ENDPOINTS: "https://<account-id>.r2.cloudflarestorage.com"
EOF

sops --encrypt --config ../../../.sops.yaml /tmp/longhorn-backup.yaml \
  > backup-credential.sops.yaml
rm -f /tmp/longhorn-backup.yaml

grep -q 'ENC\[' backup-credential.sops.yaml && echo "OK: 暗号化されています"

# 4) values.yaml のバックアップ設定を有効化する
#      defaultSettings:
#        backupTarget: s3://homelab-longhorn@auto/
#        backupTargetCredentialSecret: longhorn-backup-credential

# 5) KSOPS 用の kustomization を追加してコミットする
#    （cloudflared / monitoring と同じ構成）
git add backup-credential.sops.yaml values.yaml
git commit -m "feat(longhorn): バックアップ先を設定（SOPS 暗号化済み）"
```

### 定期バックアップの設定

`RecurringJob` CRD で自動化します。

```yaml
apiVersion: longhorn.io/v1beta2
kind: RecurringJob
metadata:
  name: daily-backup
  namespace: longhorn-system
spec:
  cron: "0 17 * * *"   # 02:00 JST（UTC で指定）
  task: backup
  groups:
    - default          # default グループの全ボリュームが対象
  retain: 14           # 14 世代保持
  concurrency: 2
```

## Talos 特有の前提

以下が揃っていないと Longhorn は起動しません。
`tofu/10-proxmox-talos` が設定済みですが、トラブル時の確認箇所として記載します。

```bash
export TALOSCONFIG=_out/talosconfig

# 1) system extension が入っているか
talosctl -n 172.16.40.11 get extensions
#    → iscsi-tools と util-linux-tools が見えること

# 2) データ用ボリュームがマウントされているか
talosctl -n 172.16.40.11 get uservolumestatus
talosctl -n 172.16.40.11 ls /var/mnt/longhorn
#    → マウントされていること

# 3) iscsid が動いているか
talosctl -n 172.16.40.11 services | grep -i iscsi
```

## アクセス方法

```bash
# ⚠️ Longhorn UI はボリュームの削除やノードの無効化ができる管理ツール。
#    Ingress は作っていない（外部公開しない）。
kubectl -n longhorn-system port-forward svc/longhorn-frontend 8080:80
# → http://localhost:8080
```

## 動作確認

```bash
export KUBECONFIG=_out/kubeconfig

# ノードが全て Ready で、ディスクがスケジュール可能か
kubectl -n longhorn-system get nodes.longhorn.io -o wide

# テスト用 PVC を作ってみる
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: longhorn-test
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: longhorn
  resources:
    requests:
      storage: 1Gi
EOF

kubectl get pvc longhorn-test        # Bound になること
kubectl delete pvc longhorn-test     # 後片付け
```

## dedicated worker planeのreconcile

LonghornのHelm設定はworker selectorを宣言しているが、CSI Deploymentや
engine-image DaemonSetの一部はLonghorn自身が生成する。既存クラスタを3+3へ
移行した場合は、次の冪等スクリプトでcontrol-plane上のレプリカを1台ずつ
安全に退避し、生成済みリソースにもselectorを反映する。

```bash
export KUBECONFIG=_out/kubeconfig
scripts/reconcile-longhorn-worker-plane.sh
```

各control-planeのレプリカが0件、全volumeが`healthy`になるまで待ってから
次のノードへ進む。全control-planeを連続再起動してはならない。

## よくある失敗

| 症状 | 原因 | 対処 |
| --- | --- | --- |
| Pod が `ContainerCreating` のまま | `iscsi-tools` extension が無い | Image Factory の schematic を確認し、Talos を再インストール |
| ボリュームが `Degraded` から戻らない | レプリカを置けるノードが足りない | `kubectl -n longhorn-system get nodes.longhorn.io` でスケジュール可能なノード数を確認 |
| `/var/mnt/longhorn` が空 | `UserVolumeConfig` の diskSelector がディスクを見つけていない | `talosctl get disks` で 2 本目のディスクが見えているか確認 |
| マウントは成功するが Pod から見えない | kubelet の `extraMounts` が `rshared` でない | machine config を確認 |
