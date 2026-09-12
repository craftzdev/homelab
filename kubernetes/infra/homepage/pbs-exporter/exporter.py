"""Read-only PBS host/snapshot telemetry and Proxmox backup-job telemetry."""
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socketserver
import os
import ssl
import threading
import time
import urllib.parse
import urllib.request

BASE = 'https://172.16.10.11:8006/api2/json'
NODES = ('sv-proxmox-01', 'sv-proxmox-02', 'sv-proxmox-03')
STORAGE = 'pbs-gateway'
CONTEXT = ssl.create_default_context(cafile='/etc/pve-ca/pve-root-ca.pem')
# The existing PVE CA lacks keyUsage. Python 3.13 enables stricter legacy-CA
# rejection by default; retain chain/hostname verification against this pinned
# CA, with the same compatibility behavior as existing Python 3.12 clients.
CONTEXT.verify_flags &= ~ssl.VERIFY_X509_STRICT
AUTH = 'PVEAPIToken=' + os.environ['PVE_TOKEN_ID'] + '=' + os.environ['PVE_TOKEN_SECRET']
PBS_BASE = 'https://pbs.home.arpa:8007/api2/json'
DATASTORE = 'gateway-backup'
PBS_CONTEXT = ssl.create_default_context(cafile='/etc/pbs-ca/pbs-ca.pem')
PBS_AUTH = 'PBSAPIToken=' + os.environ['PBS_TOKEN_ID'] + ':' + os.environ['PBS_TOKEN_SECRET']
snapshot = b'pbs_observer_collection_success 0\n'
lock = threading.Lock()


def api(path, pbs=False):
    req = urllib.request.Request((PBS_BASE if pbs else BASE) + path,
                                 headers={'Authorization': PBS_AUTH if pbs else AUTH})
    with urllib.request.urlopen(req, context=PBS_CONTEXT if pbs else CONTEXT, timeout=12) as response:
        return json.load(response)['data']


