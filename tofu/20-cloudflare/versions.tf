terraform {
  required_version = ">= 1.8.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.24"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }

  # ---------------------------------------------------------------------------
  # ⚠️ ステートの取り扱い
  #
  # このステートには以下の機密が平文で含まれる:
  #   - Tunnel の TunnelSecret（これを持つ者は同じトンネルを張れる）
  #   - Access Service Token の client_secret
  #
  # 10-proxmox-talos と同様、ローカル保管か暗号化バックエンドを使うこと。
  # ---------------------------------------------------------------------------
}
