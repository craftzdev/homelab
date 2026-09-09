locals {
  # control-plane と worker を 1 つの map に統合する。
  # role を持たせることで、VM 定義・machine config の生成を共通化できる。
  all_nodes = merge(
    { for k, v in var.control_plane_nodes : k => merge(v, { role = "controlplane" }) },
    { for k, v in var.worker_nodes : k => merge(v, { role = "worker" }) },
  )

  # Talos API / kubectl の接続先。VIP ではなく実 IP を列挙する理由:
  #   VIP は control-plane のいずれか 1 台にしか付かない。Talos API は
  #   各ノードが個別に応答するため、実 IP を並べておくことで
  #   1 台が落ちていても talosctl が別のノードへフォールバックできる。
  controlplane_ips = [for k, v in var.control_plane_nodes : v.ip]

  # kube-apiserver の証明書に載せる SAN。
  # VIP・各 CP の実 IP・localhost を含めることで、どの経路から繋いでも
  # 証明書検証が通るようにする。
  cert_sans = concat(
    [var.cluster_vip, "127.0.0.1"],
    local.controlplane_ips,
  )

  # Kubernetes ノードのサブネット（CIDR 表記）
  k8s_subnet = "${cidrhost("${var.k8s_gateway}/24", 0)}/24"

  # Image Factory の schematic（拡張入りイメージの定義）
  schematic_yaml = yamlencode({
    customization = {
      systemExtensions = {
        officialExtensions = var.talos_extensions
      }
    }
  })
}
