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
  description = <<-EOT
    Talos ISO を置く Proxmox ストレージ。

    Ceph 廃止後は共有ストレージが無くなるため、各ノードのローカル `local`
    （/var/lib/vz）に置く。ISO は各ノードへ個別にダウンロードされるが、
    数百 MB なので許容できる。
  EOT
  type        = string
  default     = "local"
}

variable "vm_datastore_id" {
  description = <<-EOT
    VM ディスクを置く Proxmox ストレージ。

    Ceph を廃止し、各ノードの SATA SSD を単体 ZFS（local-zfs）にした構成を前提とする。
    共有ストレージではないため VM はノードに固定されるが、Talos ノードは
    ステートレスに近く「壊れたら tofu で作り直す」運用が成立するため問題ない。
    作り直せないデータ（PV）は Longhorn が 3 レプリカで保護する。
  EOT
  type        = string
  default     = "local-zfs"
}

variable "controlplane_os_datastore_id" {
  description = <<-EOT
    control-plane の OS / etcd ディスクを置く Proxmox ストレージ。
    SATA SSD の local-zfs では同期書き込みが数秒停止し API timeout を起こしたため、
    NVMe の local-lvm に分離する。各 Proxmox ノードに同名ストレージが必要。
    worker の OS と Longhorn データディスクは vm_datastore_id を使用する。
  EOT
  type        = string
  default     = "local-lvm"
}

variable "longhorn_disk_gib" {
  description = <<-EOT
    Longhorn 用のデータディスクサイズ（GiB）。OS ディスクとは別に付ける。

    分ける理由: Talos の再インストールや upgrade で EPHEMERAL パーティションが
    初期化されても、この専用ディスク上の Longhorn データは失われない。
    OS とデータのライフサイクルを分離しておくことが復旧時に効く。
  EOT
  type        = number
  default     = 300
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

    ⚠️ 不要な拡張は攻撃面になる。追加する際は「何のために必要か」を
       ここに書き残すこと。

      siderolabs/qemu-guest-agent
        Proxmox から VM の状態取得・正常シャットダウンを行うために必要。

      siderolabs/iscsi-tools
        Longhorn が PV を iSCSI でノードへアタッチするために必要
        （iscsid / iscsiadm を提供する）。これが無いと Pod が
        ボリュームをマウントできず永久に ContainerCreating のままになる。

      siderolabs/util-linux-tools
        Longhorn がボリュームの trim（fstrim）を行うために必要。
        無くても動くが、削除済みブロックが解放されず容量を食い続ける。
  EOT
  type        = list(string)
  default = [
    "siderolabs/qemu-guest-agent",
    "siderolabs/iscsi-tools",
    "siderolabs/util-linux-tools",
  ]
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

# ===========================================================================
# ノード定義
# ===========================================================================
variable "control_plane_nodes" {
  description = <<-EOT
    Kubernetes control-plane ノードの定義。

    etcd quorumを3台で構成し、各Proxmoxホストへ1台ずつ配置する。
    application workloadは専用workerへ限定し、control-planeには載せない。
  EOT
  type = map(object({
    vmid       = number
    pve_node   = string
    ip         = string
    mac_k8s    = string
    cores      = number
    memory_mib = number
    disk_gib   = number
  }))
  default = {
    "k8s-1" = {
      vmid    = 1001, pve_node = "sv-proxmox-01"
      ip      = "172.16.40.11"
      mac_k8s = "BC:24:11:40:00:11"
      cores   = 4, memory_mib = 16384, disk_gib = 60
    }
    "k8s-2" = {
      vmid    = 1002, pve_node = "sv-proxmox-02"
      ip      = "172.16.40.12"
      mac_k8s = "BC:24:11:40:00:12"
      cores   = 4, memory_mib = 16384, disk_gib = 60
    }
    "k8s-3" = {
      vmid    = 1003, pve_node = "sv-proxmox-03"
      ip      = "172.16.40.13"
      mac_k8s = "BC:24:11:40:00:13"
      cores   = 4, memory_mib = 16384, disk_gib = 60
    }
  }
}

variable "worker_nodes" {
  description = <<-EOT
    Kubernetes workerノードの定義。

    application workloadとLonghorn replicaを担当し、各Proxmoxホストへ
    1台ずつ配置する。VMIDは1101以降を使用する。
  EOT
  type = map(object({
    vmid       = number
    pve_node   = string
    ip         = string
    mac_k8s    = string
    cores      = number
    memory_mib = number
    disk_gib   = number
  }))
  default = {
    "k8s-worker-1" = {
      vmid    = 1101, pve_node = "sv-proxmox-01"
      ip      = "172.16.40.21"
      mac_k8s = "BC:24:11:40:00:21"
      cores   = 6, memory_mib = 20480, disk_gib = 60
    }
    "k8s-worker-2" = {
      vmid    = 1102, pve_node = "sv-proxmox-02"
      ip      = "172.16.40.22"
      mac_k8s = "BC:24:11:40:00:22"
      cores   = 6, memory_mib = 20480, disk_gib = 60
    }
    "k8s-worker-3" = {
      vmid    = 1103, pve_node = "sv-proxmox-03"
      ip      = "172.16.40.23"
      mac_k8s = "BC:24:11:40:00:23"
      cores   = 6, memory_mib = 20480, disk_gib = 60
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
