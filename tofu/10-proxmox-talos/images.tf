# ---------------------------------------------------------------------------
# Talos イメージ
#
# Image Factory の schematic をコードとして定義する。これにより
# 「このクラスタのノードには何が入っているのか」がリポジトリから読み取れる。
# ブラウザで factory.talos.dev を操作して schematic ID を控える運用は、
# その情報がコードに残らないため採用しない。
# ---------------------------------------------------------------------------
resource "talos_image_factory_schematic" "this" {
  schematic = local.schematic_yaml
}

# nocloud プラットフォームの URL を取得する。
#
# なぜ metal ではなく nocloud なのか:
#   VLAN40 に DHCP サーバが存在しないことを実測で確認済み。metal ISO で起動すると
#   ノードに IP が付かず、machine config を適用する術がない（鶏と卵）。
#   nocloud プラットフォームなら Proxmox の cloud-init ドライブ（cidata）から
#   user-data として machine config を読み込むため、**初回起動の時点で
#   静的 IP を含む完全な設定が適用された状態**になる。
data "talos_image_factory_urls" "this" {
  talos_version = var.talos_version
  schematic_id  = talos_image_factory_schematic.this.id
  platform      = "nocloud"
  architecture  = "amd64"
}

# ISO を配置する。
#
# ⚠️ Ceph 廃止により共有ストレージが無くなったため、各ノードのローカル
#    `local`（/var/lib/vz）へ置く。bpg provider は node_name で指定した
#    1 ノードにのみダウンロードするため、VM を作る全ノードで
#    ISO が必要になる場合は for_each でノードごとに作ること。
#    現構成は 1 物理ノード 1 VM なので、下の for_each で全ノードへ配置する。
resource "proxmox_download_file" "talos_iso" {
  # 各ノードのローカルストレージへ配置するため、ノードごとに作成する
  for_each = { for n in var.proxmox_nodes : n.name => n }

  content_type = "iso"
  datastore_id = var.iso_datastore_id
  node_name    = each.key

  # schematic ID をファイル名に含めることで、拡張構成を変更したときに
  # 別ファイルとして扱われ、古いイメージで起動する事故を防ぐ。
  file_name = "talos-${var.talos_version}-${substr(talos_image_factory_schematic.this.id, 0, 12)}-nocloud-amd64.iso"
  url       = data.talos_image_factory_urls.this.urls.iso

  # 同名ファイルがあっても再取得しない（帯域と時間の節約）
  overwrite = false
}
