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

`root@pam` のパスワードは使いません。**権限を絞った**専用ユーザーのトークンを作ります。

```bash
ssh root@172.16.10.11

# --- 1) 専用ユーザー ---
pveum user add tofu@pve

# --- 2) 必要な権限だけを持つロール ---
pveum role add TofuProvisioner -privs \
  "VM.Allocate,VM.Clone,VM.Config.CDROM,VM.Config.CPU,VM.Config.Cloudinit,\
VM.Config.Disk,VM.Config.HWType,VM.Config.Memory,VM.Config.Network,\
VM.Config.Options,VM.Monitor,VM.Audit,VM.PowerMgmt,\
Datastore.AllocateSpace,Datastore.AllocateTemplate,Datastore.Audit,Sys.Audit"

# --- 3) Kubernetes ノード専用のリソースプールを作る ---
#     VM をプールに入れることで、ACL の適用範囲をそのプールに限定できる。
pveum pool add k8s

# --- 4) ACL を「必要な範囲」だけに付ける（/ には付けない）---
#     ⚠️ `pveum aclmod / ...` としてしまうと、このトークンで
#        Proxmox 上のあらゆる VM・ストレージを操作できてしまう。
#        Kubernetes と無関係な VM（OpenClaw 等）まで巻き込む事故を防ぐため、
#        プールと使用するストレージにだけ権限を与える。
pveum aclmod /pool/k8s              -user tofu@pve -role TofuProvisioner
pveum aclmod /storage/local         -user tofu@pve -role TofuProvisioner
pveum aclmod /storage/local-zfs     -user tofu@pve -role TofuProvisioner

#     VM の作成にはノードへの参照権限も要る（PVEAuditor で十分）
pveum aclmod /nodes -user tofu@pve -role PVEAuditor

# --- 5) トークンを発行する ---
#     ⚠️ --privsep 1（既定）にすること。
#        --privsep 0 は「ユーザーの全権限をそのままトークンへ与える」設定で、
#        トークン単位で権限を絞れなくなる。
pveum user token add tofu@pve provider --privsep 1

#     privsep 1 のトークンには、トークン自身にも ACL が必要
pveum aclmod /pool/k8s            -token 'tofu@pve!provider' -role TofuProvisioner
pveum aclmod /storage/local       -token 'tofu@pve!provider' -role TofuProvisioner
pveum aclmod /storage/local-zfs   -token 'tofu@pve!provider' -role TofuProvisioner
pveum aclmod /nodes               -token 'tofu@pve!provider' -role PVEAuditor
```

出力された `tofu@pve!provider=<uuid>` を控えます（表示は一度きりです）。

> **⚠️ 権限が足りずに `tofu apply` が失敗した場合**
> エラーメッセージに不足している権限が出ます。`/` へ ACL を付けて
> 済ませるのではなく、**必要な権限を特定してから**該当スコープに追加してください。
> 「面倒だから全権限」は、この構成でトークンを分けた意味を失わせます。

> **SSH は不要です。** 本構成は Proxmox API のみで完結する設計にしており
> （machine config は snippets ではなく Talos API 経由で適用）、
> OpenTofu 実行環境に Proxmox の root SSH 権限を持たせません。

### 2.3 ステート暗号化のパスフレーズ

OpenTofu のステートには Kubernetes / etcd / Talos の CA 秘密鍵や
Cloudflare の TunnelSecret が含まれます。**平文で保存させない**ため、
state encryption を必須（`enforced = true`）にしています。

```bash
export TF_VAR_state_encryption_passphrase="$(openssl rand -base64 32)"
echo "$TF_VAR_state_encryption_passphrase"   # パスワードマネージャへ保管する
```

> ⚠️ このパスフレーズを失うとステートを復号できません。age 秘密鍵と同様に
> オフラインでバックアップしてください。
> 未設定のまま `tofu apply` すると、平文で書き込まれるのではなく**失敗します**。

### 2.4 前提チェック

```bash
./scripts/preflight.sh
```

3台すべての`local-zfs`、Proxmox quorum、必要なAPI/ネットワーク到達性を
検証します。Cephは廃止済みであり、残っている場合は警告します。
`local-zfs`が無い場合は、PBSバックアップとCeph解除を確認したうえで
`decommission-ceph.sh`の手順を完了させてください。

