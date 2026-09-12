#!/usr/bin/env python3
"""Export bounded, non-sensitive Gateway health metrics for node_exporter."""
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request

OUTPUT = Path('/var/lib/prometheus/node-exporter/ai-gateway.prom')
SERVICES = ('public-api', 'callback-api', 'db')


def command(args):
    return subprocess.check_output(args, text=True, timeout=12, stderr=subprocess.DEVNULL)


def collect(lines):
    def metric(name, value, labels=None):
        suffix = '' if not labels else '{' + ','.join(k + '=' + json.dumps(v) for k, v in labels.items()) + '}'
        lines.append(f'ai_gateway_{name}{suffix} {value}')

    try:
        # Explicit fields only: never export container environment or healthcheck output.
        names = command(['docker', 'ps', '-a', '--filter', 'label=com.docker.compose.project=ai-business-gateway', '--format', '{{.Names}}']).splitlines()
        for service in SERVICES:
            name = f'ai-business-gateway-{service}-1'
            labels = {'component': service}
            metric('container_present', int(name in names), labels)
            if name not in names:
                metric('container_running', 0, labels)
                metric('container_healthy', 0, labels)
                continue
            raw = command(['docker', 'inspect', '--format', '{{json .State.Running}} {{.RestartCount}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}', name]).split()
            metric('container_running', int(raw[0] == 'true'), labels)
            metric('container_restart_count', int(raw[1]), labels)
            metric('container_healthy', int(raw[2] == 'healthy'), labels)
        # Docker reports memory with the inactive file cache excluded, as in docker stats.
        selected = [n for n in names if n in {f'ai-business-gateway-{s}-1' for s in SERVICES}]
        stats = command(['docker', 'stats', '--no-stream', '--format', '{{json .}}', *selected]) if selected else ''
        units = {'B': 1, 'kB': 1000, 'MB': 1000**2, 'GB': 1000**3, 'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3}
        for row in stats.splitlines():
            data = json.loads(row)
            service = data['Name'].removeprefix('ai-business-gateway-').removesuffix('-1')
            labels = {'component': service}
            metric('container_cpu_percent', float(data['CPUPerc'].rstrip('%')), labels)
            match = re.fullmatch(r'([\d.]+)(\w+)', data['MemUsage'].split(' / ')[0])
            if match:
                metric('container_memory_bytes', float(match[1]) * units[match[2]], labels)
        metric('docker_collection_success', 1)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        metric('docker_collection_success', 0)

    for service, port in [('public-api', 8080), ('callback-api', 8081)]:
        started = time.monotonic()
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/ready', timeout=3) as response:
                ok = response.status == 200
        except (OSError, http.client.HTTPException):
            ok = False
        metric('ready', int(ok), {'component': service})
        metric('ready_duration_seconds', time.monotonic() - started, {'component': service})

    allowed = ('cloudflared_tunnel_total_requests', 'cloudflared_tunnel_request_errors',
               'cloudflared_tunnel_response_by_code', 'cloudflared_tunnel_ha_connections')
    try:
        with urllib.request.urlopen('http://127.0.0.1:20241/metrics', timeout=3) as response:
            for line in response.read(2_000_000).decode().splitlines():
                if any(line.startswith(name + ' ') or line.startswith(name + '{') for name in allowed):
                    lines.append(line)
        metric('tunnel_collection_success', 1)
    except (OSError, ValueError, http.client.HTTPException):
        metric('tunnel_collection_success', 0)


def write(lines):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix('.tmp')
    temporary.write_text('\n'.join(lines) + '\n')
    temporary.chmod(0o644)
    os.replace(temporary, OUTPUT)


if __name__ == '__main__':
    # The file is rewritten even when collection fails part way through. Leaving
    # the previous file in place would make node_exporter keep serving stale
    # values with no indication that the collector stopped working.
    collected = []
    try:
        collect(collected)
        success = 1
    except Exception:
        # Never include command output, URLs or response bodies here.
        success = 0
    collected.append(f'ai_gateway_collection_success {success}')
    collected.append(f'ai_gateway_collection_timestamp_seconds {time.time()}')
    write(collected)
