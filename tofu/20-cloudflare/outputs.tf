output "tunnel_id" {
  description = "Cloudflare Tunnel の UUID"
  value       = cloudflare_zero_trust_tunnel_cloudflared.this.id
}

output "tunnel_cname" {
  description = "公開ホスト名が指す CNAME のターゲット"
  value       = "${cloudflare_zero_trust_tunnel_cloudflared.this.id}.cfargotunnel.com"
}

output "public_hostnames" {
  description = "匿名公開しているホスト名（Access なし）"
  value       = { for k, v in var.public_services : k => v.hostname }
}

output "published_hostnames" {
  description = "外部へ公開されているホスト名の一覧"
  value       = { for k, v in var.published_services : k => v.hostname }
}

output "access_application_auds" {
  description = "Access アプリケーションの aud タグ（cloudflared の JWT 検証に使用）"
  value       = { for k, v in cloudflare_zero_trust_access_application.published : k => v.aud }
}

output "service_token_client_id" {
  description = <<-EOT
    Workers に設定する CF-Access-Client-Id の値。
    これ単体では認可を通せないため sensitive にはしていないが、
    公開の場に貼らないこと。
  EOT
  value       = cloudflare_zero_trust_access_service_token.saas_worker.client_id
}

output "service_token_client_secret" {
  description = <<-EOT
    Workers に設定する CF-Access-Client-Secret の値。

    取得方法:
      tofu output -raw service_token_client_secret

    Workers への登録（平文で wrangler.toml に書かないこと）:
      wrangler secret put CF_ACCESS_CLIENT_SECRET
  EOT
  value       = cloudflare_zero_trust_access_service_token.saas_worker.client_secret
  sensitive   = true
}

output "service_token_expires_at" {
  description = "Service Token の有効期限。期限前にローテーションすること。"
  value       = cloudflare_zero_trust_access_service_token.saas_worker.expires_at
}

# ---------------------------------------------------------------------------
# Gatus 用 Service Token
#
# 値は Keychain へ保存し、そこから
# scripts/bootstrap-cluster-secrets.sh が Kubernetes Secret を作る。
# Git にも tfvars にも平文で置かない。
# ---------------------------------------------------------------------------
output "gatus_service_token_client_id" {
  description = "Gatus に設定する CF-Access-Client-Id の値"
  value       = cloudflare_zero_trust_access_service_token.gatus_monitor.client_id
}

output "gatus_service_token_client_secret" {
  description = <<-EOT
    Gatus に設定する CF-Access-Client-Secret の値。

    Keychain へ保存する。security はパスワードを標準入力から読まないため、
    パイプではなく引数で渡すこと（パイプすると対話プロンプトに落ちて失敗する）:

      s="$(tofu output -raw gatus_service_token_client_secret)"
      security add-generic-password -U \
        -s dev.craftz.homelab.gatus-cloudflare-access-client-secret \
        -a gatus -w "$s"
      unset s
  EOT
  value       = cloudflare_zero_trust_access_service_token.gatus_monitor.client_secret
  sensitive   = true
}

output "gatus_service_token_expires_at" {
  description = "Gatus 用 Service Token の有効期限。期限前にローテーションすること。"
  value       = cloudflare_zero_trust_access_service_token.gatus_monitor.expires_at
}

output "credentials_file_path" {
  description = "cloudflared の credentials.json を書き出したパス（機密）"
  value       = local_sensitive_file.cloudflared_credentials.filename
}

output "next_steps" {
  description = "この後に実行すべきこと"
  value       = <<-EOT

    ┌──────────────────────────────────────────────────────────────────┐
    │ Cloudflare 側のリソース作成が完了しました。                       │
    └──────────────────────────────────────────────────────────────────┘

    1) cloudflared の Secret を SOPS で暗号化して Git へ入れる
         ../../scripts/sync-cloudflare-secrets.sh

    2) 生成された ingress ConfigMap をコミットする
         git add ${var.cloudflared_ingress_output_path}
         git commit -m "chore(cloudflared): ingress ルールを更新"

    3) Workers 側に Service Token を登録する
         cd ../../workers/example-origin-api
         wrangler secret put CF_ACCESS_CLIENT_ID
         wrangler secret put CF_ACCESS_CLIENT_SECRET
         # 値は以下で取得:
         #   tofu output -raw service_token_client_id
         #   tofu output -raw service_token_client_secret

    4) 疎通確認

    %{if length(var.published_services) > 0~}
       Access が効いていることの確認（published_services）
         # トークン無し → 401 が返ること（200 が返ったら設定ミス）
         curl -s -o /dev/null -w '%%{http_code}\n' https://${values(var.published_services)[0].hostname}/
    %{endif~}
    %{if length(var.public_services) > 0~}
       匿名公開の確認（public_services）
         # 認証なしで到達できること。Access は意図的に付いていない。
         curl -s -o /dev/null -w '%%{http_code}\n' https://${values(var.public_services)[0].hostname}/
    %{endif~}

    詳細は docs/40-external-access.md を参照してください。
  EOT
}
