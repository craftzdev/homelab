#!/usr/bin/env bash
# Keep PBS retention and physical space reclamation in sync. No VM data is read.
set -euo pipefail
PBS_HOST="${PBS_HOST:-172.16.10.51}"
apply=false
case "${1:-}" in
  '') ;;
  --apply) apply=true ;;
  *) echo "Usage: $0 [--apply]" >&2; exit 2 ;;
esac

ssh -o BatchMode=yes "root@${PBS_HOST}" "python3 - ${apply}" <<'PY'
import datetime
import json
import pathlib
import shutil
import subprocess
import sys

store = 'gateway-backup'
prune_schedule = '04:00'
gc_schedule = '00,06,12,18:15'

def read(*args):
    return json.loads(subprocess.check_output(
        ['proxmox-backup-manager', *args, '--output-format', 'json'], text=True))

jobs = [j for j in read('prune-job', 'list') if j.get('store') == store]
if len(jobs) != 1:
    raise SystemExit('Expected exactly one prune job for gateway-backup; refusing ambiguous update')
job = jobs[0]
print(json.dumps({'current': job, 'desired_keep_daily': 3,
                  'prune_schedule': prune_schedule, 'gc_schedule': gc_schedule}))
if sys.argv[1] != 'true':
    print('Read-only. Pass --apply to configure the schedules.')
    raise SystemExit(0)

backup = pathlib.Path('/root/pbs-capacity-policy-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
backup.mkdir(mode=0o700)
for name in ['datastore.cfg', 'prune.cfg']:
    shutil.copy2(pathlib.Path('/etc/proxmox-backup') / name, backup / name)

args = ['proxmox-backup-manager', 'prune-job', 'update', job['id'],
        '--keep-daily', '3', '--schedule', prune_schedule, '--disable', 'false']
for field in ['keep-last', 'keep-hourly', 'keep-weekly', 'keep-monthly', 'keep-yearly']:
    if field in job:
        args += ['--delete', field]
subprocess.run(args, check=True)
subprocess.run(['proxmox-backup-manager', 'datastore', 'update', store,
                '--gc-schedule', gc_schedule], check=True)

updated = next(j for j in read('prune-job', 'list') if j['id'] == job['id'])
assert updated['keep-daily'] == 3 and updated['schedule'] == prune_schedule
assert not any(k.startswith('keep-') and k != 'keep-daily' for k in updated)
gc = read('garbage-collection', 'status', store)
assert gc['schedule'] == gc_schedule and gc.get('next-run')
print('Verified 3 daily restore points, daily pruning, and six-hourly GC; default GC grace preserved.')
PY
