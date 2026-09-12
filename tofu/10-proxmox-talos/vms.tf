# ===========================================================================
# Kubernetes ノード VM
#
# 1物理ノードにつきcontrol-plane 1 VM + worker 1 VMの6ノード構成。
#
# ディスクは 2 本:
#   scsi0 = OS（Talos がインストールされる）
#   scsi1 = Longhorn のデータ領域（UserVolumeConfig で切り出す）
#
# NIC は VLAN40 の 1 枚のみ。Ceph を廃止したため VLAN20 への接続は不要になった
# （[ADR-0009](../../docs/adr/0009-drop-ceph-adopt-longhorn.md)）。
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

  # ---------------------------------------------------------------------------
  # リソースプール
  #
  # API トークンの ACL を `/pool/k8s` に限定しているため、VM は必ず
  # このプールに属している必要がある（docs/50-operations.md §2.2）。
  # これにより、トークンが漏洩しても Kubernetes 以外の VM
  # （OpenClaw 等）には手が出せない。
  # ---------------------------------------------------------------------------
  pool_id = var.proxmox_pool_id

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
  # control-plane の OS / etcd は NVMe（local-lvm）へ分離する。
  # SATA SSD の ZFS では fsync が秒単位で停止し、API timeout が発生した。
  # worker の OS と Longhorn データはローカル ZFS（local-zfs）上に置く。
  # Ceph を廃止したため共有ストレージは無く、VM はノードに固定される。
  # Talos ノードはステートレスに近く「壊れたら tofu で作り直す」運用が
  # 成立するため、これで問題ない。作り直せないデータ（PV）は
  # Longhorn が 3 レプリカで保護する。
  # ---------------------------------------------------------------------------
  #
  # ⚠️ datastore_id の変更は VM 再作成ではなく move_disk（オンラインの
  #    ディスク移動）になる。既定の -parallelism=10 では control-plane
  #    3 台の移動が同時に走り、分離の理由である ZFS プールへ 3 本の
  #    フルコピーを同時にかけることになる。必ず 1 台ずつ実行する。
  #
  #      tofu apply -target='proxmox_virtual_environment_vm.node["k8s-1"]'
  #      # etcd のメンバー健全性を確認してから次の 1 台へ
  #
  #    事前に、移動先データストアに disk_gib 以上の空きがあることを
  #    各ノードで確認する。
  # ---------------------------------------------------------------------------
  # --- OS ディスク ---
  disk {
    datastore_id = each.value.role == "controlplane" ? var.controlplane_os_datastore_id : var.vm_datastore_id
    interface    = "scsi0"
    size         = each.value.disk_gib
    file_format  = "raw"
    # SSD として見せることで、ゲスト側が discard を発行しやすくなる
    ssd = true
    # 未使用ブロックを ZFS へ返却する（thin provision を維持する）
    discard = "on"
    # I/O を専用スレッドで処理し、他の VM の影響を受けにくくする
    iothread = true
    cache    = "none"
  }

  # ---------------------------------------------------------------------------
  # --- Longhorn データディスク ---
  #
  # OS と分ける理由: Talos の再インストールや upgrade で EPHEMERAL パーティションが
  # 初期化されても、この専用ディスク上の Longhorn データは失われない。
  # Talos の UserVolumeConfig がこのディスクを検出して
  # /var/mnt/longhorn へマウントする（talos/patches/ を参照）。
  #
  # ⚠️ serial を固定している。Talos の diskSelector がこの値で
  #    「どちらが Longhorn 用か」を判別するため、変更してはならない。
  # ---------------------------------------------------------------------------
  disk {
    datastore_id = var.vm_datastore_id
    interface    = "scsi1"
    size         = var.longhorn_disk_gib
    file_format  = "raw"
    ssd          = true
    discard      = "on"
    iothread     = true
    cache        = "none"
    serial       = "longhorn"
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
    file_id   = proxmox_download_file.talos_iso[each.value.pve_node].id
    interface = "ide0"
  }

  boot_order = ["scsi0", "ide0"]

  # ---------------------------------------------------------------------------
  # cloud-init（初回到達用の IP / DNS だけ）
  #
  # ⚠️ ここには machine config（＝クラスタの秘密鍵）を置かない。
  #    理由は machine-config.tf の冒頭コメントを参照。
  #
  # Talos の nocloud プラットフォームは cloud-init の network-config を
  # 解釈する。これは初回Talos API到達のためだけに使う。永続network設定、
  # OS/Kubernetes設定、middlewareをcloud-initへ載せない。
  # ---------------------------------------------------------------------------
  initialization {
    datastore_id = var.vm_datastore_id
    interface    = "ide2"

    ip_config {
      ipv4 {
        address = "${each.value.ip}/24"
        gateway = var.k8s_gateway
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
