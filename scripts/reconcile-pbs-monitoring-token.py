#!/usr/bin/env python3
"""Reconcile the PBS Audit token, Keychain copy and portal Kubernetes Secret."""
import http.client
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SERVICE = 'dev.craftz.homelab.grafana-pbs-token'
IDENTITY = 'grafana@pbs!monitor'
# Named so a failure says which step broke. The messages themselves stay free of
# command output, so the stage name is the only diagnostic the operator gets.
STAGE = 'startup'


def stage(name):
    global STAGE
    STAGE = name


def run(args, timeout=60, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kwargs)
    if result.returncode:
        # Exceptions must never include command arguments or secret-bearing output.
        raise RuntimeError(f'{args[0]} failed (exit {result.returncode})')
    return result.stdout


def remote(command):
    return run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                'root@172.16.10.51', command])


def main():
    stage('the PBS user lookup over SSH')
    users = json.loads(remote('proxmox-backup-manager user list --output-format json'))
    if not any(user['userid'] == 'grafana@pbs' for user in users):
        remote("proxmox-backup-manager user create grafana@pbs --comment 'Grafana read-only monitoring'")
    stage('the Keychain lookup')
    stored = subprocess.run(['security', 'find-generic-password', '-s', SERVICE,
                             '-a', IDENTITY, '-w'], capture_output=True, text=True, timeout=15)
    if stored.returncode == 0:
        token = stored.stdout.strip()
    elif stored.returncode == 44:  # Keychain item not found; do not rotate existing tokens.
        stage('the PBS token creation')
        tokens = json.loads(remote('proxmox-backup-manager user list-tokens grafana@pbs --output-format json'))
        if any(item.get('tokenid') == IDENTITY or item.get('token-name') == 'monitor' for item in tokens):
            raise RuntimeError('PBS token already exists, but its Keychain copy is missing; restore the saved secret.')
        created = json.loads(remote('proxmox-backup-debug api create /access/users/grafana@pbs/token/monitor '
                                    '--comment "Grafana read-only monitoring" --output-format json'))
        token = created['value']
        run(['security', 'add-generic-password', '-U', '-s', SERVICE, '-a', IDENTITY, '-w', token])
    else:
        raise RuntimeError('Could not read the PBS token from Keychain.')
    stage('the PBS ACL update')
    for identity in ('grafana@pbs', IDENTITY):
        for path, role in (('/system', 'Audit'), ('/datastore/gateway-backup', 'DatastoreAudit')):
            remote(f"proxmox-backup-manager acl update {path} {role} --auth-id '{identity}' --propagate true")

    stage('the read-only PBS API verification')
    context = ssl.create_default_context(cafile=ROOT / 'kubernetes/infra/homepage/pbs-exporter/pbs-ca.pem')
    paths = ['/nodes/localhost/status', '/admin/datastore/gateway-backup/status',
             '/admin/datastore/gateway-backup/snapshots', '/admin/datastore/gateway-backup/gc', '/config/verify']
    for path in paths:
        connection = http.client.HTTPSConnection('pbs.home.arpa', 8007, context=context, timeout=15)
        try:
            connection.sock = context.wrap_socket(socket.create_connection(('172.16.10.51', 8007), timeout=15),
                                                  server_hostname='pbs.home.arpa')
            connection.request('GET', '/api2/json' + path, headers={'Authorization': 'PBSAPIToken=' + IDENTITY + ':' + token})
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError(f'PBS read-only API check failed (HTTP {response.status}).')
            # Not an assert: python3 -O would drop the check entirely.
            if 'data' not in json.loads(response.read()):
                raise RuntimeError('PBS returned an unexpected response shape.')
        finally:
            connection.close()
    stage('the Kubernetes Secret apply')
    secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'pbs-observer-credentials', 'namespace': 'portal'},
              'type': 'Opaque', 'stringData': {'PBS_TOKEN_ID': IDENTITY, 'PBS_TOKEN_SECRET': token}}
    run(['kubectl', '--kubeconfig', str(ROOT / '_out/kubeconfig'), '--request-timeout=120s',
         'apply', '-f', '-'], input=json.dumps(secret), timeout=180)
    print('PBS Audit token verified; Keychain and portal/pbs-observer-credentials are ready.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(f'PBS monitoring reconciliation failed during {STAGE}. Secrets were not printed.\n'
              'Re-running is safe: an existing token is reused, never rotated.', file=sys.stderr)
        sys.exit(1)
