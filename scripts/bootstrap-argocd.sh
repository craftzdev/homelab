#!/usr/bin/env bash
# ===========================================================================
# ArgoCD を導入し、GitOps 管理を開始する
#
# このスクリプトが「手作業で行う最後の操作」である。
# これ以降、クラスタの状態は Git が唯一の情報源になる。
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

KUBECONFIG_PATH="${KUBECONFIG:-${REPO_ROOT}/_out/kubeconfig}"
# argo-cd chart 10.4.2 = ArgoCD v3.5.2
ARGOCD_CHART_VERSION="${ARGOCD_CHART_VERSION:-10.4.2}"
AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-${HOME}/.config/sops/age/keys.txt}"
VALUES_FILE="${REPO_ROOT}/kubernetes/bootstrap/argocd/values.yaml"
GITOPS_REVISION="${GITOPS_REVISION:-main}"

readonly C_RED=$'\033[0;31m' C_GREEN=$'\033[0;32m' C_YELLOW=$'\033[0;33m'
readonly C_BLUE=$'\033[0;34m' C_RESET=$'\033[0m'
info() { printf '%s[INFO]%s  %s\n' "${C_BLUE}"   "${C_RESET}" "$*"; }
ok()   { printf '%s[OK]%s    %s\n' "${C_GREEN}"  "${C_RESET}" "$*"; }
warn() { printf '%s[WARN]%s  %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die()  { printf '%s[ERROR]%s %s\n' "${C_RED}"    "${C_RESET}" "$*" >&2; exit 1; }

command -v helm    >/dev/null || die "helm が見つかりません"
command -v kubectl >/dev/null || die "kubectl が見つかりません"

[[ -f "${KUBECONFIG_PATH}" ]] || die "kubeconfig が見つかりません: ${KUBECONFIG_PATH}"
[[ -f "${VALUES_FILE}" ]]     || die "values ファイルが見つかりません: ${VALUES_FILE}"
[[ -f "${AGE_KEY_FILE}" ]]    || die "age 秘密鍵が見つかりません: ${AGE_KEY_FILE}
     age-keygen -o ${AGE_KEY_FILE} で作成してください。"

export KUBECONFIG="${KUBECONFIG_PATH}"

kubectl version -o json >/dev/null 2>&1 || die "クラスタへ接続できません"

# ---------------------------------------------------------------------------
# namespace
# ---------------------------------------------------------------------------
info "argocd namespace を作成しています..."
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -

# ArgoCD 自体は非特権で動作するが、repo-server が initContainer で
# バイナリをコピーするため baseline とする
kubectl label namespace argocd \
  pod-security.kubernetes.io/enforce=baseline \
  pod-security.kubernetes.io/audit=restricted \
  pod-security.kubernetes.io/warn=restricted \
  --overwrite
ok "namespace を準備しました"

# ---------------------------------------------------------------------------
# age 秘密鍵を Secret として投入する
#
# ⚠️ この Secret を読める者は、リポジトリ内の全ての暗号化済み秘密を
#    復号できる。argocd namespace への RBAC を厳格に管理すること。
# ---------------------------------------------------------------------------
info "age 秘密鍵を Secret として投入しています..."
kubectl create secret generic sops-age \
  --namespace argocd \
  --from-file=keys.txt="${AGE_KEY_FILE}" \
  --dry-run=client -o yaml | kubectl apply -f -
ok "sops-age Secret を作成しました"

# ---------------------------------------------------------------------------
# ArgoCD の導入
# ---------------------------------------------------------------------------
info "ArgoCD (chart ${ARGOCD_CHART_VERSION}) を導入しています..."
helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
helm repo update argo >/dev/null

helm upgrade --install argocd argo/argo-cd \
  --version "${ARGOCD_CHART_VERSION}" \
  --namespace argocd \
  --values "${VALUES_FILE}" \
  --wait --timeout 10m

ok "ArgoCD を導入しました"

# ---------------------------------------------------------------------------
# KSOPS の動作確認
#
# ここで失敗すると、暗号化された Secret を含む Application が
# 全て同期エラーになる。原因が分かりにくいため、先に検証する。
# ---------------------------------------------------------------------------
info "repo-server で ksops が使えるか確認しています..."
if kubectl -n argocd exec deploy/argocd-repo-server -c repo-server -- \
     sh -c 'command -v ksops >/dev/null && test -f "$SOPS_AGE_KEY_FILE"' 2>/dev/null; then
  ok "ksops と age 秘密鍵を確認しました"
else
  warn "repo-server で ksops または age 秘密鍵を確認できませんでした"
  warn "暗号化された Secret を含む Application が同期に失敗する可能性があります。"
  warn "確認: kubectl -n argocd logs deploy/argocd-repo-server -c repo-server"
fi

# ---------------------------------------------------------------------------
# Project と root Application
# ---------------------------------------------------------------------------
info "AppProject を作成しています..."
kubectl apply -f "${REPO_ROOT}/kubernetes/apps/project.yaml"

# 旧構成の Application は app-of-apps の対象外にしただけでは feature branch
# 検証時に残り続ける。finalizer による配下リソースの削除まで待ち、未設定の
# Velero と Gateway VM に移行済みの cloudflared を確実に退役させる。
for retired_app in cloudflared velero; do
  kubectl -n argocd delete application "${retired_app}" \
    --ignore-not-found --wait=true --timeout=5m
done

if [[ "${GITOPS_REVISION}" == "main" ]]; then
  info "root Application を作成しています（app-of-apps / revision: main）..."
  kubectl apply -f "${REPO_ROOT}/kubernetes/apps/root.yaml"
  ok "GitOps 管理を開始しました"
else
  # root Application はリポジトリ上の Application 定義を継続的に同期する。
  # feature branch の検証時に root を作ると、子 Application にハードコード
  # された main が即座に適用され、試験対象と実際の構成が食い違う。
  # そこで非 main のときはローカルの Application 定義だけを一時レンダリングし、
  # homelab Git source の revision をまとめて差し替える。main へマージ後に
  # GITOPS_REVISION=main で再実行すれば、通常の app-of-apps 管理へ移行できる。
  [[ "${GITOPS_REVISION}" =~ ^[A-Za-z0-9._/-]+$ ]] \
    || die "GITOPS_REVISION に使用できない文字が含まれています"

  rendered_apps="$(mktemp)"
  trap 'rm -f "${rendered_apps:-}"' EXIT
  sed "s|targetRevision: main|targetRevision: ${GITOPS_REVISION}|g" \
    "${REPO_ROOT}/kubernetes/apps/infrastructure.yaml" >"${rendered_apps}"

  info "feature revision ${GITOPS_REVISION} の Application を適用しています..."
  kubectl apply -f "${rendered_apps}"
  ok "feature revision の GitOps Application を登録しました（root は未作成）"
  warn "main へマージ後、GITOPS_REVISION=main で再実行して root 管理へ切り替えてください。"
fi

# ---------------------------------------------------------------------------
# 初期パスワードの案内
#
# ⚠️ パスワードそのものは **表示しない**。
#
#    スクリプトの標準出力は CI のログ、ターミナルの履歴、画面共有、
#    セッション録画に残る。管理者パスワードがそれらに残ることは
#    「一度きりの初期パスワードだから」では正当化できない。
#
#    代わりに、必要なときに利用者自身が取得するコマンドを案内する。
# ---------------------------------------------------------------------------
if kubectl -n argocd get secret argocd-initial-admin-secret >/dev/null 2>&1; then
  PASSWORD_HINT="取得コマンド（必要なときに実行してください）:
    kubectl -n argocd get secret argocd-initial-admin-secret \\
      -o jsonpath='{.data.password}' | base64 -d; echo"
else
  PASSWORD_HINT="初期パスワードの Secret は存在しません（削除済み）。"
fi

cat <<EOF

┌──────────────────────────────────────────────────────────────────┐
│ ArgoCD の導入が完了しました。                                     │
└──────────────────────────────────────────────────────────────────┘

  UI へのアクセス（⚠️ Ingress は作っていません。外部公開しないため）:
    kubectl -n argocd port-forward svc/argocd-server 8080:80
    → http://localhost:8080

  ログイン:
    ユーザー名: admin
    ${PASSWORD_HINT}

  ⚠️ 【必ず実施】初期パスワードを変更し、Secret を削除してください:

    argocd login localhost:8080 --username admin --insecure
    argocd account update-password
    kubectl -n argocd delete secret argocd-initial-admin-secret

EOF

cat <<'EOF'
  同期状況の確認:
    kubectl -n argocd get applications
    watch kubectl -n argocd get applications

EOF
