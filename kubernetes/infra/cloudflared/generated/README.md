# generated/

このディレクトリのファイルは **OpenTofu が生成します**。手で編集しないでください。

| ファイル | 生成元 | 内容 |
| --- | --- | --- |
| `ingress-configmap.yaml` | `tofu/20-cloudflare` | cloudflared の ingress ルール（ホスト名 → クラスタ内 Service） |

## なぜ生成物をコミットするのか

Cloudflare Tunnel の設定を `config_src = "local"` にしているため、ingress ルールは
Kubernetes 側の ConfigMap として ArgoCD が管理します。一方、同じホスト名の情報は
Cloudflare Access のアプリケーション定義にも必要です。

両方を手で書くと片方の更新漏れが起き、「Access では守っているが cloudflared の
ルートが無い」あるいはその逆という不整合が生まれます。そこで
`tofu/20-cloudflare/variables.tf` の `published_services` を単一の情報源とし、
そこから両方を生成する形にしています。

## 更新手順

```bash
cd tofu/20-cloudflare
$EDITOR terraform.tfvars      # published_services を編集
tofu apply                    # このディレクトリのファイルが更新される

cd ../..
git add kubernetes/infra/cloudflared/generated/ingress-configmap.yaml
git commit -m "feat(cloudflared): 公開サービスを更新"
git push                      # ArgoCD が同期する
```

## 初回構築時の注意

`tofu apply` を実行するまで `ingress-configmap.yaml` は存在しません。
そのため cloudflared の Application は初回同期に失敗します。これは想定通りです。
`tofu/20-cloudflare` の apply と `scripts/sync-cloudflare-secrets.sh` を
実行した後、ArgoCD が正常に同期します。