### 2.5 旧クラスタの VM を削除する

```bash
./scripts/destroy-legacy-vms.sh          # dry-run（一覧表示のみ）
./scripts/destroy-legacy-vms.sh --yes    # 実際に削除（確認プロンプトあり）
```

## 3. クラスタの構築

### 3.0 クラスタ全体をワンコマンドで再構築する

通常の再構築はリポジトリのルートで次の1コマンドを実行します。

```bash
./scripts/rebuild-talos-cluster.sh \
  --execute \
  --confirm-destroy-six-k8s-vms
```

このコマンドは以下を直列化して実行します。

1. OpenTofuのdestroy planを生成し、対象がVMID `1001`〜`1003`と
   `1101`〜`1103`だけであることを検証する（Gateway VM `1200`は対象外）。
2. レジストリ、WorkerのSecret/PVCデータ、Tailscale Operator/Proxyの状態を
   age暗号化し、6 VMをPBS `172.16.10.51`へバックアップする。
3. 6 VMを削除し、OpenTofuとTalosで3 control plane + 3 workerを再作成する。
4. Gateway API、Cilium、Argo CD/KSOPS、Longhorn CSI、全GitOpsアプリを
   依存順に復元する。
5. レジストリとWorkerデータを復元し、TailscaleのIDとTLSキャッシュを
   Proxyの初回起動前に戻す。
6. 6ノード、etcd、全Pod、全Argo CD Application、Longhorn、Tailnet HTTPSを
   検証し、最後にCloudflare Access → Gateway → Worker → callbackの
   認証付きスモークテストを実行する。

デフォルト実行は読み取り専用のdestroy plan監査です。

```bash
./scripts/rebuild-talos-cluster.sh
```

中断後に既存の復旧セットから再開する場合だけ、次を使います。これは通常運用の
バックアップ取得を省略するため、`BACKUP_DIR`は同スクリプトが作成し検証済みの
ディレクトリを指定してください。

```bash
BACKUP_DIR="$PWD/_out/rebuild-backups/<timestamp>" \
  ./scripts/rebuild-talos-cluster.sh \
  --execute \
  --confirm-destroy-six-k8s-vms \
  --resume-after-backup
```

必要な機密値は平文tfvarsへ置かず、macOS Keychainの次のservice/accountから
読み取ります。

| 用途 | service | account |
|---|---|---|
| OpenTofu state暗号化 | `dev.craftz.homelab.tofu-state` | `talos-k8s` |
| Proxmox API token | `dev.craftz.proxmox.tofu-token` | `tofu@pve!provider` |
| Cloudflare Access client ID | `dev.craftz.ai-business-gateway.cloudflare-access-client-id` | `craftz` |
| Cloudflare Access client secret | `dev.craftz.ai-business-gateway.cloudflare-access-client-secret` | `craftz` |
| Business Gateway API token | `dev.craftz.ai-business-gateway.gateway-api-token` | `craftz` |
| MinIO root password | `dev.craftz.homelab.minio-root` | `minio-root` |
| Loki S3 secret | `dev.craftz.homelab.loki-s3` | `loki` |

初回の外部レジストリ取得ではCilium、Longhorn、監視スタックの展開に時間が
かかります。成功メッセージが出るまでは、途中で起動済みのPodやVMだけを見て
完了と判断しないでください。2026-09-07の実機試験では、6 VMの破棄・再作成、
全Argo CD Application、Tailnet ID/TLS継続、Gatewayジョブ成功、
`tofu plan`差分0まで確認しています。

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

### 3.3 Longhornをworker planeへ限定

```bash
./scripts/reconcile-longhorn-worker-plane.sh
```

Longhorn導入後に実行する。control-plane上に既存レプリカがある場合は1台ずつ
Workerへ再構築し、全volumeがHealthyになってから次へ進む。繰り返し実行可能。

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

通常の管理アクセスは、Tailscaleへ接続したTailnet管理者端末から次を開きます。

```text
https://argocd.tailb6c7d.ts.net
```

