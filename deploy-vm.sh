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

### ======== Pre-checks ========
need_cmd(){ command -v "$1" >/dev/null 2>&1 || { echo "[ERROR] '$1' not found"; exit 1; }; }
need_cmd qm; need_cmd wget; need_cmd ssh; need_cmd curl; need_cmd tee; need_cmd pvesm; need_cmd sha256sum; need_cmd flock
need_cmd ssh-keyscan; need_cmd ssh-keygen
if [[ "$REQUIRE_CLUSTER" == "true" ]]; then need_cmd pvecm; fi

# 単一実行ロック
LOCK_FILE="/tmp/deploy-vm.lock"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "[ERROR] Another deploy-vm.sh is running (lock: $LOCK_FILE)"; exit 1; }

# Proxmox クラスタ Quorum 確認（任意）
if [[ "$REQUIRE_CLUSTER" == "true" ]]; then
  if ! pvecm status | grep -q "Quorate: *Yes"; then
    echo "[ERROR] Proxmox cluster is not quorate. Check pvecm status."
    exit 1
  fi
else
  echo "[INFO] Cluster quorum check skipped (REQUIRE_CLUSTER=false)"
fi

# 外部到達性チェック（Ubuntu cloud image, node-setup script）
CLOUD_IMG_BASE="https://cloud-images.ubuntu.com/noble/current"
CLOUD_IMG_URL="${CLOUD_IMG_BASE}/${UBUNTU_IMG}"
RAW_SCRIPT_URL="${REPOSITORY_RAW_SOURCE_URL}/scripts/k8s-node-setup.sh"
curl -fsSI "$CLOUD_IMG_URL" >/dev/null || { echo "[ERROR] Cannot reach $CLOUD_IMG_URL"; exit 1; }
curl -fsSI "$RAW_SCRIPT_URL" >/dev/null || { echo "[ERROR] Cannot reach $RAW_SCRIPT_URL"; exit 1; }

