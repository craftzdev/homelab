# shellcheck shell=bash
# ===========================================================================
# Secret を argv に載せずに適用するための共通関数
#
# ---------------------------------------------------------------------------
# なぜ必要か
# ---------------------------------------------------------------------------
# `kubectl create secret generic --from-literal=key=value` は、値をそのまま
# kubectl の引数として渡す。引数は同じホストの他プロセスから `ps` で読める。
# 管理者の Mac 1 台という前提では影響は限定的だが、値を出さない運用を
# 徹底するほうが事故を起こしにくい。
#
# シェル関数の引数はプロセスの argv にはならない（bash の内部にとどまる）。
# 一方、jq の `--arg` や python の argv に渡すと、そのプロセスの argv に
# 載ってしまう。そこで値は標準入力（NUL 区切り）でだけ渡す。
#
# ---------------------------------------------------------------------------
# 使い方
# ---------------------------------------------------------------------------
#   source "${SCRIPT_DIR}/lib/secrets.sh"
#
#   # Opaque
#   apply_secret ns name "" "key1=${value1}" "key2=${value2}"
#
#   # 型を指定する（basic-auth など）
#   apply_secret ns name kubernetes.io/basic-auth "username=x" "password=${p}"
#
#   # kubectl へ追加の引数が要る場合は KUBECTL_SECRET_ARGS で渡す
#   KUBECTL_SECRET_ARGS=(--kubeconfig "${KUBECONFIG_PATH}")
#
# 値に改行やバイナリでない任意の文字列を入れてよい（JSON として組み立てる）。
# ===========================================================================

apply_secret() {
  local namespace="$1" name="$2" secret_type="${3:-}"
  shift 3
  local kubectl_args=()
  if [[ -n "${KUBECTL_SECRET_ARGS+x}" ]]; then
    kubectl_args=("${KUBECTL_SECRET_ARGS[@]}")
  fi

  local kv
  {
    for kv in "$@"; do
      printf '%s\0' "${kv}"
    done
  } | python3 -c '
import json
import sys

namespace, name, secret_type = sys.argv[1:4]
data = {}
for item in sys.stdin.buffer.read().split(b"\0"):
    if not item:
        continue
    key, _, value = item.decode().partition("=")
    data[key] = value

secret = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"name": name, "namespace": namespace},
    "stringData": data,
}
if secret_type:
    secret["type"] = secret_type
json.dump(secret, sys.stdout)
' "${namespace}" "${name}" "${secret_type}" \
    | kubectl "${kubectl_args[@]}" apply -f - >/dev/null
}
