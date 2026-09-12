variable "cloudflare_api_token" {
  description = <<-EOT
    Cloudflare API トークン。

    必要な権限（これ以上は付けないこと）。published_services が空なら
    Access 関連リソースは作られないため、上 2 つだけでよい:

      Account | Cloudflare Tunnel        | Edit   ← 常に必要
      Zone    | DNS                      | Edit   ← 常に必要
      Account | Access: Apps and Policies| Edit   ← published_services を使う場合
      Account | Access: Service Tokens   | Edit   ← published_services を使う場合

    Access 系はいずれも Account スコープであり、ドロップダウンの 1 つ目を
    Zone にしていると候補に出てこない。

    Global API Key は絶対に使わないこと（全権限を持つため）。

    環境変数で渡すことを推奨:
      export TF_VAR_cloudflare_api_token='...'
  EOT
  type        = string
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Cloudflare のアカウント ID"
  type        = string
}

variable "cloudflare_zone_id" {
  description = "公開ホスト名を作成する DNS ゾーンの ID"
  type        = string
}

variable "cloudflare_team_name" {
  description = <<-EOT
    Cloudflare Zero Trust の team name（<team>.cloudflareaccess.com の <team> 部分）。
    cloudflared が Access の JWT を検証する際に使う。
  EOT
  type        = string
}

variable "tunnel_name" {
  description = "Cloudflare Tunnel の名前"
  type        = string
  default     = "homelab-k8s"
}

variable "service_token_name" {
  description = "SaaS（Cloudflare Workers）用の Access Service Token 名"
  type        = string
  default     = "saas-worker"
}

variable "service_token_duration" {
  description = <<-EOT
    Service Token の有効期間。

    既定は 8760h（1年）だが、意図的に短くしている。
    短い有効期限は「定期的にローテーションする」運用を強制するため、
    漏洩時の被害window を小さくできる。
    期限切れ時は tofu apply で更新される。
  EOT
  type        = string
  default     = "2160h" # 90 日
}

variable "service_token_secret_version" {
  description = <<-EOT
    この値をインクリメントすると Service Token のシークレットがローテーションされる。
    ローテーション後は Workers 側の secret も更新すること
    （scripts/sync-cloudflare-secrets.sh が手順を案内する）。
  EOT
  type        = number
  default     = 0
}

# ---------------------------------------------------------------------------
# Gatus（外形監視）用 Service Token
#
# saas_worker とは独立した変数にしている。片方のローテーションが
# もう片方を巻き込まないようにするためであり、
# secret_version を別々にインクリメントできることがその要点である。
# ---------------------------------------------------------------------------
variable "gatus_service_token_name" {
  description = "Gatus（外形監視）用の Access Service Token 名"
  type        = string
  default     = "gatus-monitor"
}

variable "gatus_service_token_duration" {
  description = <<-EOT
    Gatus 用 Service Token の有効期間。

    saas_worker と同じ 90 日にしている。監視用だからといって
    長い有効期限を与えると、「監視は止めたくない」という理由で
    ローテーションが先送りされ続ける。
  EOT
  type        = string
  default     = "2160h" # 90 日
}

variable "gatus_service_token_secret_version" {
  description = <<-EOT
    この値をインクリメントすると Gatus 用 Service Token のシークレットが
    ローテーションされる。ローテーション後は Keychain と Kubernetes Secret を
    scripts/bootstrap-cluster-secrets.sh で更新すること。
  EOT
  type        = number
  default     = 1
}

variable "published_services" {
  description = <<-EOT
    外部（Cloudflare Workers 上の SaaS）へ公開するサービスの定義。

    ⚠️ ここに追加することは「インターネットからアクセス可能にする」ことを意味する。
       docs/40-external-access.md §7 の選定基準を満たすもののみを追加すること。
       管理平面（Kubernetes API / ArgoCD UI / Proxmox / Talos API）は
       絶対に追加しない。

    フィールド:
      hostname         : 公開するホスト名（FQDN）
      origin_service   : cloudflared から見た転送先 URL（クラスタ内部）
      session_duration : Access のセッション有効期間
      path             : 特定パス配下のみ公開する場合に指定（省略可）
      health_path      : Gatus に監視させる health endpoint のパス（省略可）

    health_path を指定すると、そのパスだけを対象にした専用の Access
    アプリケーションが作られ、Gatus のサービストークンだけを許可する。

    ⚠️ 監視用トークンを既存のアプリケーションのポリシーに足してはならない。
       Access アプリケーションはホスト名単位で効くため、それをすると
       監視トークンが health endpoint 以外の全パスへ到達できてしまう。
       監視 Pod が侵害されたときに漏れるのは、health endpoint だけに
       到達できるトークンであるべきである。
  EOT
  type = map(object({
    hostname         = string
    origin_service   = string
    session_duration = optional(string, "30m")
    path             = optional(string)
    health_path      = optional(string)
  }))

  default = {
    "internal-api" = {
      hostname       = "api.internal.example.com"
      origin_service = "http://cilium-gateway-external.gateway.svc.cluster.local:80"
    }
  }

  validation {
    condition = alltrue([
      for k, v in var.published_services :
      !can(regex("(?i)(argocd|argo-cd|kubernetes|k8s-api|talos|proxmox|grafana-admin)", v.hostname))
    ])
    error_message = "管理平面と思われるホスト名が published_services に含まれています。管理系サービスをインターネットへ公開することは、この構成の設計方針に反します。"
  }

  # health_path は「1 パスだけに絞る」ことが目的である。空や / を許すと
  # ホスト名全体を監視トークンへ開くことになり、分けた意味が無くなる。
  validation {
    condition = alltrue([
      for k, v in var.published_services :
      v.health_path == null ? true : (
        startswith(v.health_path, "/") && length(v.health_path) > 1
      )
    ])
    error_message = "health_path は / で始まる 2 文字以上のパスである必要があります。ホスト名全体を監視トークンへ開かないためです。"
  }

  validation {
    condition = alltrue([
      for k, v in var.published_services :
      can(regex("^(http|https|tcp|unix)://", v.origin_service))
    ])
    error_message = "origin_service はスキーム付きの URL（http:// 等）である必要があります。"
  }
}