# 共有ストレージ優先設定（Ceph RBD が存在すれば VM ディスクは共有ストレージを使用）
# 優先順位: 明示指定(CEPH_STORAGE) > 指定プール名一致(CEPH_POOL_NAME)
# 環境変数で無効化したい場合は PREFER_SHARED_STORAGE=false を指定してください。
if [[ "${PREFER_SHARED_STORAGE:-true}" == "true" ]]; then
  # 明示指定があればそれを優先（見つからなければエラー）
  if [[ -n "${CEPH_STORAGE:-}" ]]; then
    if pvesm status | awk '{print $1}' | grep -qx "$CEPH_STORAGE"; then
      CLOUDINIT_IMAGE_TARGET_VOLUME=${CLOUDINIT_IMAGE_TARGET_VOLUME:-$CEPH_STORAGE}
      TEMPLATE_BOOT_IMAGE_TARGET_VOLUME=${TEMPLATE_BOOT_IMAGE_TARGET_VOLUME:-$CEPH_STORAGE}
      BOOT_IMAGE_TARGET_VOLUME=${BOOT_IMAGE_TARGET_VOLUME:-$CEPH_STORAGE}
      echo "[INFO] Using explicitly specified Ceph storage '$CEPH_STORAGE' for VM disks"
    else
      echo "[ERROR] Specified CEPH_STORAGE '$CEPH_STORAGE' not found in pvesm status"; exit 1
    fi
  fi

  # まだ未決定の場合はプール名一致で自動選択（見つからなければエラー）
  if [[ "${CLOUDINIT_IMAGE_TARGET_VOLUME}" == "local-lvm" || "${TEMPLATE_BOOT_IMAGE_TARGET_VOLUME}" == "local-lvm" || "${BOOT_IMAGE_TARGET_VOLUME}" == "local-lvm" ]]; then
    # rbd ストレージ一覧を取得
    mapfile -t RBD_STORES < <(pvesm status | awk '$2=="rbd"{print $1}')
    if [[ ${#RBD_STORES[@]} -eq 0 ]]; then
      echo "[ERROR] No Ceph RBD storage found in pvesm status (required for Kubernetes cluster VMs)"; exit 1
    fi
    selected=""
    # 1) storage ID が CEPH_POOL_NAME と一致するものを優先的に選択
    for s in "${RBD_STORES[@]}"; do
      if [[ "$s" == "$CEPH_POOL_NAME" ]]; then
        selected="$s"; echo "[INFO] Found Ceph storage id '$s' matching CEPH_POOL_NAME"; break
      fi
    done
    # 2) 見つからない場合は pool 名一致で選択
    if [[ -z "$selected" ]]; then
      for s in "${RBD_STORES[@]}"; do
        pool=$({ pvesm config "$s" 2>/dev/null || true; } | awk -F": " '/^pool:/{print $2}')
        if [[ "$pool" == "$CEPH_POOL_NAME" ]]; then
          selected="$s"; echo "[INFO] Found Ceph storage '$s' with required pool '$pool'"; break
        fi
      done
    fi
    if [[ -z "$selected" ]]; then
      echo "[ERROR] No rbd storage with required pool or id '$CEPH_POOL_NAME' found"; exit 1
    fi
    CLOUDINIT_IMAGE_TARGET_VOLUME="$selected"
    TEMPLATE_BOOT_IMAGE_TARGET_VOLUME="$selected"
    BOOT_IMAGE_TARGET_VOLUME="$selected"
    echo "[INFO] Using shared Ceph storage '$selected' for VM disks (cloud-init/template/boot)"
  fi
fi

# Storage 存在チェック
for s in "$CLOUDINIT_IMAGE_TARGET_VOLUME" "$TEMPLATE_BOOT_IMAGE_TARGET_VOLUME" "$BOOT_IMAGE_TARGET_VOLUME" "$SNIPPET_TARGET_VOLUME"; do
  pvesm status | awk '{print $1}' | grep -qx "$s" || { echo "[ERROR] storage '$s' not found in pvesm status"; exit 1; }
done

# snippets content 有効性チェック（厳格化）
if ! pvesm list "$SNIPPET_TARGET_VOLUME" --content snippets >/dev/null 2>&1; then
  echo "[ERROR] storage '$SNIPPET_TARGET_VOLUME' does not have 'snippets' content enabled."
  echo "        Please enable 'content: snippets' on the storage before running."
  exit 1
fi
[ -d "$SNIPPET_TARGET_PATH" ] || mkdir -p "$SNIPPET_TARGET_PATH"

retry(){ local t=${1:-3}; shift; local i=0; while ((i<t)); do "$@" && return 0 || { i=$((i+1)); sleep "$RETRY_DELAY"; }; done; return 1; }
ssh_exec(){ local host=$1; shift; retry 3 ssh -o BatchMode=yes -o ConnectTimeout=10 -n "$host" "$@"; }

# VM_LIST 事前検証（重複/形式チェック）
is_ipv4(){ [[ $1 =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; }
mapfile -t VMIDS < <(printf "%s\n" "${VM_LIST[@]}" | awk '{print $1}')
mapfile -t VMNAMES < <(printf "%s\n" "${VM_LIST[@]}" | awk '{print $2}')
mapfile -t VMIPS < <(printf "%s\n" "${VM_LIST[@]}" | awk '{print $5}')
if (( $(printf "%s\n" "${VMIDS[@]}" | sort -u | wc -l) != ${#VMIDS[@]} )); then echo "[ERROR] duplicate VMID"; exit 1; fi
if (( $(printf "%s\n" "${VMNAMES[@]}" | sort -u | wc -l) != ${#VMNAMES[@]} )); then echo "[ERROR] duplicate VM name"; exit 1; fi
if (( $(printf "%s\n" "${VMIPS[@]}" | sort -u | wc -l) != ${#VMIPS[@]} )); then echo "[ERROR] duplicate IP"; exit 1; fi
for r in "${VM_LIST[@]}"; do
  IFS=' ' read -r _ name cpu mem ip _ host <<< "$r"
  [[ "$cpu" =~ ^[0-9]+$ && "$mem" =~ ^[0-9]+$ ]] || { echo "[ERROR] non-numeric cpu/mem in: $r"; exit 1; }
  is_ipv4 "$ip" || { echo "[ERROR] invalid IPv4: $r"; exit 1; }
  [[ -n "$host" ]] || { echo "[ERROR] empty targethost in: $r"; exit 1; }
done

# targethost がクラスタに存在するか（任意）
if [[ "$REQUIRE_CLUSTER" == "true" ]]; then
  mapfile -t CLUSTER_NODES < <(pvecm nodes | awk 'NR>1 {print $NF}')
  for r in "${VM_LIST[@]}"; do
    read -r _ _ _ _ _ _ host <<< "$r"
    printf "%s\n" "${CLUSTER_NODES[@]}" | grep -qx "$host" || { echo "[ERROR] targethost '$host' not found in cluster nodes"; exit 1; }
  done
else
  echo "[INFO] Cluster node membership check skipped (REQUIRE_CLUSTER=false)"
fi

# SSH 接続先ユニーク化（移行先ノードも含める）
mapfile -t SSH_TARGETS < <(printf "%s\n" "${VM_LIST[@]}" | awk -v m="$SSH_CONNECT_FIELD" '{print (m=="host"?$7:$6)}' | sort -u)

# VM移行先ノードも追加
declare -A VM_MIGRATIONS_TEMP=(
  ["k8s-cp-2"]="sv-proxmox-02"
  ["k8s-wk-2"]="sv-proxmox-02"
  ["k8s-cp-3"]="sv-proxmox-03"
  ["k8s-wk-3"]="sv-proxmox-03"
)
for target_node in "${VM_MIGRATIONS_TEMP[@]}"; do
  if ! printf "%s\n" "${SSH_TARGETS[@]}" | grep -qx "$target_node"; then
    SSH_TARGETS+=("$target_node")
  fi
done
# SSH known_hosts への事前登録（初回接続時の対話回避）
ensure_known_host(){
  local host="$1"
  local kh="${HOME}/.ssh/known_hosts"
  mkdir -p "${HOME}/.ssh" && chmod 700 "${HOME}/.ssh"
  # 既に登録済みでなければ追加
  if ! ssh-keygen -F "$host" >/dev/null 2>&1; then
    echo "[INFO] Adding SSH host key for $host to known_hosts"
    ssh-keyscan -T 5 -H "$host" >> "$kh" 2>/dev/null || echo "[WARN] ssh-keyscan failed for $host"
  fi
}
for st in "${SSH_TARGETS[@]}"; do
  echo "[INFO] Checking SSH connectivity to $st"
  ensure_known_host "$st"
  if ! ssh -o BatchMode=yes -o ConnectTimeout=5 -n "$st" true; then
    echo "[WARN] SSH to $st failed; attempting host key refresh"
    ssh-keygen -R "$st" >/dev/null 2>&1 || true
    ensure_known_host "$st"
    ssh -o BatchMode=yes -o ConnectTimeout=5 -n "$st" true || { echo "[ERROR] SSH to $st failed"; echo "        If this is due to 'Host key verification failed', run: ssh-keygen -R $st && ssh-keyscan -H $st >> ~/.ssh/known_hosts"; exit 1; }
  fi
done

### ======== Template (cloud image) ========
if ! qm status "$TEMPLATE_VMID" >/dev/null 2>&1; then
  echo "[INFO] Downloading $UBUNTU_IMG"
  wget -q "${CLOUD_IMG_BASE}/${UBUNTU_IMG}"

  # 画像に qemu-guest-agent を組み込み（可能なら virt-customize を使用）
  echo "[INFO] Preparing cloud image with qemu-guest-agent"
  if ! command -v virt-customize >/dev/null 2>&1; then
    echo "[INFO] Installing libguestfs-tools (virt-customize)"
    apt-get update -y && apt-get install -y libguestfs-tools || true
  fi
  if command -v virt-customize >/dev/null 2>&1; then
    echo "[INFO] Injecting qemu-guest-agent into ${UBUNTU_IMG}"
    virt-customize -a "$UBUNTU_IMG" --install qemu-guest-agent
  else
    echo "[WARN] virt-customize not available; qemu-guest-agent will be installed via cloud-init packages on first boot"
  fi

  echo "[INFO] Creating template VM $TEMPLATE_VMID"
  qm create "$TEMPLATE_VMID" --cores 2 --memory 4096 --name k8s-template \
    --net0 virtio,bridge=${VLAN_BRIDGE} \
    --agent enabled=1                        # QGA on

  echo "[INFO] Importing disk to shared storage"
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
# 事前に公開鍵を一括取得
KEYS=""
for url in $AUTH_KEYS_URLS; do KEYS+=$(curl -fsSL "$url"; echo); done
[ -n "$KEYS" ] || { echo "[ERROR] No SSH public keys fetched"; exit 1; }

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
  echo "[INFO] Cloning from template (shared storage allows cross-node cloning)"
  qm clone "$TEMPLATE_VMID" "$vmid" --name "$vmname" --full false --target "$targethost"

  echo "[INFO] CPU/Memory and disk"
  ssh_exec "$ssh_target" "qm set $vmid --cores $cpu --memory $mem"
  
  # Check if disk is already on target storage before moving
  current_storage=$(ssh_exec "$ssh_target" "qm config $vmid | grep '^scsi0:' | cut -d: -f2 | cut -d, -f1 | xargs")
  echo "[DEBUG] Current storage: '$current_storage', Target storage: '$BOOT_IMAGE_TARGET_VOLUME'"
  if [[ "$current_storage" != "$BOOT_IMAGE_TARGET_VOLUME" ]]; then
    echo "[INFO] Moving disk from $current_storage to $BOOT_IMAGE_TARGET_VOLUME"
    ssh_exec "$ssh_target" "qm move-disk $vmid scsi0 $BOOT_IMAGE_TARGET_VOLUME --delete true"
  else
    echo "[INFO] Disk already on target storage $BOOT_IMAGE_TARGET_VOLUME, skipping move"
  fi
  
  ssh_exec "$ssh_target" "qm resize $vmid scsi0 ${DISK_SIZE}"

  echo "[INFO] Networking (bridge ${VLAN_BRIDGE}, tag ${VLAN_ID})"
  ssh_exec "$ssh_target" "qm set $vmid --net0 virtio,bridge=${VLAN_BRIDGE},tag=${VLAN_ID}"

  echo "[INFO] Cloud-Init: ipconfig0 / DNS / user & ssh keys"
  ssh_exec "$ssh_target" "qm set $vmid --ipconfig0 ip=${ip}/${NODE_CIDR_SUFFIX},gw=${NODE_GATEWAY}"
  ssh_exec "$ssh_target" "qm set $vmid --nameserver \"$(echo $NAMESERVERS | tr ' ' ',')\" --searchdomain ${SEARCHDOMAIN}"
  ssh_exec "$ssh_target" "qm set $vmid --ciuser ${CI_USER}"
  # 注意: CI_USER は Cloud-Init が作成するユーザ名。下の user-data でも cloudinit ユーザを作成しているため、ここで指定した CI_USER が鍵認証対象になります。README の例で CI_USER=ubuntu とした場合は user-data の users セクションも合わせる必要があります。

  # user-data スニペット（runcmd だけを載せる。鍵は --sshkeys で投入）
  REMOTE_SNIPPET="${SNIPPET_TARGET_PATH}/${vmname}-user.yaml"
  ssh_exec "$ssh_target" "mkdir -p ${SNIPPET_TARGET_PATH}"
  ssh_exec "$ssh_target" "REPOSITORY_RAW_SOURCE_URL='${REPOSITORY_RAW_SOURCE_URL}' TARGET_BRANCH='${TARGET_BRANCH}' vmname='${vmname}' NAMESERVERS='${NAMESERVERS}' NODE_CIDR_SUFFIX='${NODE_CIDR_SUFFIX}' NODE_GATEWAY='${NODE_GATEWAY}' SEARCHDOMAIN='${SEARCHDOMAIN}' cat > ${REMOTE_SNIPPET} << EOF
#cloud-config
hostname: ${vmname}
timezone: Asia/Tokyo
manage_etc_hosts: true
users:
  - default
  - name: cloudinit
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: false
    shell: /bin/bash
    # mkpasswd --method=SHA-512 --rounds=4096
    # password is zaq12wsx
    passwd: \$6\$rounds=4096\$Xlyxul70asLm\$9tKm.0po4ZE7vgqc.grptZzUU9906z/.vjwcqz/WYVtTwc5i2DWfjVpXb8HBtoVfvSY61rvrs/iwHxREKl3f20
ssh_pwauth: true
ssh_authorized_keys: []
# Network configuration with proper nameserver format
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: false
      addresses:
        - ${ip}/${NODE_CIDR_SUFFIX}
      gateway4: ${NODE_GATEWAY}
      nameservers:
        addresses:$(printf '\n          - %s' $(echo ${NAMESERVERS}))
        search:
          - ${SEARCHDOMAIN}
packages:
  - qemu-guest-agent
package_upgrade: true
runcmd:
  # set ssh_authorized_keys
  - su - cloudinit -c "mkdir -p ~/.ssh && chmod 700 ~/.ssh"
  - su - cloudinit -c "curl -sS https://github.com/craftz.keys >> ~/.ssh/authorized_keys"
  - su - cloudinit -c "chmod 600 ~/.ssh/authorized_keys"
  # run install scripts
  - su - cloudinit -c "curl -s ${REPOSITORY_RAW_SOURCE_URL}/scripts/k8s-node-setup.sh > ~/k8s-node-setup.sh"
  - su - cloudinit -c "sudo bash ~/k8s-node-setup.sh ${vmname} ${TARGET_BRANCH}"
EOF"
  # SSH 公開鍵を Cloud-Init の UI パラメータで投入
  # （スニペットに埋めても良いが、UI/CLI から差し替えやすい）
  ssh_exec "$ssh_target" "cat > /tmp/${vmname}.pub <<'KEYS'
${KEYS}
KEYS"
  ssh_exec "$ssh_target" "qm set $vmid --sshkeys /tmp/${vmname}.pub && rm -f /tmp/${vmname}.pub"

  # スニペットを user= で参照（network は ipconfig0 で行う）
  ssh_exec "$ssh_target" "qm set $vmid --cicustom user=${SNIPPET_TARGET_VOLUME}:snippets/${vmname}-user.yaml"

  # 重要: Cloud-Init ISO を再生成
  ssh_exec "$ssh_target" "qm cloudinit update $vmid"

  echo "[INFO] Start VM"
  ssh_exec "$ssh_target" "qm start $vmid"

  CREATED+=("$vmid $vmname $ip on $targethost")
done

### ======== Post-creation VM migration ========
echo "[INFO] Starting post-creation VM migrations"

# VM migration mapping: vmname -> target_node
declare -A VM_MIGRATIONS=(
  ["k8s-cp-2"]="sv-proxmox-02"
  ["k8s-wk-2"]="sv-proxmox-02"
  ["k8s-cp-3"]="sv-proxmox-03"
  ["k8s-wk-3"]="sv-proxmox-03"
)

for row in "${VM_LIST[@]}"; do
  IFS=' ' read -r vmid vmname cpu mem ip targetip targethost <<< "$row"
  
  # Check if this VM needs to be migrated
  if [[ -n "${VM_MIGRATIONS[$vmname]:-}" ]]; then
    target_node="${VM_MIGRATIONS[$vmname]}"
    current_node="$targethost"
    
    # Get current hostname to avoid migrating to local node
    local_hostname=$(hostname)
    
    # Check if VM already exists on target node (idempotency)
    if ssh_exec "$target_node" "qm status $vmid" >/dev/null 2>&1; then
      echo "[INFO] VM $vmid ($vmname) already exists on target node $target_node, skipping migration"
      continue
    fi
    
    if [[ "$current_node" != "$target_node" ]]; then
      # Check if target node is the local node (avoid "target is local node" error)
      if [[ "$target_node" == "$local_hostname" ]]; then
        echo "[WARN] Cannot migrate VM $vmid ($vmname) to local node $target_node, skipping migration"
        continue
      fi
      
      # Check if VM exists on current node before attempting migration
      if ! ssh_exec "$current_node" "qm status $vmid" >/dev/null 2>&1; then
        echo "[WARN] VM $vmid ($vmname) not found on current node $current_node, skipping migration"
        continue
      fi
      
      echo "[INFO] Migrating VM $vmid ($vmname) from $current_node to $target_node"
      
      # Stop VM before migration
      ssh_exec "$current_node" "qm stop $vmid"
      echo "[INFO] Stopped VM $vmid on $current_node"
      
      # Wait for VM to stop completely
      sleep 5
      
      # Migrate VM to target node (execute from current node where VM exists)
      if ssh_exec "$current_node" "qm migrate $vmid $target_node --online false"; then
        echo "[INFO] Successfully migrated VM $vmid ($vmname) to $target_node"
        
        # Start VM on new node
        ssh_exec "$target_node" "qm start $vmid"
        echo "[INFO] Started VM $vmid on $target_node"
      else
        echo "[ERROR] Failed to migrate VM $vmid ($vmname) to $target_node"
        # Try to start VM on original node if migration failed
        ssh_exec "$current_node" "qm start $vmid"
        echo "[INFO] Restarted VM $vmid on original node $current_node"
      fi
      
    else
      echo "[INFO] VM $vmid ($vmname) already on target node $target_node, skipping migration"
    fi
  fi
done

echo "[INFO] Completed VM migrations"
echo "[INFO] Completed. Log: $LOG_FILE"

# Summary
if [[ ${#CREATED[@]} -gt 0 ]]; then
  echo "[INFO] Created/Started VMs:"
  printf '  - %s\n' "${CREATED[@]}"
fi

# ---- 後片付け: ざっくり sanity check（冪等）----
is_ipv4(){ [[ $1 =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; }
mapfile -t VMIDS < <(printf "%s\n" "${VM_LIST[@]}" | awk '{print $1}')
mapfile -t VMNAMES < <(printf "%s\n" "${VM_LIST[@]}" | awk '{print $2}')
mapfile -t VMIPS < <(printf "%s\n" "${VM_LIST[@]}" | awk '{print $5}')
if (( $(printf "%s\n" "${VMIDS[@]}" | sort -u | wc -l) != ${#VMIDS[@]} )); then echo "[ERROR] duplicate VMID"; exit 1; fi
if (( $(printf "%s\n" "${VMNAMES[@]}" | sort -u | wc -l) != ${#VMNAMES[@]} )); then echo "[ERROR] duplicate VM name"; exit 1; fi
if (( $(printf "%s\n" "${VMIPS[@]}" | sort -u | wc -l) != ${#VMIPS[@]} )); then echo "[ERROR] duplicate IP"; exit 1; fi
for r in "${VM_LIST[@]}"; do
  IFS=' ' read -r _ _ cpu mem ip _ _ <<< "$r"
  [[ "$cpu" =~ ^[0-9]+$ && "$mem" =~ ^[0-9]+$ ]] || { echo "[ERROR] non-numeric cpu/mem in: $r"; exit 1; }
  is_ipv4 "$ip" || { echo "[ERROR] invalid IPv4: $r"; exit 1; }
done
