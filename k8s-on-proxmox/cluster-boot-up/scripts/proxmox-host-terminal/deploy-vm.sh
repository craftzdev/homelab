#!/usr/bin/env bash

#region set variables
TARGET_BRANCH="${1:-${TARGET_BRANCH:-main}}"
TEMPLATE_VMID=${TEMPLATE_VMID:-9050}
CLOUDINIT_IMAGE_TARGET_VOLUME=${CLOUDINIT_IMAGE_TARGET_VOLUME:-cephfs01}
TEMPLATE_BOOT_IMAGE_TARGET_VOLUME=${TEMPLATE_BOOT_IMAGE_TARGET_VOLUME:-cephfs01}
BOOT_IMAGE_TARGET_VOLUME=${BOOT_IMAGE_TARGET_VOLUME:-cephfs01}
SNIPPET_TARGET_VOLUME=${SNIPPET_TARGET_VOLUME:-cephfs01}              # content: snippets を有効に（CephFS/NFS 等の共有ファイルストレージ推奨）
SNIPPET_TARGET_PATH=${SNIPPET_TARGET_PATH:-/mnt/pve/${SNIPPET_TARGET_VOLUME}/snippets}                     # Ubuntu 24.04
CEPH_POOL_NAME=${CEPH_POOL_NAME:-cephrdb_k8s}

# Network (Proxmox Cloud-Init 推奨パラメータ使用)
VLAN_ID=${VLAN_ID:-40}
VLAN_BRIDGE=${VLAN_BRIDGE:-vmbr1}
NODE_GATEWAY=${NODE_GATEWAY:-172.16.40.1}
NODE_CIDR_SUFFIX=${NODE_CIDR_SUFFIX:-24}                           # xxx.xxx.xxx.xxx/24
NAMESERVERS=${NAMESERVERS:-"172.16.40.1"}
SEARCHDOMAIN=${SEARCHDOMAIN:-home.arpa}

# VM inventory: vmid name vCPU mem(MiB) targetip targethost
VM_LIST=(
  "1001 k8s-cp-1  4 8192  172.16.40.11  sv-proxmox-01"
  "1002 k8s-cp-2  4 8192  172.16.40.12  sv-proxmox-02"
  "1003 k8s-cp-3  4 8192  172.16.40.13  sv-proxmox-03"
  "1101 k8s-wk-1  4 8192  172.16.40.21  sv-proxmox-01"
  "1102 k8s-wk-2  4 8192  172.16.40.22  sv-proxmox-02"
  "1103 k8s-wk-3  4 8192  172.16.40.23  sv-proxmox-03"
)

REPOSITORY_RAW_SOURCE_URL="https://raw.githubusercontent.com/craftzdev/homelab/${TARGET_BRANCH}"

# SSH 接続先の識別（host か ip）
SSH_CONNECT_FIELD=${SSH_CONNECT_FIELD:-host}
RETRY_DELAY=${RETRY_DELAY:-2}
# クラスタ関連の厳格チェックを要求する場合に true（単一ノードでは false 推奨）
REQUIRE_CLUSTER=${REQUIRE_CLUSTER:-false}

### ======== Pre-checks（簡潔化） ========
# 共有ストレージ優先設定（Ceph RBD が存在すれば VM ディスクは共有ストレージを使用）
# 優先順位: 明示指定(CEPH_STORAGE) > 指定プール名一致(CEPH_POOL_NAME)
# 環境変数で無効化したい場合は PREFER_SHARED_STORAGE=false を指定してください。
# ストレージ選択の自動化は廃止。既定値や環境変数の明示指定に委ねます。

# Storage 存在チェック
[ -d "$SNIPPET_TARGET_PATH" ] || mkdir -p "$SNIPPET_TARGET_PATH"

ssh_exec(){ local host=$1; shift; ssh -o BatchMode=yes -n "$host" "$@"; }

# VM_LIST 事前検証（重複/形式チェック）
# 事前検証は簡略化し、Proxmox/Cloud-Init のエラーに委ねます。

# targethost がクラスタに存在するか（任意）
# クラスタノード存在確認は省略。

# SSH 接続先ユニーク化（移行先ノードも含める）
# SSH 事前登録や到達性チェックは省略。

### ======== Template (cloud image) ========
# download the image(ubuntu 24.04 LTS)
wget https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img

qm create "$TEMPLATE_VMID" --cores 2 --memory 4096 --name k8s-template \
  --net0 virtio,bridge=${VLAN_BRIDGE} \
  --agent enabled=1,fstrim_cloned_disks=1

