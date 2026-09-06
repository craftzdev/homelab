#!/usr/bin/env bash
# ===========================================================================
# Cilium を導入してクラスタを Ready にする
#
# tofu apply の直後、クラスタは以下の状態にある:
#   - Talos と etcd は正常
#   - kube-apiserver は応答する
#   - しかし CNI が無いため全ノードが NotReady
#
# ここで Cilium を helm install し、ノードを Ready にする。
# 以降の管理は ArgoCD が引き継ぐ（同じ values.yaml を参照するため
# 引き継ぎ時に差分は出ない）。
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

KUBECONFIG_PATH="${KUBECONFIG:-${REPO_ROOT}/_out/kubeconfig}"
CILIUM_VERSION="${CILIUM_VERSION:-1.20.1}"
VALUES_FILE="${REPO_ROOT}/kubernetes/infra/cilium/values.yaml"
GATEWAY_API_DIR="${REPO_ROOT}/kubernetes/infra/gateway-api"

readonly C_RED=$'\033[0;31m' C_GREEN=$'\033[0;32m' C_BLUE=$'\033[0;34m' C_RESET=$'\033[0m'
info() { printf '%s[INFO]%s  %s\n' "${C_BLUE}"  "${C_RESET}" "$*"; }
ok()   { printf '%s[OK]%s    %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
die()  { printf '%s[ERROR]%s %s\n' "${C_RED}"   "${C_RESET}" "$*" >&2; exit 1; }

command -v helm    >/dev/null || die "helm が見つかりません（brew install helm）"
command -v kubectl >/dev/null || die "kubectl が見つかりません"

[[ -f "${KUBECONFIG_PATH}" ]] || die "kubeconfig が見つかりません: ${KUBECONFIG_PATH}
     先に tofu/10-proxmox-talos で apply を実行してください。"
[[ -f "${VALUES_FILE}" ]] || die "values ファイルが見つかりません: ${VALUES_FILE}"
[[ -f "${GATEWAY_API_DIR}/kustomization.yaml" ]] \
  || die "Gateway API kustomization が見つかりません: ${GATEWAY_API_DIR}"

export KUBECONFIG="${KUBECONFIG_PATH}"

info "クラスタへの接続を確認しています..."
kubectl version -o json >/dev/null 2>&1 \
  || die "kube-apiserver へ接続できません。VIP (172.16.40.10) への到達性を確認してください。"
ok "接続を確認しました"

# ---------------------------------------------------------------------------
# Gateway API CRD
#
# Cilium operator は起動時に利用可能な API を検出する。Gateway API の CRD が
# 無い状態で gatewayAPI.enabled=true の Cilium を起動すると、GatewayClass が
# Pending のままになるため、CRD の登録完了を Cilium より先に保証する。
# ---------------------------------------------------------------------------
info "Gateway API CRD を導入しています..."
kubectl apply -k "${GATEWAY_API_DIR}"

mapfile -t gateway_api_crds < <(
  kubectl get customresourcedefinitions -o name \
    | grep '\.gateway\.networking\.k8s\.io$'
)
(( ${#gateway_api_crds[@]} > 0 )) \
  || die "Gateway API CRD を検出できませんでした"
kubectl wait --for=condition=Established --timeout=5m "${gateway_api_crds[@]}"
ok "Gateway API CRD が利用可能です"

# ---------------------------------------------------------------------------
# Cilium の導入
#
# --set ではなく values ファイルを使う理由:
#   ArgoCD も同じファイルを参照するため、bootstrap と GitOps 管理の間で
#   設定が食い違わない。--set で上書きすると、ArgoCD が引き継いだ瞬間に
#   設定が巻き戻る。
# ---------------------------------------------------------------------------
if kubectl -n kube-system get daemonset cilium >/dev/null 2>&1; then
  # 初回bootstrap後はArgo CDが同じvaluesでCiliumを所有する。ここでHelmを
  # 再実行すると、Argoがserver-side applyしたcluster-scoped resourceの
  # ownership metadataと衝突するため、既存releaseはGitOpsへ委ねる。
  info "既存のCiliumを検出しました。Helm bootstrapをスキップします..."
  kubectl -n kube-system rollout status daemonset/cilium --timeout=10m
  kubectl -n kube-system rollout status deployment/cilium-operator --timeout=10m

  # 旧順序で構築されたクラスタは、CRD が後から追加されても operator が
  # Gateway API discovery を再実行しない。未受理の場合だけ安全に再起動する。
  if [[ "$(kubectl get gatewayclass cilium \
      -o jsonpath='{.status.conditions[?(@.type=="Accepted")].status}' \
      2>/dev/null || true)" != "True" ]]; then
    info "GatewayClass が未受理のため Cilium operator を再起動しています..."
    kubectl -n kube-system rollout restart deployment/cilium-operator
    kubectl -n kube-system rollout status deployment/cilium-operator --timeout=10m
  fi
  ok "既存のCiliumが利用可能です"
else
  info "Cilium ${CILIUM_VERSION} を導入しています..."
  helm repo add cilium https://helm.cilium.io/ >/dev/null 2>&1 || true
  helm repo update cilium >/dev/null

  helm upgrade --install cilium cilium/cilium \
    --version "${CILIUM_VERSION}" \
    --namespace kube-system \
    --values "${VALUES_FILE}" \
    --wait --timeout 10m

  ok "Cilium を導入しました"
fi

info "Cilium GatewayClass の受理を待機しています..."
gateway_class_accepted=false
for _ in $(seq 1 60); do
  if [[ "$(kubectl get gatewayclass cilium \
      -o jsonpath='{.status.conditions[?(@.type=="Accepted")].status}' \
      2>/dev/null || true)" == "True" ]]; then
    gateway_class_accepted=true
    break
  fi
  sleep 5
done
[[ "${gateway_class_accepted}" == true ]] \
  || die "GatewayClass cilium が5分以内に Accepted=True になりませんでした"
ok "Cilium GatewayClass が受理されました"

# ---------------------------------------------------------------------------
# LoadBalancer IP プールと L2 広告ポリシー
#
# Cilium の CRD が登録された後でないと適用できないため、
# helm install の完了を待ってから適用する。
# ---------------------------------------------------------------------------
info "LoadBalancer IP プールを設定しています..."
kubectl apply -f "${REPO_ROOT}/kubernetes/infra/cilium/lb-ipam.yaml"
ok "IP プールを設定しました"

# ---------------------------------------------------------------------------
# ノードが Ready になるまで待つ
# ---------------------------------------------------------------------------
info "全ノードが Ready になるまで待機しています（最大 5 分）..."
if kubectl wait --for=condition=Ready nodes --all --timeout=300s; then
  ok "全ノードが Ready になりました"
else
  die "ノードが Ready になりませんでした。
     確認: kubectl get nodes
           kubectl -n kube-system get pods -l k8s-app=cilium
           kubectl -n kube-system logs -l k8s-app=cilium --tail=50"
fi

kubectl get nodes -o wide

cat <<'EOF'

┌──────────────────────────────────────────────────────────────────┐
│ Cilium の導入が完了し、クラスタが利用可能になりました。            │
└──────────────────────────────────────────────────────────────────┘

  動作確認（任意、cilium CLI が必要）:
    cilium status --wait
    cilium connectivity test        # 数分かかる

  次の手順:
    ./scripts/bootstrap-argocd.sh        # GitOps 基盤を導入

EOF
