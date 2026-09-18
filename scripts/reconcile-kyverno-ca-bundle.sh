#!/usr/bin/env bash
# ===========================================================================
# Kyverno が使う CA 束を配置する
#
# ---------------------------------------------------------------------------
# なぜ必要か
# ---------------------------------------------------------------------------
# Kyverno は署名を Harbor（172.16.40.201:5000）から HTTPS で取得する。その
# 証明書は自前 CA で、Kyverno のコンテナはそれを知らない。
#
# chart の caCertificates はコンテナの ca-certificates.crt を **丸ごと
# 置き換える**。自前 CA だけを入れると Sigstore（Fulcio / Rekor / TUF）への
# TLS が検証できなくなるため、「公開 CA ＋ 自前 CA」を束ねた 1 つのファイルを
# 渡す必要がある。ここではその ConfigMap を作る。
#
# 公開 CA はこの Mac のシステム束（/etc/ssl/cert.pem）を使う。Git に 300KB の
# 証明書束を持ち込まずに済み、更新は macOS の更新に従う。
#
# ⚠️ 公開 CA が古くなると Sigstore への接続が失敗し、署名検証が error になる
#    （Audit の間は Pod は作成される）。証明書束を更新したら再実行すること。
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
KUBECONFIG_PATH="${KUBECONFIG:-${REPO_ROOT}/_out/kubeconfig}"
SYSTEM_CA_BUNDLE="${SYSTEM_CA_BUNDLE:-/etc/ssl/cert.pem}"
HARBOR_CA="${REPO_ROOT}/talos/certs/harbor-registry-ca.crt"

info() { printf '  %s\n' "$*"; }
ok() { printf 'OK  %s\n' "$*"; }
die() { printf 'NG  %s\n' "$*" >&2; exit 1; }

command -v kubectl >/dev/null || die "kubectl がありません"
[[ -s "${KUBECONFIG_PATH}" ]] || die "kubeconfig がありません: ${KUBECONFIG_PATH}"
[[ -s "${SYSTEM_CA_BUNDLE}" ]] || die "公開 CA の束がありません: ${SYSTEM_CA_BUNDLE}"
[[ -s "${HARBOR_CA}" ]] || die "Harbor の CA がありません: ${HARBOR_CA}"
export KUBECONFIG="${KUBECONFIG_PATH}"

bundle="$(mktemp)"
trap 'rm -f "${bundle}"' EXIT
cat "${SYSTEM_CA_BUNDLE}" "${HARBOR_CA}" > "${bundle}"

# ⚠️ kubectl apply は元の内容を注釈へ複製するため、300KB の束では
#    注釈の上限（256KB）を超える。create --dry-run + replace で置く。
kubectl create namespace kyverno --dry-run=client -o yaml \
  | kubectl apply -f - >/dev/null
if kubectl -n kyverno get configmap ca-bundle >/dev/null 2>&1; then
  kubectl -n kyverno create configmap ca-bundle \
    --from-file=ca-certificates.crt="${bundle}" \
    --dry-run=client -o yaml \
    | kubectl -n kyverno replace -f - >/dev/null
else
  kubectl -n kyverno create configmap ca-bundle \
    --from-file=ca-certificates.crt="${bundle}" >/dev/null
fi

# subPath でマウントしたファイルは ConfigMap を更新しても差し替わらない。
# 既に動いている Kyverno があれば作り直す（初回はまだ存在しない）。
for deployment in kyverno-admission-controller kyverno-background-controller \
  kyverno-reports-controller kyverno-cleanup-controller; do
  if kubectl -n kyverno get deployment "${deployment}" >/dev/null 2>&1; then
    kubectl -n kyverno rollout restart "deployment/${deployment}" >/dev/null
    kubectl -n kyverno rollout status "deployment/${deployment}" --timeout=180s >/dev/null
    info "${deployment} を再起動しました"
  fi
done

count="$(grep -c 'BEGIN CERTIFICATE' "${bundle}")"
ok "Kyverno の CA 束を配置しました（${count} 証明書 = 公開 CA + Harbor）"
