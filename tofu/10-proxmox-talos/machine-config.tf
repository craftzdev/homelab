# ===========================================================================
# Talos machine config の生成
#
# ---------------------------------------------------------------------------
# 設計判断: machine config を Proxmox の snippets に置かない
# ---------------------------------------------------------------------------
# Talos の nocloud プラットフォームは cloud-init の user-data を machine config
# として読める。一見これが最も簡潔だが、machine config には
#
#   - Kubernetes の CA 秘密鍵
#   - etcd の CA 秘密鍵
#   - Talos の CA 秘密鍵
#   - bootstrap token
#
# が含まれる。これを Proxmox の snippets（CephFS 上の平文ファイル）に置くと、
# 「クラスタの全権限が、Proxmox ホスト上の 1 ファイルとして常設される」
# 状態になる。VM のライフサイクルを超えて残り続ける点も良くない。
#
# そこで本構成では:
#
#   1) cloud-init には **静的 IP の設定だけ** を渡す（秘密を一切含まない）
#      → Talos はメンテナンスモードで起動し、指定の IP を持つ
#   2) 完全な machine config は Talos API 経由で適用する
#      （talos_machine_configuration_apply）
#      → 秘密は TLS で保護された経路を通り、ノードの暗号化された
#        STATE パーティションにのみ保存される
#
# 副次的な利点として、Proxmox 側で snippets content type を有効化する
# 必要が無くなり、前提条件が 1 つ減る。
# ===========================================================================

locals {
  # ingress-firewall テンプレートに埋め込む YAML 断片。
  # `ingress:` の直下に来るため 2 スペースでインデントする。
  management_ingress = join("\n", [
    for cidr in var.management_cidrs : "  - subnet: ${cidr}"
  ])

  # 各ノードのテンプレート変数。control-plane / worker で同じ map を渡す。
  # （templatefile は vars に未使用のキーがあってもエラーにならない）
  node_template_vars = {
    for name, node in local.all_nodes : name => {
      hostname        = name
      mac_k8s         = lower(node.mac_k8s)
      ip              = node.ip
      gateway         = var.k8s_gateway
      vip             = var.cluster_vip
      nameservers     = jsonencode(var.nameservers)
      ntp_servers     = jsonencode(var.ntp_servers)
      cert_sans       = jsonencode(local.cert_sans)
      installer_image = data.talos_image_factory_urls.this.urls.installer
      k8s_subnet      = local.k8s_subnet
      pod_cidr        = var.pod_cidr
      service_cidr    = var.service_cidr
    }
  }

  firewall_template_vars = {
    k8s_subnet            = local.k8s_subnet
    pod_cidr              = var.pod_cidr
    service_cidr          = var.service_cidr
    management_ingress    = local.management_ingress
    management_cidrs_desc = join(", ", var.management_cidrs)
  }
}

# ---------------------------------------------------------------------------
# クラスタのシークレット（CA・トークン類）
#
# ⚠️ これらは OpenTofu のステートに平文で保存される。
#    ステートの保護については versions.tf の注記を参照。
# ---------------------------------------------------------------------------
resource "talos_machine_secrets" "this" {
  talos_version = var.talos_version
}

# ---------------------------------------------------------------------------
# machine config の生成（ノードごと）
#
# config_patches は上から順に適用される。ingress-firewall は
# multi-document のパッチとして別ドキュメントを追加する。
# ---------------------------------------------------------------------------
data "talos_machine_configuration" "node" {
  for_each = local.all_nodes

  cluster_name     = var.cluster_name
  cluster_endpoint = "https://${var.cluster_vip}:6443"
  machine_type     = each.value.role
  machine_secrets  = talos_machine_secrets.this.machine_secrets

  talos_version      = var.talos_version
  kubernetes_version = var.kubernetes_version

  # 生成される config にコメントや例を含めない（差分が読みやすくなる）
  docs     = false
  examples = false

  config_patches = [
    templatefile(
      each.value.role == "controlplane"
      ? "${path.module}/../../talos/patches/controlplane.yaml.tftpl"
      : "${path.module}/../../talos/patches/worker.yaml.tftpl",
      local.node_template_vars[each.key]
    ),
    templatefile(
      "${path.module}/../../talos/patches/ingress-firewall.yaml.tftpl",
      local.firewall_template_vars
    ),
  ]
}

# ---------------------------------------------------------------------------
# talosctl 用のクライアント設定
#
# endpoints には VIP ではなく control-plane の実 IP を列挙する。
# VIP は 1 台にしか付かないため、そのノードが落ちると talosctl が
# 到達できなくなる。実 IP を並べておけば自動的にフォールバックする。
# ---------------------------------------------------------------------------
data "talos_client_configuration" "this" {
  cluster_name         = var.cluster_name
  client_configuration = talos_machine_secrets.this.client_configuration
  endpoints            = local.controlplane_ips
}
