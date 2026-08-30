# ---------------------------------------------------------------------------
# Proxmox VE プロバイダ
#
# 認証は **API トークン** を使う。root@pam のパスワードを使わない理由:
#   - トークンは権限を絞れる（PVEVMAdmin + PVEDatastoreUser 程度で足りる）
#   - 失効・ローテーションがパスワード変更より安全かつ容易
#   - ステートやログに平文パスワードが残るリスクを避けられる
#
# SSH 接続が必要な理由:
#   Proxmox API には snippets をアップロードするエンドポイントが存在しないため、
#   bpg プロバイダは SSH/SFTP 経由でファイルを配置する。machine config を
#   cloud-init の user-data として渡す本構成では SSH が必須になる。
# ---------------------------------------------------------------------------
provider "proxmox" {
  endpoint  = var.proxmox_endpoint
  api_token = var.proxmox_api_token

  # 自己署名証明書を使っている場合のみ true。可能なら false にし、
  # Proxmox に正式な証明書を入れること。
  insecure = var.proxmox_insecure

  ssh {
    agent    = var.proxmox_ssh_agent
    username = var.proxmox_ssh_username

    # ssh-agent を使わない場合は秘密鍵のパスを指定する
    private_key = var.proxmox_ssh_private_key != "" ? file(var.proxmox_ssh_private_key) : null

    dynamic "node" {
      for_each = var.proxmox_nodes
      content {
        name    = node.value.name
        address = node.value.address
      }
    }
  }
}

provider "talos" {}
