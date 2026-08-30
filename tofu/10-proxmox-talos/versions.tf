terraform {
  required_version = ">= 1.8.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.111"
    }
    talos = {
      source  = "siderolabs/talos"
      version = "~> 0.11"
    }
    # kubeconfig / talosconfig をローカルへ書き出すために使用
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
    # VM 起動から Talos API が応答するまでの待機に使用
    time = {
      source  = "hashicorp/time"
      version = "~> 0.12"
    }
  }

  # ---------------------------------------------------------------------------
  # ⚠️ ステートの取り扱いについて（重要）
  #
  # このステートには talos_machine_secrets が生成した **Kubernetes / etcd / Talos
  # の CA 秘密鍵が平文で** 含まれる。ステートを奪われることは、クラスタを完全に
  # 掌握されることと同義である。
  #
  # 既定ではローカルステートを使う（.gitignore で除外済み）。以下のいずれかを
  # 必ず満たすこと:
  #   (a) FileVault 等でディスク暗号化された端末上でのみ扱う
  #   (b) 暗号化・バージョニング有効なリモートバックエンドへ移行する
  #
  # (b) の例（Cloudflare R2 / MinIO などの S3 互換）:
  #
  #   backend "s3" {
  #     bucket                      = "homelab-tfstate"
  #     key                         = "10-proxmox-talos/terraform.tfstate"
  #     region                      = "auto"
  #     endpoints                   = { s3 = "https://<accountid>.r2.cloudflarestorage.com" }
  #     skip_credentials_validation = true
  #     skip_region_validation      = true
  #     skip_requesting_account_id  = true
  #     skip_s3_checksum            = true
  #     use_path_style              = true
  #   }
  # ---------------------------------------------------------------------------
}
