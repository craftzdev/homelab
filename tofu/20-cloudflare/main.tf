provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

# ===========================================================================
# Cloudflare Tunnel
# ===========================================================================

# ---------------------------------------------------------------------------
# トンネルのシークレット
#
# 32 バイト以上を base64 でエンコードしたものが要求される。
# これは「このトンネルを張る権利」そのものであり、漏洩したら
# 第三者が同じトンネル名で接続を確立できる。
# ---------------------------------------------------------------------------
resource "random_bytes" "tunnel_secret" {
  length = 32

  # 意図しない再生成（＝トンネルの作り直し）を防ぐ。
  # ローテーションしたい場合は `tofu taint` で明示的に行う。
  lifecycle {
    ignore_changes = [length]
  }
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "this" {
  account_id    = var.cloudflare_account_id
  name          = var.tunnel_name
  tunnel_secret = random_bytes.tunnel_secret.base64

  # ---------------------------------------------------------------------------
  # config_src = "local" を選ぶ理由
  #
  # ingress ルール（ホスト名 → クラスタ内 Service のマッピング）は
  # Kubernetes 側の関心事である。これを Cloudflare 側（"cloudflare"）に
  # 置くと、アプリを 1 つ増やす変更が tofu と kubernetes の 2 箇所に
  # 分散し、ArgoCD の管理対象からも外れる。
  #
  # "local" にすることで ingress は ConfigMap として GitOps 管理下に入る。
  # 詳細は docs/adr/0005-cloudflare-zero-trust.md を参照。
  # ---------------------------------------------------------------------------
  config_src = "local"
}

# ---------------------------------------------------------------------------
# 公開ホスト名の DNS レコード
#
# トンネルの CNAME を指すため、自宅のグローバル IP は DNS 上に一切現れない。
# proxied = true により Cloudflare のプロキシを経由し、
# WAF / DDoS 保護 / Access の認可が前段で効く。
#
# ⚠️ proxied = true のとき ttl は 1（automatic）でなければならない。
# ---------------------------------------------------------------------------
resource "cloudflare_dns_record" "published" {
  for_each = var.published_services

  zone_id = var.cloudflare_zone_id
  name    = each.value.hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.this.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "Managed by OpenTofu (tofu/20-cloudflare) — homelab tunnel"
}

# ===========================================================================
# Cloudflare Access
# ===========================================================================

# ---------------------------------------------------------------------------
# Service Token — SaaS（Cloudflare Workers）用の非人間 ID
#
# Workers はこの client_id / client_secret をヘッダに付けてリクエストする。
# 人間のログインを伴わないマシン間通信のための仕組みであり、
# 自前で認証機構を実装するより遥かに安全である。
# ---------------------------------------------------------------------------
resource "cloudflare_zero_trust_access_service_token" "saas_worker" {
  account_id = var.cloudflare_account_id
  name       = var.service_token_name
  duration   = var.service_token_duration
  enabled    = true

  # この値をインクリメントするとシークレットがローテーションされる
  client_secret_version = var.service_token_secret_version
}

# ---------------------------------------------------------------------------
# Access ポリシー
#
# decision = "non_identity" は「IdP でのユーザー認証を要求しない」ポリシー。
# Service Token による認可のみで通す。include に指定した Service Token を
# 持つリクエストだけが通過できる。
#
# include は OR 条件である。ここに他の条件を足すと「どれか 1 つ満たせば通る」
# ことになるため、認可を緩めたくない限り Service Token 以外を追加しないこと。
# ---------------------------------------------------------------------------
resource "cloudflare_zero_trust_access_policy" "allow_saas_worker" {
  account_id = var.cloudflare_account_id
  name       = "allow-${var.service_token_name}"
  decision   = "non_identity"

  include = [{
    service_token = {
      token_id = cloudflare_zero_trust_access_service_token.saas_worker.id
    }
  }]
}

# ---------------------------------------------------------------------------
# Access アプリケーション（公開ホスト名ごと）
# ---------------------------------------------------------------------------
resource "cloudflare_zero_trust_access_application" "published" {
  for_each = var.published_services

  account_id = var.cloudflare_account_id
  name       = "homelab-${each.key}"
  type       = "self_hosted"

  domain = each.value.path != null ? "${each.value.hostname}${each.value.path}" : each.value.hostname

  session_duration = each.value.session_duration

  # -------------------------------------------------------------------------
  # 認可失敗時に 401 を返す（IdP へリダイレクトしない）
  #
  # これは API 用途では必須の設定である。既定では認可に失敗すると
  # ログイン画面へ 302 リダイレクトされるため、Workers 側の fetch は
  # 「HTML のログインページを 200 で受け取る」という分かりにくい挙動になる。
  # -------------------------------------------------------------------------
  service_auth_401_redirect = true

  # 人間のログインを想定しないため、IdP 選択画面もアプリランチャーも不要
  app_launcher_visible      = false
  auto_redirect_to_identity = false

  # HTTP Only Cookie / Same-Site 属性を強制する
  http_only_cookie_attribute = true
  same_site_cookie_attribute = "strict"

  policies = [{
    id         = cloudflare_zero_trust_access_policy.allow_saas_worker.id
    precedence = 1
  }]
}

# ===========================================================================
# 生成物
# ===========================================================================

# ---------------------------------------------------------------------------
# cloudflared の credentials.json
#
# config_src = "local" のトンネルは、この認証情報ファイルで接続する。
#
# ⚠️ 平文の機密ファイルである。_out/ 配下（.gitignore 済み）に出力し、
#    scripts/sync-cloudflare-secrets.sh が SOPS で暗号化して
#    kubernetes/infra/cloudflared/ に配置する。
# ---------------------------------------------------------------------------
resource "local_sensitive_file" "cloudflared_credentials" {
  filename        = var.credentials_output_path
  file_permission = "0600"

  content = jsonencode({
    AccountTag   = var.cloudflare_account_id
    TunnelID     = cloudflare_zero_trust_tunnel_cloudflared.this.id
    TunnelName   = var.tunnel_name
    TunnelSecret = random_bytes.tunnel_secret.base64
  })
}

# ---------------------------------------------------------------------------
# cloudflared の ingress 設定（ConfigMap）
#
# Access Application と同じ var.published_services から生成することで、
# 「Access では守っているが cloudflared のルートが無い」あるいはその逆、
# といった設定のずれが起きないようにする。
#
# originRequest.access による JWT 再検証を全ホスト名で有効化している点が
# 重要（多層防御）。詳細は docs/40-external-access.md §5 を参照。
# ---------------------------------------------------------------------------
locals {
  cloudflared_config = {
    # メトリクスは Prometheus が取得する
    "metrics" = "0.0.0.0:2000"
    # ログレベル。debug にすると URL やヘッダが記録されるため info を既定とする
    "loglevel" = "info"
    # エッジとの接続に QUIC を使う（HTTP/2 より再接続が速い）
    "protocol" = "quic"
    # Pod がローリング更新される際に、既存接続を待つ猶予
    "grace-period" = "30s"

    "ingress" = concat(
      [
        for key, svc in var.published_services : {
          hostname = svc.hostname
          service  = svc.origin_service
          originRequest = {
            # -------------------------------------------------------------
            # Cloudflare Access の JWT を cloudflared 自身が検証する。
            #
            # これは多層防御である。Access アプリケーションの設定を誤って
            # 削除・変更してしまった場合でも、有効な JWT を持たない
            # リクエストは Origin へ到達する前にここで落ちる。
            # -------------------------------------------------------------
            access = {
              required = true
              teamName = var.cloudflare_team_name
              audTag   = [cloudflare_zero_trust_access_application.published[key].aud]
            }
            connectTimeout         = "10s"
            noTLSVerify            = false
            disableChunkedEncoding = false
          }
        }
      ],
      # 明示的なルールに一致しなかったリクエストは 404 を返す。
      # cloudflared は最後に catch-all ルールを必須とする。
      # ここを http_status:404 にしておくことで、意図しないホスト名が
      # クラスタ内へ転送されることを防ぐ。
      [{ service = "http_status:404" }]
    )
  }
}

resource "local_file" "cloudflared_ingress_configmap" {
  filename        = var.cloudflared_ingress_output_path
  file_permission = "0644"

  content = yamlencode({
    apiVersion = "v1"
    kind       = "ConfigMap"
    metadata = {
      name      = "cloudflared-config"
      namespace = "cloudflared"
      labels = {
        "app.kubernetes.io/name"       = "cloudflared"
        "app.kubernetes.io/managed-by" = "opentofu"
      }
      annotations = {
        "homelab/generated-by" = "tofu/20-cloudflare — 手で編集しないこと。変更は published_services を編集して tofu apply する。"
      }
    }
    data = {
      "config.yaml" = yamlencode(local.cloudflared_config)
    }
  })
}