qm importdisk $TEMPLATE_VMID noble-server-cloudimg-amd64.img $TEMPLATE_BOOT_IMAGE_TARGET_VOLUME
qm set "$TEMPLATE_VMID" --scsihw virtio-scsi-pci --scsi0 "$TEMPLATE_BOOT_IMAGE_TARGET_VOLUME:vm-$TEMPLATE_VMID-disk-0"
qm set "$TEMPLATE_VMID" --ide2 "$CLOUDINIT_IMAGE_TARGET_VOLUME":cloudinit
qm set "$TEMPLATE_VMID" --boot order=scsi0 --serial0 socket --vga serial0

echo "[INFO] Converting to template"
qm template "$TEMPLATE_VMID"
rm -f "$UBUNTU_IMG" SHA256SUMS

### ======== Per-VM provisioning ========
# finally attach the new disk to the VM as scsi drive
qm set $TEMPLATE_VMID --scsihw virtio-scsi-pci --scsi0 $TEMPLATE_BOOT_IMAGE_TARGET_VOLUME:vm-$TEMPLATE_VMID-disk-0

# add Cloud-Init CD-ROM drive
qm set $TEMPLATE_VMID --ide2 $CLOUDINIT_IMAGE_TARGET_VOLUME:cloudinit

# set the bootdisk parameter to scsi0
qm set "$TEMPLATE_VMID" --boot order=scsi0 --serial0 socket --vga serial0

# migrate to template
qm template $TEMPLATE_VMID

# cleanup
rm noble-server-cloudimg-amd64.img

# Create snippet for cloud-init (user-config)
# START irregular indent because heredoc
for array in "${VM_LIST[@]}"
do
    echo "${array}" | while read -r vmid vmname cpu mem targetip targethost
    do
        # clone from template
        # in clone phase, can't create vm-disk to local volume
        qm clone "${TEMPLATE_VMID}" "${vmid}" --name "${vmname}" --full true --target "${targethost}"
        
        # set compute resources
        ssh -n "${targetip}" qm set "${vmid}" --cores "${cpu}" --memory "${mem}"

        # move vm-disk to local
        ssh -n "${targetip}" qm move-disk "${vmid}" scsi0 "${BOOT_IMAGE_TARGET_VOLUME}" --delete true

        # resize disk (Resize after cloning, because it takes time to clone a large disk)
        ssh -n "${targetip}" qm resize "${vmid}" scsi0 100G
# ----- #
cat > "$SNIPPET_TARGET_PATH"/"$vmname"-user.yaml << EOF
#cloud-config
hostname: ${vmname}
timezone: Asia/Tokyo
manage_etc_hosts: true
chpasswd:
  expire: False
users:
  - default
  - name: cloudinit
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: false
    # mkpasswd --method=SHA-512 --rounds=4096
    # password is zaq12wsx
    passwd: \$6\$rounds=4096\$Xlyxul70asLm\$9tKm.0po4ZE7vgqc.grptZzUU9906z/.vjwcqz/WYVtTwc5i2DWfjVpXb8HBtoVfvSY61rvrs/iwHxREKl3f20
ssh_pwauth: true
ssh_authorized_keys: []
package_upgrade: true
runcmd:
  # set ssh_authorized_keys
  - su - cloudinit -c "mkdir -p ~/.ssh && chmod 700 ~/.ssh"
  - su - cloudinit -c "curl -sS https://github.com/craftzdev.keys >> ~/.ssh/authorized_keys"
  - su - cloudinit -c "chmod 600 ~/.ssh/authorized_keys"
  # run install scripts
  - su - cloudinit -c "curl -s ${REPOSITORY_RAW_SOURCE_URL}/k8s-on-proxmox/cluster-boot-up/scripts/nodes/k8s-node-setup.sh > ~/k8s-node-setup.sh"
  - su - cloudinit -c "sudo bash ~/k8s-node-setup.sh ${vmname} ${TARGET_BRANCH}"
  # change default shell to bash
  - chsh -s $(which bash) cloudinit
EOF
# ----- #
        # END irregular indent because heredoc
        
        # download snippet for cloud-init(network)
        curl -s "${REPOSITORY_RAW_SOURCE_URL}/k8s-on-proxmox/cluster-boot-up/snippets/${vmname}-network.yaml" > "${SNIPPET_TARGET_PATH}"/"${vmname}"-network.yaml

        # set snippet to vm
        ssh -n "${targetip}" qm set "${vmid}" --cicustom "user=${SNIPPET_TARGET_VOLUME}:snippets/${vmname}-user.yaml,network=${SNIPPET_TARGET_VOLUME}:snippets/${vmname}-network.yaml"

        # start vm
        ssh -n "${targetip}" qm start "${vmid}"

    done
done

# endregion