variable "public_services" {
  description = <<-EOT
    **匿名で** インターネットへ公開するサービスの定義。

    ⚠️ published_services との違いを必ず理解すること。

      published_services … Cloudflare Access のアプリケーションを作り、
                           サービストークンを必須にする。内部 API 用。
      public_services    … Access を一切作らない。誰でもアクセスできる。
                           一般公開の Web サイト用。

    ⚠️ ここに追加したホスト名は、**認証なしで世界中から到達可能**になる。
       WAF / DDoS 保護 / ボット対策は proxied = true により引き続き効くが、
       「誰がアクセスしてよいか」の制御は一切無い。
       公開してよいと明確に判断したものだけを追加すること。

    フラグではなく変数そのものを分けているのは、取り違えたときの影響が
    大きいためである。既定値の見落としで内部 API が無防備になる事故を
    構造的に防ぐ。

    フィールド:
      hostname       : 公開するホスト名（FQDN）
      origin_service : cloudflared から見た転送先 URL（クラスタ内部）
  EOT
  type = map(object({
    hostname       = string
    origin_service = string
  }))

  default = {}

  validation {
    condition = alltrue([
      for k, v in var.public_services :
      !can(regex("(?i)(argocd|argo-cd|kubernetes|k8s-api|talos|proxmox|grafana|harbor|umami|minio|loki)", v.hostname))
    ])
    error_message = "管理平面または管理用サービスと思われるホスト名が public_services に含まれています。これらを認証なしで公開することは、この構成の設計方針に反します。"
  }

  # 転送先をクラスタ内 Gateway に限定する。
  #
  # スキームだけを検査していると、たとえば http://127.0.0.1:2000 を指定して
  # cloudflared 自身のメトリクスエンドポイントを匿名公開できてしまう。
  # 匿名公開では「どこへでも転送できる」ことがそのまま事故になる。
  validation {
    condition = alltrue([
      for k, v in var.public_services :
      v.origin_service == "http://cilium-gateway-external.gateway.svc.cluster.local:80"
    ])
    error_message = "public_services の origin_service はクラスタ内の Gateway（http://cilium-gateway-external.gateway.svc.cluster.local:80）のみを指定できます。匿名公開の転送先を自由に選べる状態は危険です。"
  }

  # ワイルドカードと空文字を禁止する。
  #
  # ワイルドカードを許すと published_services の個別ホスト名と
  # 重なりうる。cloudflared は先に一致したルールを使うため、
  # 匿名公開のつもりが Access 必須になる（またはその逆になる）。
  validation {
    condition = alltrue([
      for k, v in var.public_services :
      length(trimspace(v.hostname)) > 0 && !can(regex("[*]", v.hostname))
    ])
    error_message = "public_services の hostname にワイルドカードや空文字は使えません。FQDN を個別に列挙してください。"
  }

  # 2 つの変数で同じホスト名を使わせない。
  # 大文字小文字と前後の空白を正規化してから比較する。
  validation {
    condition = length(setintersection(
      toset([for k, v in var.published_services : lower(trimspace(v.hostname))]),
      toset([for k, v in var.public_services : lower(trimspace(v.hostname))]),
    )) == 0
    error_message = "同じホスト名が published_services と public_services の両方に含まれています。Access が付くのか付かないのかが曖昧になるため、どちらか一方にしてください。"
  }

  # 同一 map 内での重複も禁止する。
  validation {
    condition = length(toset([
      for k, v in var.public_services : lower(trimspace(v.hostname))
    ])) == length(var.public_services)
    error_message = "public_services 内で同じホスト名が複数回指定されています。"
  }
}

variable "cloudflared_ingress_output_path" {
  description = <<-EOT
    cloudflared の ingress 設定（ConfigMap）を書き出すパス。

    Tunnel の config_src = "local" を採用しているため、ingress ルールは
    Kubernetes 側の ConfigMap として ArgoCD が管理する。ただし
    「どのホスト名を公開するか」という情報は Access Application と共通なので、
    この tofu を単一の情報源とし、ConfigMap を生成する形にしている。

    ⚠️ 生成後は Git にコミットすること。tofu apply を忘れると
       Access の設定と cloudflared の ingress がずれる。
  EOT
  type        = string
  default     = "../../kubernetes/infra/cloudflared/generated/ingress-configmap.yaml"
}

variable "credentials_output_path" {
  description = "cloudflared の credentials.json を書き出すパス（.gitignore 済み。SOPS 暗号化してから使う）"
  type        = string
  default     = "../../_out/cloudflared-credentials.json"
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
