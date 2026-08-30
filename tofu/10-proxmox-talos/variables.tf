# ===========================================================================
# Proxmox 接続
# ===========================================================================
variable "proxmox_endpoint" {
  description = "Proxmox VE の API エンドポイント（例: https://172.16.10.11:8006/）"
  type        = string

  validation {
    condition     = can(regex("^https://", var.proxmox_endpoint))
    error_message = "proxmox_endpoint は https:// で始まる必要があります。"
  }
}

variable "proxmox_api_token" {
  description = <<-EOT
    Proxmox API トークン（形式: USER@REALM!TOKENID=UUID）。
    root@pam のパスワードではなく、専用ユーザーのトークンを使うこと。
    作成手順は docs/50-operations.md を参照。
  EOT
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[^@]+@[^!]+![^=]+=.+$", var.proxmox_api_token))
    error_message = "proxmox_api_token は USER@REALM!TOKENID=UUID の形式である必要があります。"
  }
}

variable "proxmox_pool_id" {
  description = <<-EOT
    Kubernetes ノード VM を所属させる Proxmox のリソースプール。

    API トークンの ACL をこのプールに限定することで、トークンが漏洩しても
    Kubernetes 以外の VM やストレージには手が出せないようにしている。
    事前に `pveum pool add k8s` で作成しておくこと
    （手順は docs/50-operations.md §2.2）。
  EOT
  type        = string
  default     = "k8s"
}