この入口はTailscale Kubernetes OperatorのHTTPS Ingressであり、Funnel、
Cloudflare公開、LAN LoadBalancerは使用しません。Tailnet policyは
`autogroup:admin`から`tag:argocd`のTCP/443だけを許可します。障害時の
fallbackとしてのみ、上記の`kubectl port-forward`を使用してください。
クラスタ全再構築ではProxy identityとTLS stateもage暗号化して退避・復元し、
同じMagicDNS名を維持します。

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

### 3.8 Kubernetes GitHub Actions Runner

CIのmain pushと手動実行は、Actions Runner Controller（ARC）が作る
`homelab-runner`へ送ります。Runner Podはジョブごとに破棄され、待機Podは0、
同時実行数は1です。fork由来のpull requestは宅内クラスタへ入れず、引き続き
`ubuntu-latest`で実行します。

GitHub Appは`craftzdev/homelab`だけにインストールし、Repository permissionsの
`Administration: Read and write`と`Metadata: Read-only`だけを付与します。
秘密鍵をGitへ置かず、次のSecretをクラスタへ直接作成します。

```bash
kubectl create namespace arc-runners --dry-run=client -o yaml | kubectl apply -f -
kubectl -n arc-runners create secret generic arc-github-app \
  --from-literal=github_app_id='<APP_ID>' \
  --from-literal=github_app_installation_id='<INSTALLATION_ID>' \
  --from-file=github_app_private_key='<DOWNLOADED_PRIVATE_KEY.pem>'
```

通常のクラスタ全再構築では、このSecretもage暗号化された復旧セットへ退避し、
ARCを同期する前に復元します。

```bash
kubectl -n arc-systems get deploy,pods
kubectl -n arc-runners get autoscalingrunnersets,ephemeralrunnersets,pods
```

### 3.9 AI Business WorkerのGitOps管理

Workerのdesired stateはprivateリポジトリ
`craftzdev/ai-business-worker`の`main:deploy/kubernetes`に置き、Argo CD
Application `ai-business-worker`が自動同期・prune・self-healします。専用
AppProject `ai-business`はこのリポジトリ、`ai-worker` namespace、必要な6種類の
リソースだけを許可します。Runtime/Codex SecretはGitOpsの管理対象に含めません。

Argo CDの読取資格情報はリポジトリ限定・書込不可のGitHub Deploy Keyです。
秘密鍵のbase64値をmacOS Keychain service
`dev.craftz.homelab.argocd-ai-worker-deploy-key`、account
`craftzdev/ai-business-worker`へ保存すると、次の冪等コマンドがrepository Secretを
生成します。秘密鍵自体をGitへコミットしないでください。

```bash
./scripts/bootstrap-cluster-secrets.sh
kubectl -n argocd get application ai-business-worker
```

NamespaceとLonghorn PVCはArgo CD管理下にありますが、誤ったGit変更やApplication
削除で永続データを消さないよう`Prune=false`で保護しています。クラスタ全再構築の
復旧セットにはArgo CD repository Secretもage暗号化して含めます。

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
| Kubernetes の基盤 | `kubernetes/infra/**` | homelabへgit push（ArgoCDが同期） |
| AI Business Worker | `../ai-business-worker/deploy/kubernetes/**` | Workerリポジトリへgit push（ArgoCDが同期） |
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
kubectl -n longhorn-system get pods
kubectl -n longhorn-system get volumes.longhorn.io
kubectl -n longhorn-system logs deploy/longhorn-driver-deployer --tail=100
```

よくある原因:

| 症状 | 原因 | 対処 |
| --- | --- | --- |
| `driver.longhorn.io`を待ち続ける | Longhorn CSIが未収束 | `longhorn-csi-plugin` DaemonSetと`csi-provisioner`を確認 |
| volumeが`degraded`/`faulted` | workerまたはLonghorn専用ディスクが利用不可 | Longhorn node/disk状態と`/var/mnt/longhorn`を確認 |
| attach timeout | 旧PodがRWO volumeを保持 | 旧Podの終了を確認し、volumeが`detached`後に再実行 |

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
tofu taint 'proxmox_virtual_environment_vm.node["k8s-worker-2"]'
tofu apply
```

control-plane の場合は、先に etcd メンバーから外します。

