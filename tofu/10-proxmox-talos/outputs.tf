output "cluster_name" {
  description = "Kubernetes クラスタ名"
  value       = var.cluster_name
}

output "cluster_endpoint" {
  description = "kube-apiserver のエンドポイント（VIP 経由）"
  value       = "https://${var.cluster_vip}:6443"
}

output "control_plane_ips" {
  description = "control-plane ノードの IP（talosctl の -n / -e に使う）"
  value       = local.controlplane_ips
}

output "worker_ips" {
  description = "worker ノードの IP"
  value       = [for k, v in var.worker_nodes : v.ip]
}

output "talos_schematic_id" {
  description = "Image Factory の schematic ID（イメージの中身を特定する識別子）"
  value       = talos_image_factory_schematic.this.id
}

output "talos_installer_image" {
  description = "ノードにインストールされる Talos installer イメージ"
  value       = data.talos_image_factory_urls.this.urls.installer
}

output "kubeconfig_path" {
  description = "書き出された kubeconfig のパス"
  value       = local_sensitive_file.kubeconfig.filename
}

output "talosconfig_path" {
  description = "書き出された talosconfig のパス"
  value       = local_sensitive_file.talosconfig.filename
}

output "kubeconfig" {
  description = "kubeconfig の内容（sensitive）"
  value       = talos_cluster_kubeconfig.this.kubeconfig_raw
  sensitive   = true
}

output "talosconfig" {
  description = "talosconfig の内容（sensitive）"
  value       = data.talos_client_configuration.this.talos_config
  sensitive   = true
}

output "next_steps" {
  description = "この後に実行すべきこと"
  value       = <<-EOT

    ┌──────────────────────────────────────────────────────────────────┐
    │ Talos クラスタの構築が完了しました。                              │
    │ ただし CNI が未導入のため、ノードはまだ NotReady です。           │
    └──────────────────────────────────────────────────────────────────┘

    1) 環境変数を設定する
         export TALOSCONFIG="$(pwd)/${var.output_dir}/talosconfig"
         export KUBECONFIG="$(pwd)/${var.output_dir}/kubeconfig"

    2) Talos の状態を確認する
         talosctl -n ${local.controlplane_ips[0]} health --server=false
         talosctl -n ${local.controlplane_ips[0]} get members

    3) Cilium を導入してノードを Ready にする
         ../../scripts/bootstrap-cluster.sh

    4) Ceph の認証情報を作成し、ArgoCD を導入する
         ../../scripts/ceph-create-k8s-user.sh
         ../../scripts/bootstrap-argocd.sh

    詳細な手順は docs/50-operations.md を参照してください。
  EOT
}
