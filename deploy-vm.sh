#!/usr/bin/env bash
# Idempotent Proxmox VM deployer (cloud-init)
set -Eeuo pipefail
IFS=$'\n\t'
SCRIPT_NAME="$(basename "$0")"
LOG_FILE="$(pwd)/deploy-vm.log"
exec > >(tee -a "$LOG_FILE") 2>&1
trap 'echo "[ERROR] ${SCRIPT_NAME}: line ${LINENO} failed"; exit 1' ERR

### ======== Config (env で上書き可) ========
TARGET_BRANCH="${1:-${TARGET_BRANCH:-main}}"

# Template/Storage
TEMPLATE_VMID=${TEMPLATE_VMID:-9050}
CLOUDINIT_IMAGE_TARGET_VOLUME=${CLOUDINIT_IMAGE_TARGET_VOLUME:-local-lvm}
TEMPLATE_BOOT_IMAGE_TARGET_VOLUME=${TEMPLATE_BOOT_IMAGE_TARGET_VOLUME:-local-lvm}
BOOT_IMAGE_TARGET_VOLUME=${BOOT_IMAGE_TARGET_VOLUME:-local-lvm}
SNIPPET_TARGET_VOLUME=${SNIPPET_TARGET_VOLUME:-cephfs01}              # content: snippets を有効に（CephFS/NFS 等の共有ファイルストレージ推奨）
SNIPPET_TARGET_PATH=${SNIPPET_TARGET_PATH:-/mnt/pve/${SNIPPET_TARGET_VOLUME}/snippets}
UBUNTU_IMG="noble-server-cloudimg-amd64.img"                       # Ubuntu 24.04
DISK_SIZE=${DISK_SIZE:-30G}
# Ceph プール名（rbd ストレージの pool と一致するものを優先選択）
CEPH_POOL_NAME=${CEPH_POOL_NAME:-cephrdb_k8s}

# Network (Proxmox Cloud-Init 推奨パラメータ使用)
VLAN_ID=${VLAN_ID:-40}
VLAN_BRIDGE=${VLAN_BRIDGE:-vmbr1}
NODE_GATEWAY=${NODE_GATEWAY:-172.16.40.1}
NODE_CIDR_SUFFIX=${NODE_CIDR_SUFFIX:-24}                           # xxx.xxx.xxx.xxx/24
NAMESERVERS=${NAMESERVERS:-"172.16.40.1"}
SEARCHDOMAIN=${SEARCHDOMAIN:-home.arpa}

# VM inventory: vmid name vCPU mem(MiB) ip targetip targethost
VM_LIST=(
  "1001 k8s-cp-1 4 8192 172.16.40.11 - sv-proxmox-01"
  "1002 k8s-cp-2 4 8192 172.16.40.12 - sv-proxmox-01"
  "1003 k8s-cp-3 4 8192 172.16.40.13 - sv-proxmox-01"
  "1101 k8s-wk-1 4 8192 172.16.40.21 - sv-proxmox-01"
  "1102 k8s-wk-2 4 8192 172.16.40.22 - sv-proxmox-01"
  "1103 k8s-wk-3 4 8192 172.16.40.23 - sv-proxmox-01"
)

# Cloud-Init: ユーザと SSH 公開鍵（改行区切りで列挙）
CI_USER=${CI_USER:-cloudinit}
AUTH_KEYS_URLS=${AUTH_KEYS_URLS:-"https://github.com/craftzdev.keys"}
REPOSITORY_RAW_SOURCE_URL="https://raw.githubusercontent.com/craftzdev/homelab/${TARGET_BRANCH}"

# SSH 接続先の識別（host か ip）
SSH_CONNECT_FIELD=${SSH_CONNECT_FIELD:-host}
RETRY_DELAY=${RETRY_DELAY:-2}
# クラスタ関連の厳格チェックを要求する場合に true（単一ノードでは false 推奨）
REQUIRE_CLUSTER=${REQUIRE_CLUSTER:-false}

### ======== Pre-checks（簡潔化） ========
CLOUD_IMG_BASE="https://cloud-images.ubuntu.com/noble/current"

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
if ! qm status "$TEMPLATE_VMID" >/dev/null 2>&1; then
  echo "[INFO] Downloading $UBUNTU_IMG"
  wget -q "${CLOUD_IMG_BASE}/${UBUNTU_IMG}"
  
  # 画像改変は行わず、cloud-init packages で qemu-guest-agent を導入します。

  echo "[INFO] Creating template VM $TEMPLATE_VMID"
  qm create "$TEMPLATE_VMID" --cores 2 --memory 4096 --name k8s-template \
    --net0 virtio,bridge=${VLAN_BRIDGE} \
    --agent enabled=1                        # QGA on

  echo "[INFO] Importing disk to storage"
  qm importdisk "$TEMPLATE_VMID" "$UBUNTU_IMG" "$TEMPLATE_BOOT_IMAGE_TARGET_VOLUME"
  qm set "$TEMPLATE_VMID" --scsihw virtio-scsi-pci --scsi0 "$TEMPLATE_BOOT_IMAGE_TARGET_VOLUME:vm-$TEMPLATE_VMID-disk-0"
  qm set "$TEMPLATE_VMID" --ide2 "$CLOUDINIT_IMAGE_TARGET_VOLUME":cloudinit
  qm set "$TEMPLATE_VMID" --boot order=scsi0 --serial0 socket --vga serial0

  echo "[INFO] Converting to template"
  qm template "$TEMPLATE_VMID"
  rm -f "$UBUNTU_IMG" SHA256SUMS
