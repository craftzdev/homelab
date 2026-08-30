# 50. 構築手順と運用

## 1. 必要なツール

```bash
brew install opentofu kubectl helm sops age jq
brew install siderolabs/tap/talosctl
brew install cilium-cli velero   # 任意（動作確認とバックアップ操作に便利）
```

## 2. 事前準備

### 2.1 age 鍵の作成（1 度だけ）

```bash
mkdir -p ~/.config/sops/age
age-keygen -o ~/.config/sops/age/keys.txt
chmod 600 ~/.config/sops/age/keys.txt
```

> ⚠️ **この鍵を失うと、リポジトリ内の暗号化された秘密が全て復号不能になります。**
> パスワードマネージャ等にオフラインでバックアップしてください。

出力された public key を `.sops.yaml` の `age:` に記入します（3 箇所すべて）。

### 2.2 Proxmox API トークンの作成

`root@pam` のパスワードは使いません。専用ユーザーのトークンを作ります。

```bash
ssh root@172.16.10.11

pveum user add tofu@pve

pveum role add TofuProvisioner -privs \
  "VM.Allocate,VM.Clone,VM.Config.CDROM,VM.Config.CPU,VM.Config.Cloudinit,\
VM.Config.Disk,VM.Config.HWType,VM.Config.Memory,VM.Config.Network,\
VM.Config.Options,VM.Monitor,VM.Audit,VM.PowerMgmt,\
Datastore.AllocateSpace,Datastore.AllocateTemplate,Datastore.Audit,Sys.Audit"

pveum aclmod / -user tofu@pve -role TofuProvisioner

# トークンを発行（表示される値は一度しか見られない）
pveum user token add tofu@pve provider --privsep 0
```

出力された `tofu@pve!provider=<uuid>` を控えます。

### 2.3 前提チェック

```bash
./scripts/preflight.sh
```

**Ceph が `HEALTH_WARN` の場合、このスクリプトは失敗します。** これは意図的です。
Kubernetes の PV はこの Ceph の上に載るため、ストレージ層の不安定さが
そのままアプリの不安定さになります。原因の切り分け手順は
[docs/30-storage-design.md §2](30-storage-design.md) を参照してください。

承知の上で進める場合のみ `--skip-ceph-health` を付けます。

### 2.4 旧クラスタの VM を削除する

```bash
./scripts/destroy-legacy-vms.sh          # dry-run（一覧表示のみ）
./scripts/destroy-legacy-vms.sh --yes    # 実際に削除（確認プロンプトあり）
```

## 3. クラスタの構築

### 3.1 VM 作成と Talos の bootstrap

```bash
cd tofu/10-proxmox-talos
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars      # proxmox_api_token 等を設定

tofu init
tofu plan                     # 何が作られるか必ず確認する
tofu apply
```

所要時間は約 10〜15 分（ISO のダウンロードと VM の起動待ちを含む）。

完了すると `_out/kubeconfig` と `_out/talosconfig` が生成されます。

```bash
export TALOSCONFIG="$(pwd)/../../_out/talosconfig"
export KUBECONFIG="$(pwd)/../../_out/kubeconfig"

talosctl -n 172.16.40.11 get members
kubectl get nodes            # この時点では全ノードが NotReady（CNI が無いため）
```

### 3.2 Cilium の導入

```bash
cd ../..
./scripts/bootstrap-cluster.sh
kubectl get nodes            # 全ノードが Ready になる
```

### 3.3 Ceph の認証情報

```bash
./scripts/ceph-create-k8s-user.sh
git add kubernetes/infra/ceph-csi/secrets.sops.yaml
git commit -m "feat(ceph-csi): Ceph の認証情報を追加（SOPS 暗号化済み）"
git push
```

### 3.4 ArgoCD の導入

```bash
./scripts/bootstrap-argocd.sh
```

**必ず初期パスワードを変更してください。**

```bash
kubectl -n argocd port-forward svc/argocd-server 8080:80 &
argocd login localhost:8080 --username admin --insecure
argocd account update-password
kubectl -n argocd delete secret argocd-initial-admin-secret
```

### 3.5 Grafana の管理者パスワード

**この手順を飛ばすと Grafana は起動しません。** これは意図的な設計です
（既定パスワードで静かに起動するより、明確に失敗する方が安全なため）。

手順は [kubernetes/infra/monitoring/README.md](../kubernetes/infra/monitoring/README.md)
を参照してください。要約すると、強いパスワードを生成して SOPS で暗号化し、
`grafana-admin.sops.yaml` としてコミットします。

### 3.6 Cloudflare の設定

```bash
cd tofu/20-cloudflare
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars

export TF_VAR_cloudflare_api_token='...'
tofu init && tofu apply

cd ../..
./scripts/sync-cloudflare-secrets.sh

git add kubernetes/infra/cloudflared/credentials.sops.yaml \
        kubernetes/infra/cloudflared/generated/ingress-configmap.yaml
git commit -m "feat(cloudflared): Tunnel の設定を追加"
git push
```

