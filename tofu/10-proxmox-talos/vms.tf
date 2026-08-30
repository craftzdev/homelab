# ===========================================================================
# Kubernetes ノード VM
#
# 各ノードは 2 枚の NIC を持つ（[ADR-0007](../../docs/adr/0007-dual-nic-topology.md)）:
#   net0 = VLAN40 : Kubernetes 全般
#   net1 = VLAN20 : Ceph public（デフォルトゲートウェイ無し）
#
# MAC アドレスは variables.tf で固定的に払い出している。Talos の machine config
# は MAC で NIC を選択するため、ここが安定していることが構成の前提になる。
# ===========================================================================
resource "proxmox_virtual_environment_vm" "node" {
  for_each = local.all_nodes

  name        = each.key
  description = "Talos Linux ${var.talos_version} / ${each.value.role} / managed by OpenTofu"
  tags        = ["talos", "kubernetes", each.value.role]

  node_name = each.value.pve_node
  vm_id     = each.value.vmid

  # Proxmox が VM を止めるときに ACPI シャットダウンを待つ。
  # Talos は正常にシャットダウンできるため、いきなり電源断にしない。
  stop_on_destroy = false

  # ---------------------------------------------------------------------------
  # CPU
  #
  # type = "host" は物理 CPU の機能をそのまま見せる設定。
  # eBPF（Cilium）や暗号化処理で CPU 命令セットを活かすために必要。
  # ライブマイグレーションは制約を受けるが、Talos ノードは
  # 停止・再作成が容易なため許容する。
  # ---------------------------------------------------------------------------
  cpu {
    cores = each.value.cores
    type  = "host"
    units = 1024
  }

  memory {
    dedicated = each.value.memory_mib
    # ballooning は無効（floating = 0）。
    # Kubernetes ノードでメモリを動的に回収されると、kubelet の
    # eviction 判断が実態とずれて予期しない Pod 退去を招く。
    floating = 0
  }

  # ---------------------------------------------------------------------------
  # ディスク
  #
  # Ceph RBD（cephrdb_k8s）上に置く。ノード障害時に別ノードで
  # 起動し直せるようにするため、ローカルストレージは使わない。
  # ---------------------------------------------------------------------------
  disk {
    datastore_id = var.vm_datastore_id
    interface    = "scsi0"
    size         = each.value.disk_gib
    file_format  = "raw"
    # SSD として見せることで、ゲスト側が discard を発行しやすくなる
    ssd = true
    # 未使用ブロックを Ceph へ返却する（thin provision を維持する）
    discard = "on"
    # I/O を専用スレッドで処理し、他の VM の影響を受けにくくする
    iothread = true
    cache    = "none"
  }

  scsi_hardware = "virtio-scsi-single"

  # ---------------------------------------------------------------------------
  # ブートメディア
  #
  # Talos の nocloud ISO。初回はディスクが空なのでここから起動し、
  # machine config の install 設定に従って自身をディスクへ書き込む。
  # 以降は boot_order の先頭（scsi0）から起動する。
  #
  # ISO を付けたままにするのは意図的である。ディスクが破損した際に
  # メンテナンスモードで起動でき、再インストールで復旧できる。
  # ---------------------------------------------------------------------------
  cdrom {
    file_id   = proxmox_download_file.talos_iso.id
    interface = "ide0"
  }

  boot_order = ["scsi0", "ide0"]

  # ---------------------------------------------------------------------------
  # cloud-init（初期 IP の付与のみ）
  #
  # ⚠️ ここには machine config（＝クラスタの秘密鍵）を置かない。
  #    理由は machine-config.tf の冒頭コメントを参照。
  #
  # Talos の nocloud プラットフォームは cloud-init の network-config を
  # 解釈する。これにより、DHCP の無い VLAN40 でもメンテナンスモードの
  # 時点でノードに到達できるようになる。
  #
  # ip_config はリスト順に net0, net1 へ対応する。
  # ---------------------------------------------------------------------------
  initialization {
    datastore_id = var.vm_datastore_id
    interface    = "ide2"

    # net0: VLAN40（デフォルトゲートウェイあり）
    ip_config {
      ipv4 {
        address = "${each.value.ip}/24"
        gateway = var.k8s_gateway
      }
    }

    # net1: VLAN20（Ceph public / ゲートウェイなし）
    ip_config {
      ipv4 {
        address = "${each.value.ceph_ip}/24"
      }
    }

    dns {
      servers = var.nameservers
    }
  }

  # ---------------------------------------------------------------------------
  # ネットワーク
  # ---------------------------------------------------------------------------
  network_device {
    bridge      = var.network_bridge
    vlan_id     = var.vlan_k8s
    mac_address = each.value.mac_k8s
    model       = "virtio"
    firewall    = false # フィルタは Talos の ingressFirewall で行う
  }

  network_device {
    bridge      = var.network_bridge
    vlan_id     = var.vlan_ceph_public
    mac_address = each.value.mac_ceph
    model       = "virtio"
    firewall    = false
  }

  operating_system {
    type = "l26"
  }

  # qemu-guest-agent（Image Factory の system extension として組み込み済み）。
  # Proxmox からの正常シャットダウンと IP 取得に使う。
  agent {
    enabled = true
    trim    = true
    type    = "virtio"
  }

  # シリアルコンソール。Talos は SSH を持たないため、
  # 起動失敗時の唯一の目視手段になる（Proxmox の Console から参照）。
  serial_device {
    device = "socket"
  }

  vga {
    type = "serial0"
  }

  # ホスト起動時に自動起動する
  started = true
  on_boot = true

  # ---------------------------------------------------------------------------
  # 設定変更時に VM を自動再起動しない
  #
  # Talos の machine config は talos_machine_configuration_apply が
  # 適用し、必要に応じて Talos 自身が再起動を判断する。Proxmox 側から
  # 勝手に再起動されると、etcd のクォーラムを崩す形で複数ノードが
  # 同時に落ちる恐れがある。
  # ---------------------------------------------------------------------------
  reboot_after_update = false
}
