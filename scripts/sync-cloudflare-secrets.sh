#!/usr/bin/env bash
# ===========================================================================
# Cloudflare Tunnel の認証情報を SOPS で暗号化して Git 管理下に置く
#
# tofu/20-cloudflare の出力（credentials.json）を Kubernetes Secret の
# 形にして暗号化する。平文がリポジトリに残らないようにする。
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

TOFU_DIR="${REPO_ROOT}/tofu/20-cloudflare"
OUTPUT_FILE="${REPO_ROOT}/kubernetes/infra/cloudflared/credentials.sops.yaml"

readonly C_RED=$'\033[0;31m' C_GREEN=$'\033[0;32m' C_BLUE=$'\033[0;34m' C_RESET=$'\033[0m'
info() { printf '%s[INFO]%s  %s\n' "${C_BLUE}"  "${C_RESET}" "$*"; }
ok()   { printf '%s[OK]%s    %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
die()  { printf '%s[ERROR]%s %s\n' "${C_RED}"   "${C_RESET}" "$*" >&2; exit 1; }

command -v sops >/dev/null || die "sops が見つかりません"
command -v tofu >/dev/null || die "tofu が見つかりません"

[[ -d "${TOFU_DIR}" ]] || die "${TOFU_DIR} が見つかりません"

if grep -q "REPLACE_WITH_YOUR_AGE_PUBLIC_KEY" "${REPO_ROOT}/.sops.yaml"; then
  die ".sops.yaml に age 公開鍵が設定されていません。"
fi

# ---------------------------------------------------------------------------
# tofu の出力から認証情報を取得する
#
# ⚠️ 一時ファイルは mktemp で作り、trap で確実に削除する。
#    途中で失敗しても平文が残らないようにする。
# ---------------------------------------------------------------------------
TMP_FILE="$(mktemp "${TMPDIR:-/tmp}/cloudflared-secret.XXXXXX.yaml")"
chmod 600 "${TMP_FILE}"
# shellcheck disable=SC2317 # Invoked indirectly by trap.
cleanup() { rm -f "${TMP_FILE}"; }
trap cleanup EXIT INT TERM

info "tofu の出力から Tunnel の認証情報を取得しています..."

CREDENTIALS_PATH="$(cd "${TOFU_DIR}" && tofu output -raw credentials_file_path 2>/dev/null || echo "")"
[[ -n "${CREDENTIALS_PATH}" ]] \
  || die "tofu output を取得できませんでした。${TOFU_DIR} で apply を実行してください。"

# tofu の出力は tofu ディレクトリからの相対パス
if [[ "${CREDENTIALS_PATH}" != /* ]]; then
  CREDENTIALS_PATH="${TOFU_DIR}/${CREDENTIALS_PATH}"
fi

[[ -f "${CREDENTIALS_PATH}" ]] || die "credentials.json が見つかりません: ${CREDENTIALS_PATH}"

CREDENTIALS_JSON="$(cat "${CREDENTIALS_PATH}")"

# 中身が期待した形か検証する（空ファイルや壊れた JSON を配ってしまわないように）
printf '%s' "${CREDENTIALS_JSON}" | grep -q '"TunnelSecret"' \
  || die "credentials.json の形式が想定と異なります。"

ok "認証情報を取得しました"

# ---------------------------------------------------------------------------
# Secret マニフェストを作って暗号化する
# ---------------------------------------------------------------------------
info "Secret を生成して暗号化しています..."

cat > "${TMP_FILE}" <<EOF
# このファイルは scripts/sync-cloudflare-secrets.sh によって生成されました。
# 編集する場合は 'sops kubernetes/infra/cloudflared/credentials.sops.yaml' を使ってください。
apiVersion: v1
kind: Secret
metadata:
  name: cloudflared-credentials
  namespace: cloudflared
type: Opaque
stringData:
  credentials.json: |
$(printf '%s' "${CREDENTIALS_JSON}" | sed 's/^/    /')
EOF

# ---------------------------------------------------------------------------
# ⚠️ 直接リダイレクトしない（atomic に置き換える）
#
# `sops ... > "${OUTPUT_FILE}"` はシェルが先に出力先を truncate するため、
# sops が失敗すると既存の暗号化済み Secret が空ファイルとして破壊される。
# 一時ファイルへ書いて検証してから mv する。
# ---------------------------------------------------------------------------
ENC_TMP="$(mktemp "${TMPDIR:-/tmp}/cloudflared-secret-enc.XXXXXX.yaml")"
chmod 600 "${ENC_TMP}"
cleanup() { rm -f "${TMP_FILE}" "${ENC_TMP}"; }
trap cleanup EXIT INT TERM

if ! sops --encrypt --config "${REPO_ROOT}/.sops.yaml" "${TMP_FILE}" > "${ENC_TMP}"; then
  die "sops による暗号化に失敗しました。既存の ${OUTPUT_FILE} は変更していません。"
fi

# 平文が漏れていないことを検証する（保険）
TUNNEL_SECRET="$(printf '%s' "${CREDENTIALS_JSON}" \
  | sed -n 's/.*"TunnelSecret"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
if [[ -n "${TUNNEL_SECRET}" ]] && grep -qF "${TUNNEL_SECRET}" "${ENC_TMP}"; then
  die "暗号化に失敗しています（平文が出力に含まれています）。
     既存の ${OUTPUT_FILE} は変更していません。"
fi

mv "${ENC_TMP}" "${OUTPUT_FILE}"
chmod 600 "${OUTPUT_FILE}"

ok "暗号化された Secret を書き出しました: ${OUTPUT_FILE#"${REPO_ROOT}/"}"

# ---------------------------------------------------------------------------
# Service Token の案内
# ---------------------------------------------------------------------------
CLIENT_ID="$(cd "${TOFU_DIR}" && tofu output -raw service_token_client_id 2>/dev/null || echo "")"
EXPIRES_AT="$(cd "${TOFU_DIR}" && tofu output -raw service_token_expires_at 2>/dev/null || echo "unknown")"

cat <<EOF

┌──────────────────────────────────────────────────────────────────┐
│ Cloudflare の認証情報を Git 管理下に配置しました。                │
└──────────────────────────────────────────────────────────────────┘

  コミット:
    git add ${OUTPUT_FILE#"${REPO_ROOT}/"} \\
            kubernetes/infra/cloudflared/generated/ingress-configmap.yaml
    git commit -m "feat(cloudflared): Tunnel の認証情報と ingress 設定を更新"
    git push

  ⚠️ ingress ルールを変更した場合は、ArgoCD の同期後に cloudflared を
     再起動してください。cloudflared は config.yaml のホットリロードに
     対応していないため、ConfigMap を更新しただけでは反映されません。

    kubectl -n cloudflared rollout restart deployment/cloudflared
    kubectl -n cloudflared rollout status deployment/cloudflared

  ---------------------------------------------------------------------
  Workers 側の設定（SaaS から Access を通すために必要）
  ---------------------------------------------------------------------
    Service Token client_id : ${CLIENT_ID}
    有効期限                : ${EXPIRES_AT}

    cd workers/example-origin-api
    wrangler secret put CF_ACCESS_CLIENT_ID
    # ↑ 上記の client_id を貼り付ける

    wrangler secret put CF_ACCESS_CLIENT_SECRET
    # ↑ 以下のコマンドで取得した値を貼り付ける:
    #     cd ${TOFU_DIR#"${REPO_ROOT}/"} && tofu output -raw service_token_client_secret

  ⚠️ client_secret を wrangler.toml の [vars] に書かないこと。
     [vars] は平文でダッシュボードに表示されます。

EOF
