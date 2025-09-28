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
SNIPPET_TARGET_VOLUME=${SNIPPET_TARGET_VOLUME:-local}              # content: snippets を有効に
SNIPPET_TARGET_PATH=${SNIPPET_TARGET_PATH:-/var/lib/vz/snippets}
UBUNTU_IMG="noble-server-cloudimg-amd64.img"                       # Ubuntu 24.04
DISK_SIZE=${DISK_SIZE:-30G}

# Network (Proxmox Cloud-Init 推奨パラメータ使用)
VLAN_ID=${VLAN_ID:-40}
VLAN_BRIDGE=${VLAN_BRIDGE:-vmbr1}
NODE_GATEWAY=${NODE_GATEWAY:-172.16.40.1}
NODE_CIDR_SUFFIX=${NODE_CIDR_SUFFIX:-24}                           # xxx.xxx.xxx.xxx/24
NAMESERVERS=${NAMESERVERS:-"172.16.40.1 1.1.1.1"}
SEARCHDOMAIN=${SEARCHDOMAIN:-home.arpa}

# VM inventory: vmid name vCPU mem(MiB) ip targetip targethost
VM_LIST=(
  "1001 k8s-cp-1 4 8192 172.16.40.11 - sv-proxmox-02"
  "1002 k8s-cp-2 4 8192 172.16.40.12 - sv-proxmox-02"
  "1003 k8s-cp-3 4 8192 172.16.40.13 - sv-proxmox-02"
  "1101 k8s-wk-1 4 8192 172.16.40.21 - sv-proxmox-02"
  "1102 k8s-wk-2 4 8192 172.16.40.22 - sv-proxmox-02"
  "1103 k8s-wk-3 4 8192 172.16.40.23 - sv-proxmox-02"
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
  read -r _ name cpu mem ip _ host <<< "$r"
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

# SSH 接続先ユニーク化
mapfile -t SSH_TARGETS < <(printf "%s\n" "${VM_LIST[@]}" | awk -v m="$SSH_CONNECT_FIELD" '{print (m=="host"?$7:$6)}' | sort -u)
for st in "${SSH_TARGETS[@]}"; do
  echo "[INFO] Checking SSH connectivity to $st"
  ssh -o BatchMode=yes -o ConnectTimeout=5 -n "$st" true || { echo "[ERROR] SSH to $st failed"; exit 1; }
done

### ======== Template (cloud image) ========
if ! qm status "$TEMPLATE_VMID" >/dev/null 2>&1; then
  echo "[INFO] Downloading $UBUNTU_IMG"
  wget -q "${CLOUD_IMG_BASE}/${UBUNTU_IMG}"
  # 署名ファイルからSHA256を検証
  wget -q "${CLOUD_IMG_BASE}/SHA256SUMS"
  SUM_LINE=$(grep -E "[[:space:]]${UBUNTU_IMG}$" SHA256SUMS || true)
  if [[ -z "$SUM_LINE" ]]; then echo "[ERROR] SHA256SUM for ${UBUNTU_IMG} not found"; exit 1; fi
  echo "$SUM_LINE" | sha256sum -c - || { echo "[ERROR] checksum verification failed for ${UBUNTU_IMG}"; exit 1; }

  echo "[INFO] Creating template VM $TEMPLATE_VMID"
  qm create "$TEMPLATE_VMID" --cores 2 --memory 4096 --name unc-k8s-template \
    --net0 virtio,bridge=${VLAN_BRIDGE} \
    --agent enabled=1                        # QGA on

  echo "[INFO] Importing disk"
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
  read -r vmid vmname cpu mem ip targetip targethost <<< "$row"
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
  ssh_exec "$ssh_target" "qm move-disk $vmid scsi0 $BOOT_IMAGE_TARGET_VOLUME --delete true"
  ssh_exec "$ssh_target" "qm resize $vmid scsi0 ${DISK_SIZE}"

  echo "[INFO] Networking (bridge ${VLAN_BRIDGE}, tag ${VLAN_ID})"
  ssh_exec "$ssh_target" "qm set $vmid --net0 virtio,bridge=${VLAN_BRIDGE},tag=${VLAN_ID}"

  echo "[INFO] Cloud-Init: ipconfig0 / DNS / user & ssh keys"
  ssh_exec "$ssh_target" "qm set $vmid --ipconfig0 ip=${ip}/${NODE_CIDR_SUFFIX},gw=${NODE_GATEWAY}"
  ssh_exec "$ssh_target" "qm set $vmid --nameserver '$(echo $NAMESERVERS | tr ' ' ',')' --searchdomain ${SEARCHDOMAIN}"
  ssh_exec "$ssh_target" "qm set $vmid --ciuser ${CI_USER}"

  # user-data スニペット（runcmd だけを載せる。鍵は --sshkeys で投入）
  REMOTE_SNIPPET="${SNIPPET_TARGET_PATH}/${vmname}-user.yaml"
  ssh_exec "$ssh_target" "mkdir -p ${SNIPPET_TARGET_PATH}"
  ssh_exec "$ssh_target" "cat > ${REMOTE_SNIPPET} << 'EOF'
#cloud-config
hostname: ${vmname}
timezone: Asia/Tokyo
manage_etc_hosts: true
ssh_pwauth: false
package_upgrade: true
runcmd:
  - 'curl -fsSL ${REPOSITORY_RAW_SOURCE_URL}/scripts/k8s-node-setup.sh -o /root/k8s-node-setup.sh'
  - 'bash /root/k8s-node-setup.sh ${vmname} ${TARGET_BRANCH}'
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

echo "[INFO] Completed. Log: $LOG_FILE"

# Summary
if ((${#CREATED[@]:-0} > 0)); then
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
  read -r _ _ cpu mem ip _ _ <<< "$r"
  [[ "$cpu" =~ ^[0-9]+$ && "$mem" =~ ^[0-9]+$ ]] || { echo "[ERROR] non-numeric cpu/mem in: $r"; exit 1; }
  is_ipv4 "$ip" || { echo "[ERROR] invalid IPv4: $r"; exit 1; }
done
