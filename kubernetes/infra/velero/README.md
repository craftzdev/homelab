# Velero のセットアップ

Velero は**バックアップ先のオブジェクトストレージが用意されるまで正常に動作しない**。
このディレクトリを ArgoCD へ同期する前に、以下を済ませること。

## 1. なぜクラスタ外に置くのか

旧構成ではクラスタ内の MinIO をバックアップ先にしていた。しかしその MinIO の
PVC も Ceph 上にあるため、**Ceph が壊れればバックアップも同時に失われる**。

| 障害 | クラスタ内 MinIO | クラスタ外 S3 |
| --- | --- | --- |
| PVC の誤削除 | ✅ 復旧できる | ✅ 復旧できる |
| Ceph の論理破損 | ❌ バックアップも消える | ✅ 復旧できる |
| ランサムウェア | ❌ 同時に暗号化される | ✅ 復旧できる |

詳細は [docs/adr/0008-backup-strategy.md](../../../docs/adr/0008-backup-strategy.md) を参照。

## 2. バックアップ先の選択肢

| 選択肢 | 費用 | 備考 |
| --- | --- | --- |
| **Cloudflare R2**（推奨） | 10GB まで無料、egress 無料 | 既に Cloudflare を使っているため管理先が増えない |
| Backblaze B2 | 10GB まで無料 | S3 互換 |
| PBS 上の MinIO | 無料 | 別筐体（172.16.10.51）なので Ceph 障害には耐える。ただし同一宅内なので災害には弱い |

## 3. Cloudflare R2 を使う場合の手順

```bash
# 1) R2 バケットを作成する（Cloudflare ダッシュボード or wrangler）
wrangler r2 bucket create homelab-velero

# 2) R2 の API トークンを作成する
#    Cloudflare ダッシュボード → R2 → Manage R2 API Tokens
#    権限: Object Read & Write、対象バケットを homelab-velero に限定する
#    ⚠️ アカウント全体の権限を与えないこと

# 3) values.yaml のプレースホルダを置き換える
#      bucket: homelab-velero
#      s3Url:  https://<account-id>.r2.cloudflarestorage.com

# 4) 認証情報を SOPS で暗号化して配置する
#    ⚠️ 一時ファイルは必ず削除すること。平文が残ると意味がない。
umask 077
cat > /tmp/velero-creds <<'EOF'
[default]
aws_access_key_id=<R2 の Access Key ID>
aws_secret_access_key=<R2 の Secret Access Key>
EOF

kubectl create secret generic velero-credentials \
  --namespace velero \
  --from-file=cloud=/tmp/velero-creds \
  --dry-run=client -o yaml > /tmp/velero-secret.yaml

sops --encrypt --config ../../../.sops.yaml /tmp/velero-secret.yaml \
  > credentials.sops.yaml

rm -f /tmp/velero-creds /tmp/velero-secret.yaml

# 5) 暗号化されていることを確認する（保険）
grep -q 'ENC\[' credentials.sops.yaml && echo "OK: 暗号化されています"

git add credentials.sops.yaml kustomization.yaml secret-generator.yaml
git commit -m "feat(velero): バックアップ先の認証情報を追加（SOPS 暗号化済み）"
```

### KSOPS で Secret を生成する構成にする

`credentials.sops.yaml` を ArgoCD に復号させるため、
cloudflared / ceph-csi と同じ構成のファイルを 2 つ作ります。

**`kustomization.yaml`**

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: velero
resources:
  - namespace-and-policy.yaml
  - schedules.yaml
generators:
  - secret-generator.yaml
```

**`secret-generator.yaml`**

```yaml
apiVersion: viaduct.ai/v1
kind: ksops
metadata:
  name: velero-secret-generator
  annotations:
    config.kubernetes.io/function: |
      exec:
        path: ksops
files:
  - credentials.sops.yaml
```

あわせて `kubernetes/apps/infrastructure.yaml` の velero Application で、
Git 側 source の `directory.include` を外して kustomize として扱わせます
（`include` を指定していると kustomization.yaml が無視されます）。

## 4. 動作確認

```bash
export KUBECONFIG=_out/kubeconfig

# BackupStorageLocation が Available になっていること
kubectl -n velero get backupstoragelocation

# 手動でバックアップを取ってみる
velero backup create test-backup --include-namespaces default --wait

# 中身を確認する
velero backup describe test-backup --details
```

## 5. 復元テスト（四半期ごと）

**バックアップは復元テストをして初めてバックアップである。**

```bash
# 別名の namespace へ復元して、元の namespace を壊さずに検証する
velero restore create test-restore \
  --from-backup daily-apps-<timestamp> \
  --namespace-mappings default:default-restore-test

kubectl -n default-restore-test get pods,pvc

# 確認後は片付ける
kubectl delete namespace default-restore-test
```

結果は [docs/50-operations.md](../../../docs/50-operations.md) の運用記録に追記すること。

## 6. まだ準備できていない場合

`kubernetes/apps/infrastructure.yaml` の velero Application を一時的に
コメントアウトしてよい。ただし**バックアップが無い状態で本番データを
載せないこと**。