### 3.7 疎通確認

```bash
# 認証情報なしでは 401 が返ること（200 が返ったら設定ミス）
curl -s -o /dev/null -w '%{http_code}\n' https://api.internal.example.com/

# Service Token 付きなら通ること
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "CF-Access-Client-Id: $(cd tofu/20-cloudflare && tofu output -raw service_token_client_id)" \
  -H "CF-Access-Client-Secret: $(cd tofu/20-cloudflare && tofu output -raw service_token_client_secret)" \
  https://api.internal.example.com/
```

---

## 4. 日常の運用

### 4.1 よく使うコマンド

| やりたいこと | コマンド |
| --- | --- |
| ノードの状態 | `talosctl -n 172.16.40.11 health` |
| ノードのログ | `talosctl -n 172.16.40.11 logs kubelet` |
| ノードのプロセス | `talosctl -n 172.16.40.11 processes` |
| ノードのディスク | `talosctl -n 172.16.40.11 get disks` |
| NIC の一覧（MAC 確認） | `talosctl -n 172.16.40.11 get links` |
| etcd メンバー | `talosctl -n 172.16.40.11 get members` |
| 適用中の設定 | `talosctl -n 172.16.40.11 get machineconfig -o yaml` |
| ArgoCD の同期状況 | `kubectl -n argocd get applications` |
| 通信の可視化 | `kubectl -n kube-system exec -it ds/cilium -- hubble observe --follow` |
| 落ちている通信 | `... hubble observe --verdict DROPPED --last 100` |
| Hubble UI | `kubectl -n kube-system port-forward svc/hubble-ui 12000:80` |
| 脆弱性レポート | `kubectl get vulnerabilityreports -A` |

> Talos には SSH がありません。ノードの調査は `talosctl` で行います。
> シェルが無いことは不便ですが、それが攻撃者にとっても同じであることが
> この構成の要点です（[ADR-0001](adr/0001-talos-linux.md)）。

### 4.2 設定を変更する

**すべての変更は Git 経由で行います。** `kubectl edit` や `talosctl apply-config` を
手で叩くと、ArgoCD の self-heal や次回の `tofu apply` で巻き戻ります。

| 変更したいもの | 編集するファイル | 反映方法 |
| --- | --- | --- |
| ノードのスペック・台数 | `tofu/10-proxmox-talos/terraform.tfvars` | `tofu apply` |
| Talos の設定 | `talos/patches/*.yaml.tftpl` | `tofu apply` |
| Kubernetes のアプリ | `kubernetes/infra/**` | git push（ArgoCD が同期） |
| 外部公開するサービス | `tofu/20-cloudflare/terraform.tfvars` | `tofu apply` → git push |

### 4.3 アップグレード

#### Talos

```bash
# 1 台ずつ実施する。次のノードに進む前に必ず Ready を確認すること。
talosctl -n 172.16.40.11 upgrade \
  --image factory.talos.dev/installer/<schematic-id>:v1.13.10

kubectl get nodes -w
```

schematic ID は `cd tofu/10-proxmox-talos && tofu output talos_schematic_id` で取得できます。
完了後、`terraform.tfvars` の `talos_version` も更新してコミットしてください
（コードと実態を一致させるため）。

#### Kubernetes

```bash
talosctl -n 172.16.40.11 upgrade-k8s --to v1.34.4
```

#### Helm chart

Renovate が PR を出します。内容を確認してマージすると ArgoCD が同期します。

---

## 5. トラブルシューティング

### ノードに到達できない

Talos の ingressFirewall を誤設定した可能性があります。本構成は
**nocloud プラットフォーム**を使っており、初期 IP は Proxmox の cloud-init から
与えられるため、以下の手順で確実に復旧できます。

1. `talos/patches/ingress-firewall.yaml.tftpl` を修正する
2. `cd tofu/10-proxmox-talos && tofu apply`
3. それでも到達できない場合は Proxmox から VM を再起動する

Proxmox の Console でシリアルコンソールを開けば、起動時のログを確認できます。

### PVC が Pending のまま

```bash
kubectl describe pvc <name>
kubectl -n ceph-csi logs -l app=ceph-csi-rbd-provisioner -c csi-rbdplugin
```

よくある原因:

| 症状 | 原因 | 対処 |
| --- | --- | --- |
| `clusterID` のエラー | StorageClass の fsid が実際の Ceph と不一致 | `ceph fsid` で確認して修正 |
| MON への接続タイムアウト | ノードから VLAN20 へ到達できていない | `talosctl -n <node> get addresses` で eth1 の IP を確認 |
| 認証エラー | cephx の権限不足 | `ceph auth get client.k8s-rbd` で caps を確認 |

### 通信が落ちている

```bash
kubectl -n kube-system exec -it ds/cilium -- \
  hubble observe --namespace <ns> --verdict DROPPED --last 50
```

NetworkPolicy の許可漏れが原因のことがほとんどです。
`kubernetes/infra/policies/default-deny-template.yaml` のコメントを参照してください。

