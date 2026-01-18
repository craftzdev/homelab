#!/usr/bin/env bash

#region set variables

TARGET_BRANCH=$1
TEMPLATE_VMID=9050
CEPH_POOL_NAME=cephrdb_k8s
CLOUDINIT_IMAGE_TARGET_VOLUME=$CEPH_POOL_NAME
TEMPLATE_BOOT_IMAGE_TARGET_VOLUME=$CEPH_POOL_NAME
BOOT_IMAGE_TARGET_VOLUME=$CEPH_POOL_NAME
SNIPPET_TARGET_VOLUME=cephfs01
SNIPPET_TARGET_PATH=/mnt/pve/${SNIPPET_TARGET_VOLUME}/snippets
REPOSITORY_RAW_SOURCE_URL="https://raw.githubusercontent.com/craftzdev/homelab/${TARGET_BRANCH}"
VM_LIST=(
    #vmid #vmname             #cpu #mem  #targetip      #targethost
    "1001 k8s-cp-1 4    8192  172.16.10.11 sv-proxmox-01"
    "1002 k8s-cp-2 4    8192  172.16.10.12 sv-proxmox-02"
    "1003 k8s-cp-3 4    8192  172.16.10.13 sv-proxmox-03"
    "1101 k8s-wk-1 6    24576 172.16.10.11 sv-proxmox-01"
    "1102 k8s-wk-2 6    24576 172.16.10.12 sv-proxmox-02"
    "1103 k8s-wk-3 6    24576 172.16.10.13 sv-proxmox-03"
)

#endregion

# ---

#region create-template

# Check if template already exists
if qm status $TEMPLATE_VMID &>/dev/null; then
    echo "[INFO] Template VM $TEMPLATE_VMID already exists. Skipping template creation."
else
    echo "[INFO] Creating template VM $TEMPLATE_VMID..."

    # download the image(ubuntu 24.04 LTS)
    wget https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img

    # install qemu-guest-agent to image using libguestfs-tools
    apt-get update && apt-get install libguestfs-tools -y
    virt-customize -a noble-server-cloudimg-amd64.img --install liburing2 --install qemu-guest-agent

    # create a new VM and attach Network Adaptor
    qm create $TEMPLATE_VMID --cores 2 --memory 4096 --net0 virtio,bridge=vmbr1,tag=40,firewall=1 --name k8s-template
    # enable qemu-guest-agent (set separately from create)
    qm set $TEMPLATE_VMID --agent enabled=1,fstrim_cloned_disks=1

    # set UEFI (OVMF) BIOS and machine type q35
    qm set $TEMPLATE_VMID --bios ovmf --machine q35

    # import the downloaded disk to $TEMPLATE_BOOT_IMAGE_TARGET_VOLUME storage
    qm importdisk $TEMPLATE_VMID noble-server-cloudimg-amd64.img $TEMPLATE_BOOT_IMAGE_TARGET_VOLUME

    # finally attach the new disk to the VM as scsi drive
    qm set $TEMPLATE_VMID --scsihw virtio-scsi-pci --scsi0 $TEMPLATE_BOOT_IMAGE_TARGET_VOLUME:vm-$TEMPLATE_VMID-disk-0

    # add EFI disk on shared Ceph storage AFTER attaching main disk to avoid name collision
    qm set $TEMPLATE_VMID --efidisk0 $BOOT_IMAGE_TARGET_VOLUME:0

    # add Cloud-Init CD-ROM drive
    qm set $TEMPLATE_VMID --ide2 $CLOUDINIT_IMAGE_TARGET_VOLUME:cloudinit

    # set the bootdisk parameter to scsi0
    qm set $TEMPLATE_VMID --boot c --bootdisk scsi0

    # set serial console
    qm set $TEMPLATE_VMID --serial0 socket --vga serial0

    # migrate to template
    qm template $TEMPLATE_VMID

    # cleanup
    rm noble-server-cloudimg-amd64.img
fi

#endregion

# ---

# region create vm from template

for array in "${VM_LIST[@]}"
do
    echo "${array}" | while read -r vmid vmname cpu mem targetip targethost
    do
        echo "=== Processing VM: ${vmname} (${vmid}) on ${targethost} ==="

        # Check if VM already exists and remove it
        if qm status "${vmid}" &>/dev/null; then
            echo "[INFO] VM ${vmid} already exists. Removing..."
            qm stop "${vmid}" 2>/dev/null || true
            qm destroy "${vmid}" --purge 2>/dev/null || true
        fi

        # Clean up orphan Ceph images if any
        for suffix in cloudinit disk-0 disk-1; do
            if rbd ls "${CEPH_POOL_NAME}" 2>/dev/null | grep -q "vm-${vmid}-${suffix}"; then
                echo "[INFO] Removing orphan Ceph image: vm-${vmid}-${suffix}"
                rbd rm "${CEPH_POOL_NAME}/vm-${vmid}-${suffix}" --no-progress 2>/dev/null || true
            fi
        done

        # clone from template
        # in clone phase, can't create vm-disk to local volume
        qm clone "${TEMPLATE_VMID}" "${vmid}" --name "${vmname}" --full true --target "${targethost}"
        
        # set compute resources
        ssh -n "${targetip}" qm set "${vmid}" --cores "${cpu}" --memory "${mem}"
        ssh -n "${targetip}" qm set "${vmid}" --net0 virtio,bridge=vmbr1,tag=40,firewall=1

        # resize disk (Resize after cloning, because it takes time to clone a large disk)
        ssh -n "${targetip}" qm resize "${vmid}" scsi0 100G

        # create snippet for cloud-init(user-config)
        # START irregular indent because heredoc
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
    lock_passwd: true
    # SSH key-only authentication - no password
ssh_pwauth: false
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
        
        # update cloud init iso
        ssh -n "${targetip}" qm cloudinit update "${vmid}"

        # start vm
        ssh -n "${targetip}" qm start "${vmid}"

    done
done

# endregion
