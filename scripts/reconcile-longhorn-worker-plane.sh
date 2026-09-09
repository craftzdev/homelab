#!/usr/bin/env bash
set -euo pipefail

# Reconcile Longhorn onto the dedicated worker plane.
#
# Helm owns the desired selectors and default settings. Longhorn creates some
# child Deployments/DaemonSets itself, so this script also reconciles already
# existing generated resources and safely evacuates replicas from the three
# control-plane nodes. It is safe to run repeatedly.

namespace=${LONGHORN_NAMESPACE:-longhorn-system}
control_planes=(k8s-1 k8s-2 k8s-3)
workers=(k8s-worker-1 k8s-worker-2 k8s-worker-3)

for node in "${workers[@]}"; do
  kubectl wait --for=condition=Ready "node/${node}" --timeout=5m
  test "$(kubectl get "node/${node}" -o jsonpath='{.metadata.labels.homelab\.craftz\.dev/workload-plane}')" = true
done

for node in "${control_planes[@]}"; do
  if ! kubectl -n "$namespace" get "nodes.longhorn.io/${node}" >/dev/null 2>&1; then
    printf 'Longhorn node %s is absent; skipping evacuation\n' "$node"
    continue
  fi

  evacuation_patch=$(kubectl -n "$namespace" get "nodes.longhorn.io/${node}" -o json | jq -c '
    {
      spec: {
        allowScheduling: false,
        evictionRequested: true,
        disks: (.spec.disks | with_entries(
          .value.allowScheduling = false |
          .value.evictionRequested = true
        ))
      }
    }
  ')
  kubectl -n "$namespace" patch "nodes.longhorn.io/${node}" --type=merge -p "$evacuation_patch" >/dev/null

  for _ in $(seq 1 120); do
    remaining=$(kubectl -n "$namespace" get replicas.longhorn.io -o json | jq --arg node "$node" '[.items[] | select(.spec.nodeID == $node)] | length')
    unhealthy=$(kubectl -n "$namespace" get volumes.longhorn.io -o json | jq '[.items[] | select(.status.robustness != "healthy")] | length')
    if [[ $remaining == 0 && $unhealthy == 0 ]]; then
      break
    fi
    sleep 5
  done

  remaining=$(kubectl -n "$namespace" get replicas.longhorn.io -o json | jq --arg node "$node" '[.items[] | select(.spec.nodeID == $node)] | length')
  unhealthy=$(kubectl -n "$namespace" get volumes.longhorn.io -o json | jq '[.items[] | select(.status.robustness != "healthy")] | length')
  if [[ $remaining != 0 || $unhealthy != 0 ]]; then
    printf 'Timed out evacuating %s (replicas=%s, unhealthy volumes=%s)\n' "$node" "$remaining" "$unhealthy" >&2
    exit 1
  fi

  settled_patch=$(kubectl -n "$namespace" get "nodes.longhorn.io/${node}" -o json | jq -c '
    {
      spec: {
        allowScheduling: false,
        evictionRequested: false,
        disks: (.spec.disks | with_entries(
          .value.allowScheduling = false |
          .value.evictionRequested = false
        ))
      }
    }
  ')
  kubectl -n "$namespace" patch "nodes.longhorn.io/${node}" --type=merge -p "$settled_patch" >/dev/null
  printf 'Longhorn replicas evacuated from %s\n' "$node"
done

selector_patch='{"spec":{"template":{"spec":{"nodeSelector":{"homelab.craftz.dev/workload-plane":"true"}}}}}'

for name in csi-attacher csi-provisioner csi-resizer csi-snapshotter; do
  if kubectl -n "$namespace" get "deployment/${name}" >/dev/null 2>&1; then
    kubectl -n "$namespace" patch "deployment/${name}" --type=merge -p "$selector_patch" >/dev/null
    kubectl -n "$namespace" rollout status "deployment/${name}" --timeout=10m
  fi
done

generated_daemonsets=$(kubectl -n "$namespace" get daemonsets -o name | grep -E '^daemonset.apps/(longhorn-csi-plugin|engine-image-)' || true)
while IFS= read -r resource; do
  [[ -z $resource ]] && continue
  kubectl -n "$namespace" patch "$resource" --type=merge -p "$selector_patch" >/dev/null
  kubectl -n "$namespace" rollout status "$resource" --timeout=10m
done <<< "$generated_daemonsets"

kubectl -n "$namespace" get volumes.longhorn.io \
  -o custom-columns='NAME:.metadata.name,STATE:.status.state,ROBUSTNESS:.status.robustness,NODE:.status.currentNodeID'
