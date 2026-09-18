#!/usr/bin/env bash
# ===========================================================================
# Kyverno が Harbor の署名を読むための資格情報を用意する
#
# ---------------------------------------------------------------------------
# なぜ既存の Secret をそのまま使えないのか
# ---------------------------------------------------------------------------
# アプリの namespace にある harbor-pull は、レジストリのホスト名が
# 172.16.40.201:5000 と harbor.tailb6c7d.ts.net になっている。Kyverno は
# 署名をクラスタ内の Harbor（harbor.harbor.svc）から取るため、同じロボットの
# 資格情報を「そのホスト名で」持った Secret が要る。
#
# 新しいロボットは作らない。既存の読み取り専用ロボットを、宛先ホスト名だけ
# 変えて複製する。値はパイプで渡し、プロセスの引数には載せない。
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
KUBECONFIG_PATH="${KUBECONFIG:-${REPO_ROOT}/_out/kubeconfig}"
HARBOR_IN_CLUSTER_HOST="${HARBOR_IN_CLUSTER_HOST:-harbor.harbor.svc}"

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

# 既存の読み取り専用ロボットを、クラスタ内ホスト名向けに複製する。
copy_pull_secret() {
  local source_namespace="$1" target_name="$2"

  kubectl -n "${source_namespace}" get secret harbor-pull >/dev/null 2>&1 \
    || die "${source_namespace}/harbor-pull がありません。先に該当の bootstrap を実行してください"

  kubectl -n "${source_namespace}" get secret harbor-pull \
    -o jsonpath='{.data.\.dockerconfigjson}' \
    | base64 --decode \
    | jq --arg host "${HARBOR_IN_CLUSTER_HOST}" --arg name "${target_name}" '
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

  info "${target_name} <- ${source_namespace}/harbor-pull（${HARBOR_IN_CLUSTER_HOST} 向け）"
}

copy_pull_secret ai-agent harbor-pull-ai-business
copy_pull_secret moshitoku harbor-pull-moshitoku

ok "Kyverno の署名読み取り用資格情報を配置しました"
