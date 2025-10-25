# Homelab VM デプロイとネットワーク更新手順

このリポジトリは、Proxmox 上に Kubernetes ノード用の VM を Cloud-Init で一括作成・設定するためのスクリプト群です。Ubuntu 24.04 LTS の Cloud Image をテンプレート化し、VM をクローンして起動します。ネットワーク設定（Cloud-Init スニペット）は後から更新・再適用できます。

## 前提条件
- 実行場所は Proxmox ノード。`qm`、`wget`、`ssh`、`curl` が利用可能であること。
- 共有スニペットストレージと VM ディスクストレージが存在していること（例）
  - スニペット: `cephfs01` に `snippets` が有効化済み（例: `/mnt/pve/cephfs01/snippets`）
  - VM ディスク: `cephrdb_k8s`
- ネットワークブリッジが正しく設定されていること（例: `vmbr1`）。
- ノード間 SSH が可能（スクリプト内で `ssh -n <targetip> qm ...` を使います）。

## 主要スクリプトとパス
- テンプレート作成・VMデプロイ: 
  - `k8s-on-proxmox/cluster-boot-up/scripts/proxmox-host-terminal/deploy-vm.sh`
- ノード初期セットアップ（VM内で cloud-init の `runcmd` から取得・実行）:
  - `k8s-on-proxmox/cluster-boot-up/scripts/nodes/k8s-node-setup.sh`
- ネットワークスニペット（例）:
  - `k8s-on-proxmox/cluster-boot-up/snippets/k8s-wk-3-network.yaml`

## 実行手順
### 1. テンプレート作成と VM クローン（初回）
- 対象ブランチを指定して実行（例: `main`）
```
bash k8s-on-proxmox/cluster-boot-up/scripts/proxmox-host-terminal/deploy-vm.sh main
```
- スクリプトの主な処理
  - Ubuntu Cloud Image をダウンロードしてテンプレート化（EFI を OS ディスク後に作成し、`vm-9050-disk-0` が OS/`scsi0` になるよう順序調整）。
  - `VM_LIST` に従ってクローン、CPU・メモリ設定、Cloud-Init の `user`/`network` スニペットを割り当て、`qm cloudinit update` 実行後に起動。

### 2. ネットワーク設定の更新（再適用）
- ネットワークスニペットを編集後、VMに再適用します。
- 例: `k8s-wk-3` のネットワークを更新（Proxmox ノードで実行）
```
# 1) スニペットを更新（ブランチのRAWから取得する場合の例）
TARGET_BRANCH=main
SNIPPET_TARGET_VOLUME=cephfs01
SNIPPET_TARGET_PATH=/mnt/pve/${SNIPPET_TARGET_VOLUME}/snippets
VMNAME=k8s-wk-3
VMID=1103
curl -s "https://raw.githubusercontent.com/craftzdev/homelab/${TARGET_BRANCH}/k8s-on-proxmox/cluster-boot-up/snippets/${VMNAME}-network.yaml" \
  > "${SNIPPET_TARGET_PATH}/${VMNAME}-network.yaml"

# 2) VM にスニペットを再設定（user を併せて指定すると安全）
qm set ${VMID} --cicustom "user=${SNIPPET_TARGET_VOLUME}:snippets/${VMNAME}-user.yaml,network=${SNIPPET_TARGET_VOLUME}:snippets/${VMNAME}-network.yaml"

# 3) Cloud-Init ISO を更新し、再起動
qm cloudinit update ${VMID}
qm reboot ${VMID}
```
- Cloud-Init は「初回適用のみ」のため、反映されない場合はゲスト内で再適用します。
```
sudo cloud-init clean
sudo reboot
# 必要なら起動後に
sudo netplan apply
```

## 検証と確認
- Proxmox 側
```
# Cloud-Init のネットワーク内容を確認
qm cloudinit dump 1103 network
# VM設定の確認
qm config 1103 | grep cicustom
```
- VM 内
```
cloud-init status --long
sudo cat /etc/netplan/50-cloud-init.yaml
ip a
journalctl -u systemd-networkd --no-pager | tail -n 200
```

## トラブルシュート
- ネットワークが反映されない / IF 名の揺らぎ
  - 既存スニペットが `version: 1` で `name: ens18` 固定の場合、ゲスト側の命名（例: `enp6s18` など）とズレると失敗しやすいです。
  - 推奨: `version: 2` に移行し、`match` + `set-name` を使って NIC を確実に特定してから静的 IP を設定してください。
  - 例（netplan v2）
```
network:
  version: 2
  ethernets:
    net0:
      match:
        macaddress: "<NICのMAC>"
      set-name: "ens18"
      dhcp4: false
      addresses: [172.16.40.23/24]
      gateway4: 172.16.40.1
      nameservers:
        addresses: [192.168.100.1]
```
- Cloud-Init のパッケージアップグレードで失敗（例: `package_update_upgrade_install`）
  - 一時的な APT 失敗のケースがあるため、ネットワーク正常化後に以下を実行し、再度 `cloud-init clean` → 再起動を試してください。
```
sudo apt-get update
sudo apt-get -o Dpkg::Options::="--force-confold" --assume-yes dist-upgrade
```

## よく使うコマンド
```
# Cloud-Init ISO の再生成
qm cloudinit update <VMID>
# VM 再起動
qm reboot <VMID>
# ネットワークスニペットの適用先確認
qm cloudinit dump <VMID> network
# Cloud-Init の状態
cloud-init status --long
```

---
この手順で、テンプレート作成からネットワーク設定の更新・検証まで一貫して行えます。必要に応じて、全ノードのネットワークスニペットを `version: 2` に移行し、`match` + `set-name` で安定運用に切り替えてください。