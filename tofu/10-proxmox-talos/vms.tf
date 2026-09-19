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
  #
  # backup: worker では PBS のバックアップ対象から外す。
  #    このディスクは Talos の EPHEMERAL（/var）で、中身はコンテナイメージ・
  #    Pod ログ・emptyDir である。いずれも再取得できる。worker の
  #    代替不能なデータは Longhorn 側（scsi1）にあり、そちらは取り続ける。
  #
  #    ⚠️ 外す理由は「優先度が低いから」ではなく、**取ると PBS が破綻するから**
  #    である。2026-09-19 の実測（docs/pbs-capacity-2026-09-19.md）:
  #
  #      - EPHEMERAL は LUKS2 で暗号化されている（talos/patches/*.tftpl の
  #        systemDiskEncryption、鍵は nodeID＝ノードごとに別）。このため
  #        同じコンテナイメージでもノード間で完全に別のチャンクになり、
  #        PBS の重複排除が効かない。圧縮も効かない（ZFS compressratio 1.00x）
  #      - 4 MiB チャンク内の 1 セクタが変わるだけで全体が新規チャンクになる。
  #        実測で毎晩 OS ディスクの 40〜62% が新規チャンクになっていた
  #      - 結果、worker 3 台の scsi0 だけで 1 世代 89.1 GiB、毎晩 +47 GiB。
  #        データストア全体 428 GiB のうち 166.9 GiB をこれが占めていた
  #
  #    復旧経路は変わらない。worker が壊れたら tofu で作り直して
  #    talosctl apply-config で再参加させ、Longhorn が他の 2 台から
  #    レプリカを再構築する（scripts/rebuild-talos-cluster.sh）。
  #    5 日前の EPHEMERAL イメージを書き戻すより、そちらの方が速く確実である。
  #
  #    control-plane では取り続ける。etcd のデータが /var/lib/etcd にあり、
  #    1 世代 16.6 GiB と安い。ADR-0012 のとおり etcd スナップショットは
  #    別途取っていないため、ここが唯一のコピーになる。
  # ---------------------------------------------------------------------------
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
    backup   = each.value.role == "controlplane"
  }

  # ---------------------------------------------------------------------------
  # --- Longhorn データディスク ---
  #
  # OS と分ける理由: Talos の再インストールや upgrade で EPHEMERAL パーティションが
  # 初期化されても、この専用ディスク上の Longhorn データは失われない。
  # Talos の UserVolumeConfig がこのディスクを検出して
  # /var/mnt/longhorn へマウントする（talos/patches/ を参照）。
  #
  # ⚠️ serial = "longhorn" を付けているが、**Talos はこの値を見ていない。**
  #    2026-09-15 に talosctl で実測したところ、ゲスト内の Talos は
  #    どちらのディスクにも serial を報告しない（両方とも
  #    model="QEMU HARDDISK" / transport=virtio、by-id は drive-scsiN）。
  #    実際の判別条件は talos/patches/worker.yaml.tftpl の
  #    `match: '!system_disk'` とサイズ（minSize）である。
  #    詳細と実測値は同ファイルのコメント、および
  #    docs/storage-migration-2026-09-13.md を参照。
  #
  # backup: control-plane では PBS のバックアップ対象から外す。
  #    Longhorn の storage node は worker 3 台だけで
  #    （worker.yaml.tftpl の node.longhorn.io/create-default-disk ラベル）、
  #    control のこのディスクは実使用 816K の空ディスクである。
  #    バックアップしても得るものが無い。worker 側は Longhorn データの
  #    唯一のクラスタ外コピーなので必ず含める（ADR-0012 の階層3。
  #    Velero を採らない判断をしたため、PBS が S5/S6 を単独で受けている）。
  #
  #    ⚠️ ここが PBS の容量の大半を占める。Longhorn は同じデータを 3 レプリカ
  #    持つが、レプリカのファイル配置がノードごとに違うため PBS から見ると
  #    別データであり、ノード間の重複排除が効かない（2026-09-19 の実測で
  #    3 ワーカー間の共有は 99.3 GiB 中 0.06 GiB ＝ 0.06%）。
  #    つまり **同じデータを 3 重に保存している**。1 世代 99.3 GiB、毎晩 +41.5 GiB。
  #    うち約 6 割は Prometheus の TSDB（30.5 GiB × 3 レプリカ）である。
  #    保持世代を増やせない根本要因はここにある。
  #    詳細は docs/pbs-capacity-2026-09-19.md を参照。
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
    backup       = each.value.role != "controlplane"
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
