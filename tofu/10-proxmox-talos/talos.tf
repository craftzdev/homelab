# ===========================================================================
# Talos クラスタの構築
#
# 流れ:
#   1) VM が起動し、cloud-init の network-config で静的 IP が付く
#      → Talos はメンテナンスモード（machine config 未適用）
#   2) machine config を Talos API 経由で適用
#      → 各ノードが自身をディスクへインストールし、再起動する
#   3) control-plane の 1 台で etcd を bootstrap
#   4) kubeconfig を取得
#
# この時点ではまだ CNI が入っていないため、ノードは NotReady のままである。
# Cilium の導入は scripts/bootstrap-cluster.sh が担う。
# ===========================================================================

# ---------------------------------------------------------------------------
# VM の起動待ち
#
# Proxmox の API が「VM を作成した」と返した時点では、まだ Talos の
# API（:50000）は応答しない。ISO からの起動と cloud-init による
# ネットワーク設定が終わるまで待つ必要がある。
#
# provider 側にもリトライはあるが、初回の接続失敗でエラーになる事故を
# 避けるため明示的に待機する。
# ---------------------------------------------------------------------------
resource "time_sleep" "wait_for_vm_boot" {
  depends_on      = [proxmox_virtual_environment_vm.node]
  create_duration = "120s"

  triggers = {
    # VM が再作成されたら、この待機もやり直す
    vm_ids = join(",", [for k, v in proxmox_virtual_environment_vm.node : v.id])
  }
}

# ---------------------------------------------------------------------------
# machine config の適用
#
# 初回はノードがメンテナンスモード（クライアント証明書による認証が
# まだ確立していない状態）のため、provider が自動的に insecure モードで
# 接続する。2 回目以降は mTLS で接続される。
# ---------------------------------------------------------------------------
resource "talos_machine_configuration_apply" "node" {
  for_each = local.all_nodes

  depends_on = [time_sleep.wait_for_vm_boot]

  client_configuration        = talos_machine_secrets.this.client_configuration
  machine_configuration_input = data.talos_machine_configuration.node[each.key].machine_configuration

  node     = each.value.ip
  endpoint = each.value.ip

  # 変更を適用する方法。auto はノードの状態に応じて
  # 無停止反映 / 再起動を Talos 側が判断する。
  apply_mode = "auto"

  # tofu destroy 時にノードを reset（初期化）する。
  # これを false にすると、VM を作り直したときに古い etcd メンバー情報が
  # 残って join に失敗する事故が起きる。
  on_destroy = {
    # A full six-node rebuild destroys all etcd members. Graceful leave cannot
    # succeed for the final member after quorum has already disappeared, and
    # parallel provider deletion makes the failure nondeterministic. The VMs
    # and their disks are deleted immediately afterwards, so forced reset is
    # the correct whole-cluster lifecycle behavior.
    graceful = false
    reboot   = false
    reset    = true
  }
}

# ---------------------------------------------------------------------------
# etcd の bootstrap
#
# control-plane の 1 台に対してのみ実行する。残りの 2 台は
# machine config の cluster_endpoint を見て自動的に join する。
#
# ⚠️ このリソースは 1 度しか実行できない。既に bootstrap 済みの
#    クラスタに再実行するとエラーになる（provider が検知する）。
# ---------------------------------------------------------------------------
resource "talos_machine_bootstrap" "this" {
  depends_on = [talos_machine_configuration_apply.node]

  client_configuration = talos_machine_secrets.this.client_configuration
  node                 = local.controlplane_ips[0]
  endpoint             = local.controlplane_ips[0]
}

# Cluster health is verified by scripts/bootstrap-cluster.sh after Cilium is
# installed. A talos_cluster_health data source runs during every plan refresh;
# that makes a corrective plan impossible precisely when the cluster is
# unhealthy, so it must not be part of the state graph.

# ---------------------------------------------------------------------------
# kubeconfig の取得
# ---------------------------------------------------------------------------
resource "talos_cluster_kubeconfig" "this" {
  depends_on = [talos_machine_bootstrap.this]

  client_configuration = talos_machine_secrets.this.client_configuration
  node                 = local.controlplane_ips[0]
  endpoint             = local.controlplane_ips[0]
}

# ---------------------------------------------------------------------------
# 認証情報をローカルファイルへ出力
#
# ⚠️ これらのファイルはクラスタの**完全な管理権限**そのものである。
#
#   kubeconfig  : cluster-admin 相当の認証情報
#   talosconfig : Talos API の全操作が可能なクライアント証明書
#
# ファイルは 0600、ディレクトリは 0700 で作成する。ディレクトリの権限を
# 指定しないと 0755 で作られ、同一マシンの他ユーザーからディレクトリを
# 一覧できてしまう（中身は読めないが、存在と名前が漏れる）。
#
# ⚠️⚠️ さらに重要な注意:
#   OpenTofu の **ステートファイルにも同じ秘密が平文で含まれる**。
#   `_out/` だけを守ってもステートが無防備なら意味がない。
#   ステートの保護方針は versions.tf の注記および
#   docs/50-operations.md §7.3 を参照すること。
# ---------------------------------------------------------------------------
resource "local_sensitive_file" "kubeconfig" {
  content              = talos_cluster_kubeconfig.this.kubeconfig_raw
  filename             = "${var.output_dir}/kubeconfig"
  file_permission      = "0600"
  directory_permission = "0700"
}

resource "local_sensitive_file" "talosconfig" {
  content              = data.talos_client_configuration.this.talos_config
  filename             = "${var.output_dir}/talosconfig"
  file_permission      = "0600"
  directory_permission = "0700"
}
