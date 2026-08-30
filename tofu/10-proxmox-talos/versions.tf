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
  # ステートの暗号化（必須）
  #
  # ⚠️ このステートには talos_machine_secrets が生成した
  #    **Kubernetes / etcd / Talos の CA 秘密鍵**が含まれる。
  #    ステートを奪われることは、クラスタを完全に掌握されることと同義である。
  #
  # OpenTofu の state encryption により、ステートを **保存時点で暗号化** する。
  # ディスク上・端末バックアップ上・（リモートバックエンド利用時は）
  # オブジェクトストレージ上のいずれでも平文にならない。
  #
  # 使い方:
  #   export TF_VAR_state_encryption_passphrase="$(openssl rand -base64 32)"
  #
  #   ⚠️ このパスフレーズを失うとステートを復号できなくなる。
  #      age 秘密鍵と同様、パスワードマネージャへ必ず保管すること。
  #
  # `enforced = true` にしているため、パスフレーズが未設定なら
  # **tofu は平文で書き込まずに失敗する**。
  # 「暗号化し忘れて平文で保存されていた」という事故が起きない設計にしている。
  # ---------------------------------------------------------------------------
  encryption {
    key_provider "pbkdf2" "state" {
      passphrase = var.state_encryption_passphrase
    }

    method "aes_gcm" "state" {
      keys = key_provider.pbkdf2.state
    }

    state {
      method   = method.aes_gcm.state
      enforced = true
    }

    plan {
      method   = method.aes_gcm.state
      enforced = true
    }
  }

  # ---------------------------------------------------------------------------
  # リモートバックエンドへの移行（推奨）
  #
  # 上記の暗号化に加え、バージョニングとロックのあるバックエンドへ
  # 移行することを推奨する。端末の故障でステートを失うと、
  # 既存リソースを OpenTofu の管理下へ戻す作業が非常に面倒になる。
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