def collect():
    lines = []

    def metric(name, value, **labels):
        suffix = '{' + ','.join(k + '=' + json.dumps(str(v)) for k, v in labels.items()) + '}' if labels else ''
        lines.append(f'pbs_observer_{name}{suffix} {float(value)}')

    queries = {'storage': f'/nodes/{NODES[0]}/storage/{STORAGE}/status', 'jobs': '/cluster/backup'}
    queries.update({node: f'/nodes/{node}/tasks?typefilter=vzdump&limit=100' for node in NODES})
    queries.update({'pbs_host': '/nodes/localhost/status',
                    'pbs_datastore': f'/admin/datastore/{DATASTORE}/status',
                    'pbs_snapshots': f'/admin/datastore/{DATASTORE}/snapshots',
                    'pbs_gc': f'/admin/datastore/{DATASTORE}/gc',
                    'pbs_verify_jobs': '/config/verify'})
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {key: executor.submit(api, path, key.startswith('pbs_')) for key, path in queries.items()}
        for key, future in futures.items():
            try:
                results[key] = future.result()
            except Exception:
                # Never include credentials, API bodies, task logs or request objects.
                pass

    outcomes = {}

    def parse(key, handler):
        """Emit one source's metrics. A fetch error or an unexpected payload shape
        drops that source only: its partial lines are removed and it reports 0.
        Without this, one missing field would raise out of collect() and replace
        the whole snapshot with a bare failure, losing the other sources and the
        collection timestamp."""
        first = len(lines)
        try:
            handler(results[key])
        except Exception:
            del lines[first:]
            outcomes[key] = 0
        else:
            outcomes[key] = 1

    def storage_metrics(data):
        metric('storage_active', data.get('active', 0), storage=STORAGE)
        for field in ('total', 'used', 'avail'):
            metric('storage_' + field + '_bytes', data[field], storage=STORAGE)

    def job_metrics(jobs):
        for job in jobs:
            if job.get('storage') != STORAGE:
                continue
            labels = {'job_id': job['id'], 'storage': STORAGE}
            metric('schedule_enabled', job.get('enabled', 0), **labels)
            if job.get('next-run'):
                metric('schedule_next_run_timestamp_seconds', job['next-run'], **labels)
            vmids = [v for v in str(job.get('vmid', '')).replace(',', ' ').split() if v.isdigit()]
            metric('schedule_vm_count', len(vmids), **labels)

    def task_metrics(node, tasks):
        metric('backup_tasks_running', sum(1 for task in tasks if not task.get('endtime')), node=node)
        finished = [task for task in tasks if task.get('endtime')]
        if finished:
            task = max(finished, key=lambda item: item['starttime'])
            # These are node-level vzdump outcomes, not datastore snapshot verification.
            metric('latest_backup_task_success', task.get('status') == 'OK', node=node)
            metric('latest_backup_task_end_timestamp_seconds', task['endtime'], node=node)
            metric('latest_backup_task_duration_seconds', task['endtime'] - task['starttime'], node=node)
        recent = [task for task in finished if task['endtime'] >= time.time() - 86400]
        metric('backup_tasks_failed_24h', sum(task.get('status') != 'OK' for task in recent), node=node)

    def host_metrics(host):
        for field, name in [('cpu', 'cpu_ratio'), ('wait', 'iowait_ratio'), ('uptime', 'uptime_seconds')]:
            metric('host_' + name, host[field])
        for field in ('used', 'total'):
            metric('host_memory_' + field + '_bytes', host['memory'][field])
        for period, value in zip(('1m', '5m', '15m'), host['loadavg']):
            metric('host_load', value, period=period)

    def datastore_metrics(datastore):
        for field in ('total', 'used', 'avail'):
            metric('datastore_' + field + '_bytes', datastore[field], datastore=DATASTORE)

    def snapshot_metrics(snapshots):
        # This endpoint lists the root namespace. No namespaced coverage is implied.
        counts = dict(ok=0, failed=0, unverified=0)
        groups = {}
        for item in snapshots:
            verification = (item.get('verification') or {}).get('state')
            counts[verification if verification in ('ok', 'failed') else 'unverified'] += 1
            group = (item['backup-type'], item['backup-id'])
            groups[group] = max(groups.get(group, 0), item['backup-time'])
        metric('snapshots', len(snapshots), datastore=DATASTORE)
        metric('backup_groups', len(groups), datastore=DATASTORE)
        for state, count in counts.items():
            metric('snapshot_verification_count', count, datastore=DATASTORE, state=state)
        for (backup_type, backup_id), timestamp in groups.items():
            metric('latest_snapshot_timestamp_seconds', timestamp, datastore=DATASTORE,
                   backup_type=backup_type, backup_id=backup_id)

    def verify_metrics(jobs):
        jobs = [job for job in jobs if job.get('store') == DATASTORE]
        metric('verify_schedules_enabled', sum(bool(job.get('schedule')) and not job.get('disable', False)
                                             for job in jobs), datastore=DATASTORE)

    def gc_metrics(gc):
        if gc.get('last-run-state') is not None:
            metric('gc_last_success', gc['last-run-state'] == 'OK', datastore=DATASTORE)
        for field, name in [('last-run-endtime', 'last_end_timestamp_seconds'),
                            ('next-run', 'next_run_timestamp_seconds'),
                            ('pending-bytes', 'pending_bytes'), ('still-bad', 'bad_chunks')]:
            if gc.get(field) is not None:
                metric('gc_' + name, gc[field], datastore=DATASTORE)

    parse('storage', storage_metrics)
    parse('jobs', job_metrics)
    for node in NODES:
        parse(node, lambda tasks, node=node: task_metrics(node, tasks))
    parse('pbs_host', host_metrics)
    parse('pbs_datastore', datastore_metrics)
    parse('pbs_snapshots', snapshot_metrics)
    parse('pbs_verify_jobs', verify_metrics)
    parse('pbs_gc', gc_metrics)

    # Emitted as one block so each series appears exactly once per scrape.
    for key in queries:
        metric('source_success', outcomes.get(key, 0), source=key)
    metric('collection_success', all(outcomes.get(key) for key in queries))
    metric('collection_timestamp_seconds', time.time())
    return ('\n'.join(lines) + '\n').encode()


def refresh():
    global snapshot
    while True:
        started = time.monotonic()
        try:
            payload = collect()
        except Exception:
            payload = b'pbs_observer_collection_success 0\n'
        with lock:
            snapshot = payload
        time.sleep(max(1, 60 - (time.monotonic() - started)))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/metrics', '/healthz'):
            self.send_error(404)
            return
        with lock:
            body = snapshot if self.path == '/metrics' else b'ok\n'
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class Server(ThreadingHTTPServer):
    # http.server resolves the bind address with getfqdn() between bind() and
    # listen(). This pod has no DNS egress on purpose, so whenever /etc/hosts
    # cannot answer, that lookup blocks until it times out: the port is bound
    # but never listening, probes get "connection refused", and the liveness
    # probe kills the container before it can serve. The hostname is only used
    # for error pages, so skip the lookup.
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = 'pbs-observer'
        self.server_port = self.server_address[1]


if __name__ == '__main__':
    threading.Thread(target=refresh, daemon=True).start()
    Server(('0.0.0.0', 9099), Handler).serve_forever()