```bash
talosctl -n 172.16.40.11 etcd members
talosctl -n 172.16.40.11 etcd remove-member <member-id>
```

### 6.2 クラスタ全損

通常の計画確認は読み取り専用です。

```bash
./scripts/rebuild-talos-cluster.sh
```

完全再構築は次の**1コマンド**で行います。削除対象はVMID
`1001,1002,1003,1101,1102,1103`に固定され、Gateway VM `1200`はガードで
除外されます。

```bash
./scripts/rebuild-talos-cluster.sh \
  --execute --confirm-destroy-six-k8s-vms
```

このコマンドは、暗号化したWorker/RegistryデータとSecretの退避、PBSへの6 VM
スナップショット、VM破棄・OpenTofu/Talos再作成、Cilium/Argo CD/Longhornと
全GitOps Applicationの復元、Tailnet identityの再作成を順番に行います。最後に
全Applicationの`Synced/Healthy`、全Pod、Business Gateway API、Worker API、callbackを
検証し、どれか一つでも満たさなければ失敗します。

Grafana管理者パスワードはmacOS Keychain service
`dev.craftz.homelab.grafana-admin`に保存され、初回だけ自動生成されます。
外部スモークテスト用Cloudflare Access Service TokenもKeychainの
`dev.craftz.ai-business-gateway.cloudflare-access-client-id`と
`dev.craftz.ai-business-gateway.cloudflare-access-client-secret`から読みます。
外部バックアップ先が未設定のVeleroはベースラインに含めません。現在の復旧点は
PBSスナップショットと、再構築時に作るage暗号化済みApplicationバックアップです。

### 6.3 Longhorn 全損

最も深刻なケースです。以下の順で復旧します。

1. OpenTofuでworker VMとLonghorn専用diskを再作成する
2. Helm/Argo CDでLonghornを再導入する
3. 外部S3のLonghorn backupまたはVeleroからPVCを復元する
4. `reconcile-longhorn-worker-plane.sh` でworker限定配置を再確認する

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
| Longhorn volumeの状態確認 | 週次 | `kubectl -n longhorn-system get volumes.longhorn.io` |
| PBS/暗号化バックアップからの再構築テスト | 四半期 | `rebuild-talos-cluster.sh` の完全再構築 |
| Service Token のローテーション | 90 日 | `service_token_secret_version` を +1 して apply |
| age 鍵のバックアップ確認 | 半期 | パスワードマネージャの内容を確認 |

### 7.2 Git 履歴に残る平文パスワードの除去

> ⚠️ **リポジトリを公開する前に必ず実施してください。**

このリポジトリの Git 履歴には、旧構成の**複数の平文認証情報**が残っています。

| ファイル | 内容 |
| --- | --- |
| `ansible/hosts/k8s-servers/inventory` | ノードの SSH パスワード（`ansible_ssh_pass`） |
| `k8s-manifests/apps/cluster-wide-app-resources/minio-for-velero/secret.yaml` | MinIO の管理者認証情報 |

いずれも作業ツリーからは削除済みですが、**履歴には残っています**。

```bash
# 1) バックアップを取る
git clone --mirror . ../homelab-backup.git

# 2) 履歴から除去する
pip install git-filter-repo
git filter-repo \
  --path ansible/hosts/k8s-servers/inventory \
  --path k8s-manifests/apps/cluster-wide-app-resources/minio-for-velero/secret.yaml \
  --invert-paths

# 3) 除去できたことを確認する
git log --all --oneline -- ansible/hosts/k8s-servers/inventory   # 何も出ないこと

# 4) 強制プッシュする（履歴が書き換わるため、他のクローンは作り直しが必要）
git push --force --all
git push --force --tags
```

> ⚠️ **履歴から消すより先に、認証情報そのものを無効化してください。**
>
> 履歴の書き換えは「これから見る人」に対してしか効きません。既にクローン
> された分、GitHub のキャッシュ、フォーク、CI のログには残り得ます。
> 順序としては
>
>   1. 該当のパスワードを変更する（他で使い回していないかも確認する）
>   2. MinIO のアクセスキーを再発行する
>   3. その上で履歴を書き換える
>
> が正しい対処です。「消したから大丈夫」にはなりません。

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
