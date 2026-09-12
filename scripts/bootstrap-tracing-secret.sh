#!/usr/bin/env bash
# Materialize only the dedicated Tempo credential; never print its value.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KUBECONFIG_PATH="${KUBECONFIG:-${SCRIPT_DIR}/../_out/kubeconfig}"
service=dev.craftz.homelab.tempo-s3
account=tempo
for tool in security kubectl jq openssl; do
  command -v "${tool}" >/dev/null
done
if ! secret_value="$(security find-generic-password -s "$service" -a "$account" -w 2>/dev/null)"; then
  secret_value="$(openssl rand -base64 32)"
  security add-generic-password -U -s "$service" -a "$account" -w "$secret_value" >/dev/null
fi
[[ -n "$secret_value" ]]
# Feed JSON through stdin so the credential is never a kubectl argument.
printf '%s' "$secret_value" | jq -Rs '{
  apiVersion:"v1",kind:"Secret",
  metadata:{name:"tempo-s3-credentials",namespace:"logging"},
  type:"Opaque",stringData:{AWS_ACCESS_KEY_ID:"tempo",AWS_SECRET_ACCESS_KEY:.}
}' | kubectl --kubeconfig "$KUBECONFIG_PATH" apply -f - >/dev/null
unset secret_value
printf '%s\n' "Tempo S3 credential reconciled"
