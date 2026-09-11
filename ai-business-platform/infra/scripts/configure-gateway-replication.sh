#!/usr/bin/env bash
set -euo pipefail

# Run on the Proxmox node currently hosting the Gateway VM. The two schedules
# are deliberately offset so a snapshot timeout cannot make two replication
# jobs contend for the same guest-agent freeze window.
vm_id="${1:-1200}"
rate_mbps="${REPLICATION_RATE_MBPS:-100}"
comment="AI Gateway HA replica"

if ! qm status "${vm_id}" >/dev/null 2>&1; then
  echo "Gateway VM ${vm_id} does not exist on this cluster" >&2
  exit 1
fi

source_node="$(hostname)"
job_file="$(mktemp)"

ensure_job() {
  local job_id="$1"
  local target_node="$2"
  local schedule="$3"
  local current_target

  if pvesh get "/cluster/replication/${job_id}" --output-format json \
    >"${job_file}" 2>/dev/null; then
    current_target="$(python3 -c \
      'import json,sys; print(json.load(open(sys.argv[1]))["target"])' \
      "${job_file}")"
    if [[ "${current_target}" != "${target_node}" ]]; then
      echo "${job_id} targets ${current_target}; refusing to replace it" >&2
      exit 1
    fi
    pvesr update "${job_id}" \
      --schedule "${schedule}" \
      --rate "${rate_mbps}" \
      --comment "${comment}"
  else
    pvesr create-local-job "${job_id}" "${target_node}" \
      --source "${source_node}" \
      --schedule "${schedule}" \
      --rate "${rate_mbps}" \
      --comment "${comment}"
  fi
}

recover_stale_freeze() {
  local freeze_state

  freeze_state="$(qm agent "${vm_id}" fsfreeze-status 2>/dev/null || true)"
  if [[ "${freeze_state}" != "frozen" ]]; then
    return
  fi
  if pgrep -af '[v]zdump|[p]vesr run' >/dev/null; then
    echo "Gateway is frozen by an active backup or replication job; aborting" >&2
    exit 1
  fi
  qm agent "${vm_id}" fsfreeze-thaw >/dev/null
}

trap 'rm -f "${job_file}"' EXIT
recover_stale_freeze
ensure_job "${vm_id}-0" sv-proxmox-02 '*/10'
ensure_job "${vm_id}-1" sv-proxmox-03 '5,15,25,35,45,55'

# A sequential manual sync validates both destinations and guarantees that the
# guest filesystem is thawed before this command reports success.
pvesr run --id "${vm_id}-0" --verbose 1
pvesr run --id "${vm_id}-1" --verbose 1

if [[ "$(qm agent "${vm_id}" fsfreeze-status)" != "thawed" ]]; then
  echo "Gateway guest filesystem did not return to the thawed state" >&2
  exit 1
fi

pvesr status