### ArgoCD の Application が同期に失敗する

```bash
kubectl -n argocd get applications
kubectl -n argocd describe application <name>
kubectl -n argocd logs deploy/argocd-repo-server -c repo-server --tail=100
```

`ksops` 関連のエラーが出る場合は、age 秘密鍵の Secret が正しくマウントされているか
確認してください。

```bash
kubectl -n argocd exec deploy/argocd-repo-server -c repo-server -- \
  sh -c 'command -v ksops; ls -l $SOPS_AGE_KEY_FILE'
```

---

## 6. 災害復旧

### 6.1 ノード 1 台の障害

Talos ノードはステートレスに近いため、作り直すのが最も速い方法です。

```bash
# Proxmox から該当 VM を削除し、tofu で再作成する
cd tofu/10-proxmox-talos
tofu taint 'proxmox_virtual_environment_vm.node["k8s-wk-2"]'
tofu apply
```

control-plane の場合は、先に etcd メンバーから外します。

```bash
talosctl -n 172.16.40.11 etcd members
talosctl -n 172.16.40.11 etcd remove-member <member-id>
```

### 6.2 クラスタ全損

**前提**: `_out/talosconfig`、age 秘密鍵、etcd スナップショット、
OpenTofu のステートが手元にあること。

```bash
# 1) VM を作り直す
cd tofu/10-proxmox-talos
tofu apply

# 2) etcd スナップショットから復元する
talosctl -n 172.16.40.11 bootstrap \
  --recover-from ./_out/etcd-snapshots/etcd-<timestamp>.snapshot

# 3) Cilium と ArgoCD を再導入する
./scripts/bootstrap-cluster.sh
./scripts/bootstrap-argocd.sh
```

etcd スナップショットが無い場合でも、GitOps を徹底していればクラスタは
Git から再構築できます。ただし PVC の中身は Velero から復元する必要があります。

### 6.3 Ceph 全損

最も深刻なケースです。以下の順で復旧します。

1. Proxmox 側で Ceph を再構築する（本リポジトリのスコープ外）
2. PBS から Kubernetes ノード VM を復元する
3. Velero で PVC の中身を復元する（**バックアップ先がクラスタ外にあることが前提**）

> これが、[ADR-0008](adr/0008-backup-strategy.md) で
> 「Velero のバックアップ先をクラスタ内 MinIO にしてはならない」と
> 定めた理由です。

---

## 7. セキュリティ運用

### 7.1 定期的に実施すること

| 項目 | 頻度 | コマンド / 手順 |
| --- | --- | --- |
| etcd スナップショット | 日次 | `./scripts/etcd-snapshot.sh`（cron 化推奨） |
| 脆弱性レポートの確認 | 週次 | `kubectl get vulnerabilityreports -A` |
| Renovate の PR 対応 | 週次 | GitHub の PR を確認 |
| Ceph の状態確認 | 週次 | `ssh root@172.16.10.11 ceph -s` |
| Velero の復元テスト | 四半期 | [velero/README.md](../kubernetes/infra/velero/README.md) §5 |
| Service Token のローテーション | 90 日 | `service_token_secret_version` を +1 して apply |
| age 鍵のバックアップ確認 | 半期 | パスワードマネージャの内容を確認 |

### 7.2 Git 履歴に残る平文パスワードの除去

> ⚠️ **リポジトリを公開する前に必ず実施してください。**

このリポジトリの Git 履歴には、旧構成の Ansible インベントリに書かれていた
平文の SSH パスワード（`ansible/hosts/k8s-servers/inventory` の `ansible_ssh_pass`）が
残っています。

```bash
# 1) バックアップを取る
git clone --mirror . ../homelab-backup.git

# 2) 履歴から除去する
pip install git-filter-repo
git filter-repo --path ansible/hosts/k8s-servers/inventory --invert-paths

# 3) 強制プッシュする（履歴が書き換わるため、他のクローンは作り直しが必要）
git push --force --all
git push --force --tags
```

**さらに重要**: 履歴から消しても、そのパスワードが他で使い回されている場合は
意味がありません。該当のパスワードは変更してください。

### 7.3 OpenTofu ステートの保護

`tofu/*/terraform.tfstate` には以下が**平文で**含まれます。

- Kubernetes / etcd / Talos の CA 秘密鍵
- Cloudflare Tunnel の TunnelSecret
- Access Service Token の client_secret

これらを奪われることは、クラスタと外部公開経路を完全に掌握されることと同じです。

- 最低限: ディスク暗号化された端末でのみ扱う（`.gitignore` 済み）
- 推奨: 暗号化・バージョニング有効なリモートバックエンドへ移行する
  （`tofu/10-proxmox-talos/versions.tf` にコメントで設定例を記載）

---

## 8. 運用記録

実施した検証やインシデントをここに追記していきます。

| 日付 | 内容 | 結果 |
| --- | --- | --- |
| — | — | — |
