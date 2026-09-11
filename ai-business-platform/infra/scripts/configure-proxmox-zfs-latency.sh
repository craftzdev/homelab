#!/usr/bin/env bash
set -euo pipefail

# Keep ZFS transaction groups small enough that local-zfs snapshots finish
# inside Proxmox's short non-worker snapshot timeout, even while Kubernetes CI
# is writing heavily to the same consumer SSD pool. Apply to every HA node so
# the behavior follows the Gateway after a relocation.
dirty_data_max_bytes="${ZFS_DIRTY_DATA_MAX_BYTES:-268435456}"
if (( $# > 0 )); then
  nodes=("$@")
else
  nodes=(172.16.10.11 172.16.10.12 172.16.10.13)
fi

if [[ ! "${dirty_data_max_bytes}" =~ ^[0-9]+$ ]] \
  || (( dirty_data_max_bytes < 134217728 || dirty_data_max_bytes > 1073741824 )); then
  echo "ZFS_DIRTY_DATA_MAX_BYTES must be between 128 MiB and 1 GiB" >&2
  exit 1
fi

for node in "${nodes[@]}"; do
  echo "Configuring ${node}"
  ssh -o BatchMode=yes -o ConnectTimeout=10 "root@${node}" \
    bash -s -- "${dirty_data_max_bytes}" <<'REMOTE'
set -euo pipefail

dirty_data_max_bytes="$1"
parameter=/sys/module/zfs/parameters/zfs_dirty_data_max
config=/etc/modprobe.d/zfs-ai-business.conf
desired="options zfs zfs_dirty_data_max=${dirty_data_max_bytes}"

[[ -w "${parameter}" ]] || {
  echo "ZFS dirty-data parameter is not writable" >&2
  exit 1
}

current_config=""
if [[ -f "${config}" ]]; then
  current_config="$(<"${config}")"
fi
if [[ "${current_config}" != "${desired}" ]]; then
  temp_config="$(mktemp)"
  trap 'rm -f "${temp_config}"' EXIT
  printf '%s\n' "${desired}" >"${temp_config}"
  install -o root -g root -m 0644 "${temp_config}" "${config}"
fi

if [[ "$(<"${parameter}")" != "${dirty_data_max_bytes}" ]]; then
  printf '%s\n' "${dirty_data_max_bytes}" >"${parameter}"
fi

printf 'node=%s zfs_dirty_data_max=%s\n' "$(hostname)" "$(<"${parameter}")"
REMOTE
done
