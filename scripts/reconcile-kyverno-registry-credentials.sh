#!/usr/bin/env bash
# ===========================================================================
# Kyverno が Harbor の署名を読むための資格情報を用意する
#
# ---------------------------------------------------------------------------
# なぜ既存の Secret をそのまま使えないのか
# ---------------------------------------------------------------------------
# 既存の harbor-pull はアプリの namespace にあり、Kyverno からは読めない。
# 新しいロボットは作らず、同じ読み取り専用ロボットの資格情報を kyverno
# namespace へ複製する。値はパイプで渡し、プロセスの引数には載せない。
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
KUBECONFIG_PATH="${KUBECONFIG:-${REPO_ROOT}/_out/kubeconfig}"
# Kyverno が署名を取りに行く先。ClusterPolicy はイメージ参照と同じ
# エンドポイントを使う（policies/verify-harbor-images.yaml）。資格情報は
# レジストリのホスト名で選ばれるため、ここが一致していないと匿名アクセスに
# なり、署名を読めない。
HARBOR_SIGNATURE_HOST="${HARBOR_SIGNATURE_HOST:-172.16.40.201:5000}"

info() { printf '  %s\n' "$*"; }
ok() { printf 'OK  %s\n' "$*"; }
die() { printf 'NG  %s\n' "$*" >&2; exit 1; }

for tool in kubectl jq; do
  command -v "${tool}" >/dev/null || die "${tool} がありません"
done
[[ -s "${KUBECONFIG_PATH}" ]] || die "kubeconfig がありません: ${KUBECONFIG_PATH}"
export KUBECONFIG="${KUBECONFIG_PATH}"

kubectl create namespace kyverno --dry-run=client -o yaml \
  | kubectl apply -f - >/dev/null

# 既存の読み取り専用ロボットを、署名取得先のホスト名向けに複製する。
copy_pull_secret() {
  local source_namespace="$1" target_name="$2"

  kubectl -n "${source_namespace}" get secret harbor-pull >/dev/null 2>&1 \
    || die "${source_namespace}/harbor-pull がありません。先に該当の bootstrap を実行してください"

  kubectl -n "${source_namespace}" get secret harbor-pull \
    -o jsonpath='{.data.\.dockerconfigjson}' \
    | base64 --decode \
    | jq --arg host "${HARBOR_SIGNATURE_HOST}" --arg name "${target_name}" '
        (.auths | to_entries | map(select(.value.auth != null)) | first) as $entry
        | if $entry == null then
            error("harbor-pull に auth がありません")
          else
            {
              apiVersion: "v1",
              kind: "Secret",
              type: "kubernetes.io/dockerconfigjson",
              metadata: {name: $name, namespace: "kyverno"},
              stringData: {
                ".dockerconfigjson": ({auths: {($host): {auth: $entry.value.auth}}} | tojson)
              }
            }
          end' \
    | kubectl apply -f - >/dev/null

  info "${target_name} <- ${source_namespace}/harbor-pull（${HARBOR_SIGNATURE_HOST} 向け）"
}

copy_pull_secret ai-agent harbor-pull-ai-business
copy_pull_secret moshitoku harbor-pull-moshitoku

ok "Kyverno の署名読み取り用資格情報を配置しました"
