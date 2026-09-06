#!/usr/bin/env bash
# Idempotently forward the Tailnet DNS zone from CoreDNS to the Tailscale
# Operator nameserver. The nameserver ClusterIP changes on every rebuild.
set -euo pipefail

TAILNET_ZONE="${TAILNET_ZONE:-tailb6c7d.ts.net}"
KUBECONFIG_PATH="${KUBECONFIG:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/_out/kubeconfig}"
export KUBECONFIG="${KUBECONFIG_PATH}"

nameserver_ip="$(kubectl get dnsconfig ts-dns \
  -o jsonpath='{.status.nameserver.ip}')"
[[ "${nameserver_ip}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || { printf 'Tailscale nameserver is not ready\n' >&2; exit 1; }

current_corefile="$(kubectl -n kube-system get configmap coredns \
  -o jsonpath='{.data.Corefile}')"
base_corefile="$(awk '
  /^# BEGIN HOMELAB TAILSCALE DNS$/ { managed=1; next }
  /^# END HOMELAB TAILSCALE DNS$/ { managed=0; next }
  !managed { print }
' <<<"${current_corefile}")"
managed_block="# BEGIN HOMELAB TAILSCALE DNS
${TAILNET_ZONE}:53 {
    errors
    cache 30
    forward . ${nameserver_ip}
}
# END HOMELAB TAILSCALE DNS"
new_corefile="${managed_block}

${base_corefile}"
patch="$(jq -n --arg corefile "${new_corefile}" '{data:{Corefile:$corefile}}')"
kubectl -n kube-system patch configmap coredns --type=merge -p "${patch}" >/dev/null
kubectl -n kube-system rollout restart deployment/coredns >/dev/null
kubectl -n kube-system rollout status deployment/coredns --timeout=5m
printf 'CoreDNS forwards %s to %s\n' "${TAILNET_ZONE}" "${nameserver_ip}"