else
  echo "[INFO] Template $TEMPLATE_VMID exists. Skipping."
fi

### ======== Per-VM provisioning ========

CREATED=()
for row in "${VM_LIST[@]}"; do
  IFS=' ' read -r vmid vmname cpu mem ip targetip targethost <<< "$row"
  ssh_target=$([ "$SSH_CONNECT_FIELD" = "host" ] && echo "$targethost" || echo "$targetip")
  echo "[INFO] VMID=$vmid NAME=$vmname -> node=$ssh_target"

  # 既存ならスキップ（冪等）
  if ssh_exec "$ssh_target" "qm status $vmid" >/dev/null 2>&1; then
    echo "[WARN] VM $vmid exists on $ssh_target. Skipping clone/config."
    continue
  fi

  # Clone to target node
  echo "[INFO] Cloning from template"
  qm clone "$TEMPLATE_VMID" "$vmid" --name "$vmname" --full true --target "$targethost"

  echo "[INFO] CPU/Memory and disk"
  ssh_exec "$ssh_target" "qm set $vmid --cores $cpu --memory $mem"
  
  # ディスク移動は省略（--full true クローンで簡潔化）
  
  ssh_exec "$ssh_target" "qm resize $vmid scsi0 ${DISK_SIZE}"

  echo "[INFO] Networking (bridge ${VLAN_BRIDGE}, tag ${VLAN_ID})"
  ssh_exec "$ssh_target" "qm set $vmid --net0 virtio,bridge=${VLAN_BRIDGE},tag=${VLAN_ID}"

  echo "[INFO] Cloud-Init configuration will be handled by YAML files"

  # Create snippet directory
  ssh_exec "$ssh_target" "mkdir -p ${SNIPPET_TARGET_PATH}"

  # Create snippet for cloud-init (user-config)
  # START irregular indent because heredoc
# ----- #
  USER_SNIPPET_CONTENT=$(cat <<EOF
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
$(printf '  - su - cloudinit -c "curl -sS %s >> ~/.ssh/authorized_keys"\n' $(echo ${AUTH_KEYS_URLS}))
  - su - cloudinit -c "chmod 600 ~/.ssh/authorized_keys"
  # run install scripts
  - su - cloudinit -c "curl -s ${REPOSITORY_RAW_SOURCE_URL}/scripts/k8s-node-setup.sh > ~/k8s-node-setup.sh"
  - su - cloudinit -c "sudo bash ~/k8s-node-setup.sh ${vmname} ${TARGET_BRANCH}"
  # change default shell to bash
  - chsh -s \$(which bash) cloudinit
EOF
)
  ssh_exec "$ssh_target" "cat > ${SNIPPET_TARGET_PATH}/${vmname}-user.yaml <<'EOF'
${USER_SNIPPET_CONTENT}
EOF
"
# ----- #
  # END irregular indent because heredoc

  # Create snippet for cloud-init (network-config)
  # START irregular indent because heredoc
# ----- #
  NETWORK_SNIPPET_CONTENT=$(cat <<EOF
version: 1
config:
  - type: physical
    name: ens18
    subnets:
    - type: static
      address: '${ip}'
      netmask: '255.255.255.0'
      gateway: '${NODE_GATEWAY}'
  - type: nameserver
    address:
$(printf '    - %s\n' $(echo ${NAMESERVERS}))
    search:
    - '${SEARCHDOMAIN}'
EOF
)
  ssh_exec "$ssh_target" "cat > ${SNIPPET_TARGET_PATH}/${vmname}-network.yaml <<'EOF'
${NETWORK_SNIPPET_CONTENT}
EOF
"
# ----- #
  # END irregular indent because heredoc

  # Set snippet to vm
  ssh_exec "$ssh_target" "qm set $vmid --cicustom user=${SNIPPET_TARGET_VOLUME}:snippets/${vmname}-user.yaml,network=${SNIPPET_TARGET_VOLUME}:snippets/${vmname}-network.yaml"

  # 重要: Cloud-Init ISO を再生成
  ssh_exec "$ssh_target" "qm cloudinit update $vmid"

  echo "[INFO] Start VM"
  ssh_exec "$ssh_target" "qm start $vmid"

  CREATED+=("$vmid $vmname $ip on $targethost")
done

### ======== Post-creation ========
echo "[INFO] Completed. Log: $LOG_FILE"

# Summary
if [[ ${#CREATED[@]} -gt 0 ]]; then
  echo "[INFO] Created/Started VMs:"
  printf '  - %s\n' "${CREATED[@]}"
fi

# 終了