variable "proxmox_insecure" {
  description = "Proxmox の TLS 証明書検証をスキップするか（自己署名証明書の場合のみ true）"
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# ⚠️ SSH 関連の変数は意図的に持たせていない
#
# 本構成は Proxmox API のみで完結する（snippets を使わない設計のため）。
# provider に SSH 設定を渡すと「OpenTofu 実行環境 = Proxmox root SSH が
# 使える環境」になり、API トークンの権限を絞る意味が薄れる。
# 詳細は providers.tf のコメントを参照。
# ---------------------------------------------------------------------------

variable "proxmox_nodes" {
  description = "Proxmox クラスタのノード一覧（VM の配置先ノード名の参照に使う）"
  type = list(object({
    name    = string
    address = string
  }))
  default = [
    { name = "sv-proxmox-01", address = "172.16.10.11" },
    { name = "sv-proxmox-02", address = "172.16.10.12" },
    { name = "sv-proxmox-03", address = "172.16.10.13" },
  ]
}

# ===========================================================================
# ストレージ
# ===========================================================================
variable "iso_datastore_id" {
  description = "Talos ISO を置く Proxmox ストレージ（全ノードから見える共有ストレージであること）"
  type        = string
  default     = "cephfs01"
}

variable "snippet_datastore_id" {
  description = <<-EOT
    cloud-init user-data（machine config）を置くストレージ。
    `snippets` content type が有効化されている必要がある:
      pvesm set cephfs01 --content backup,vztmpl,iso,snippets
  EOT
  type        = string
  default     = "cephfs01"
}

variable "vm_datastore_id" {
  description = "VM ディスクを置く Proxmox ストレージ（Ceph RBD プール）"
  type        = string
  default     = "cephrdb_k8s"
}

# ===========================================================================
# Talos / Kubernetes
# ===========================================================================
variable "talos_version" {
  description = "Talos Linux のバージョン（例: v1.13.9）"
  type        = string
  default     = "v1.13.9"

  validation {
    condition     = can(regex("^v[0-9]+\\.[0-9]+\\.[0-9]+$", var.talos_version))
    error_message = "talos_version は vX.Y.Z の形式である必要があります。"
  }
}

variable "kubernetes_version" {
  description = "Kubernetes のバージョン（Talos がサポートする範囲であること）"
  type        = string
  default     = "v1.34.3"

  validation {
    condition     = can(regex("^v[0-9]+\\.[0-9]+\\.[0-9]+$", var.kubernetes_version))
    error_message = "kubernetes_version は vX.Y.Z の形式である必要があります。"
  }
}

variable "cluster_name" {
  description = "Kubernetes クラスタ名"
  type        = string
  default     = "homelab"
}

variable "talos_extensions" {
  description = <<-EOT
    Image Factory に組み込む公式 system extension。
    qemu-guest-agent は Proxmox から VM の状態取得・正常シャットダウンを
    行うために必要。不要な拡張は攻撃面になるため追加しないこと。
  EOT
  type        = list(string)
  default     = ["siderolabs/qemu-guest-agent"]
}

# ===========================================================================
# ネットワーク
# ===========================================================================
variable "network_bridge" {
  description = "VM を接続する Proxmox ブリッジ（VLAN aware であること）"
  type        = string
  default     = "vmbr1"
}

variable "vlan_k8s" {
  description = "Kubernetes 用 VLAN ID"
  type        = number
  default     = 40
}

variable "vlan_ceph_public" {
  description = "Ceph public network の VLAN ID"
  type        = number
  default     = 20
}

variable "k8s_gateway" {
  description = "VLAN40 のデフォルトゲートウェイ"
  type        = string
  default     = "172.16.40.1"
}

variable "cluster_vip" {
  description = "kube-apiserver の VIP（Talos 内蔵 VIP 機能が control-plane 間で共有する）"
  type        = string
  default     = "172.16.40.10"
}

variable "nameservers" {
  description = "ノードの DNS サーバ"
  type        = list(string)
  default     = ["172.16.40.1", "1.1.1.1"]
}

variable "ntp_servers" {
  description = "ノードの NTP サーバ"
  type        = list(string)
  default     = ["ntp.nict.jp", "time.cloudflare.com"]
}

variable "pod_cidr" {
  description = "Pod ネットワークの CIDR"
  type        = string
  default     = "10.244.0.0/16"
}

variable "service_cidr" {
  description = "Service ネットワークの CIDR"
  type        = string
  default     = "10.96.0.0/12"
}

variable "management_cidrs" {
  description = <<-EOT
    Talos API (50000) と kube-apiserver (6443) へのアクセスを許可する送信元 CIDR。
    ここを広げることは、管理平面を露出させることと同義である。最小限に保つこと。
      - 172.16.40.0/24 : Kubernetes ノード自身（ノード間通信に必要）
      - 172.16.10.0/24 : Proxmox 管理セグメント
      - 100.64.0.0/10  : Tailscale (CGNAT 帯) 経由の運用端末
  EOT
  type        = list(string)
  default     = ["172.16.40.0/24", "172.16.10.0/24", "100.64.0.0/10"]
}

variable "ceph_public_cidr" {
  description = "Ceph public network の CIDR"
  type        = string
  default     = "172.16.20.0/24"
}

# ===========================================================================
# ノード定義
# ===========================================================================
variable "control_plane_nodes" {
  description = "control-plane ノードの定義"
  type = map(object({
    vmid       = number
    pve_node   = string
    ip         = string # VLAN40
    ceph_ip    = string # VLAN20
    mac_k8s    = string
    mac_ceph   = string
    cores      = number
    memory_mib = number
    disk_gib   = number
  }))
  default = {
    "k8s-cp-1" = {
      vmid    = 1001, pve_node = "sv-proxmox-01"
      ip      = "172.16.40.11", ceph_ip = "172.16.20.41"
      mac_k8s = "BC:24:11:40:00:11", mac_ceph = "BC:24:11:20:00:11"
      cores   = 4, memory_mib = 8192, disk_gib = 60
    }
    "k8s-cp-2" = {
      vmid    = 1002, pve_node = "sv-proxmox-02"
      ip      = "172.16.40.12", ceph_ip = "172.16.20.42"
      mac_k8s = "BC:24:11:40:00:12", mac_ceph = "BC:24:11:20:00:12"
      cores   = 4, memory_mib = 8192, disk_gib = 60
    }
    "k8s-cp-3" = {
      vmid    = 1003, pve_node = "sv-proxmox-03"
      ip      = "172.16.40.13", ceph_ip = "172.16.20.43"
      mac_k8s = "BC:24:11:40:00:13", mac_ceph = "BC:24:11:20:00:13"
      cores   = 4, memory_mib = 8192, disk_gib = 60
    }
  }
}

variable "worker_nodes" {
  description = "worker ノードの定義"
  type = map(object({
    vmid       = number
    pve_node   = string
    ip         = string
    ceph_ip    = string
    mac_k8s    = string
    mac_ceph   = string
    cores      = number
    memory_mib = number
    disk_gib   = number
  }))
  default = {
    "k8s-wk-1" = {
      vmid    = 1101, pve_node = "sv-proxmox-01"
      ip      = "172.16.40.21", ceph_ip = "172.16.20.51"
      mac_k8s = "BC:24:11:40:00:21", mac_ceph = "BC:24:11:20:00:21"
      cores   = 6, memory_mib = 20480, disk_gib = 120
    }
    "k8s-wk-2" = {
      vmid    = 1102, pve_node = "sv-proxmox-02"
      ip      = "172.16.40.22", ceph_ip = "172.16.20.52"
      mac_k8s = "BC:24:11:40:00:22", mac_ceph = "BC:24:11:20:00:22"
      cores   = 6, memory_mib = 20480, disk_gib = 120
    }
    "k8s-wk-3" = {
      vmid    = 1103, pve_node = "sv-proxmox-03"
      ip      = "172.16.40.23", ceph_ip = "172.16.20.53"
      mac_k8s = "BC:24:11:40:00:23", mac_ceph = "BC:24:11:20:00:23"
      cores   = 6, memory_mib = 20480, disk_gib = 120
    }
  }
}

# ===========================================================================
# 出力ファイル
# ===========================================================================
variable "output_dir" {
  description = "kubeconfig / talosconfig の出力先ディレクトリ（.gitignore 済み）"
  type        = string
  default     = "../../_out"
}

# ===========================================================================
# ステート暗号化
# ===========================================================================
variable "state_encryption_passphrase" {
  description = <<-EOT
    OpenTofu のステート/プランを暗号化するパスフレーズ（16 文字以上）。

    環境変数で渡すこと:
      export TF_VAR_state_encryption_passphrase="$(openssl rand -base64 32)"

    ⚠️ このパスフレーズを失うとステートを復号できなくなる。
       age 秘密鍵と同様、パスワードマネージャへ必ず保管すること。

    ⚠️ tfvars ファイルに書かないこと。ステートを守るための鍵が
       ステートと同じディレクトリに平文で置かれては意味がない。
  EOT
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.state_encryption_passphrase) >= 16
    error_message = "state_encryption_passphrase は 16 文字以上である必要があります（PBKDF2 の要件）。"
  }
}
