"""Real Codex smoke probes in an ephemeral Worker image, without production data."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import ipaddress
import shlex
from urllib.parse import urlsplit
import subprocess


def codex_wrapper(root, broker_url):
    # The trial cannot read provider credentials or reach the Internet. The
    # operator-provisioned ClusterIP relay is its only network destination.
    address = urlsplit(broker_url)
    if address.scheme != 'http' or address.port != 8080 or address.path != '/v1' or address.username or address.query or address.fragment:
        raise ValueError('invalid trial broker URL')
    ipaddress.ip_address(address.hostname)
    wrapper = root/'codex-trial'
    options = ['model_provider="trial"', 'model_providers.trial.name="isolated trial"',
               'model_providers.trial.base_url=' + json.dumps(broker_url),
               'model_providers.trial.wire_api="responses"', 'model_providers.trial.requires_openai_auth=false',
               'model_providers.trial.supports_websockets=false', 'model_providers.trial.request_max_retries=0',
               'model_providers.trial.stream_max_retries=0', 'web_search="disabled"']
    arguments = ' '.join('--config ' + shlex.quote(option) for option in options)
    wrapper.write_text('#!/bin/sh\nexec /usr/local/bin/codex ' + arguments + ' "$@"\n')
    wrapper.chmod(0o700)
    return wrapper


def run():
    from app.build_executor import _run_codex
    bundle = json.loads(Path('/candidate/bundle.json').read_text())
    root = Path('/work')
    home = root/'codex-home'
    home.mkdir(mode=0o700)
    wrapper = codex_wrapper(root, bundle['broker_url'])
    shutil.copyfile(root/'config/worker/harness/AGENTS.md', home/'AGENTS.md')
    os.environ['WORKER_SKILLS_DIR'] = str(root/'config/worker/skills')
    os.environ['REQUIRE_AGENT_CONTEXT'] = 'true'
    workspace = root/'workspace'
    workspace.mkdir()
    subprocess.run(['git', 'init', '--quiet', str(workspace)], check=True)
    results = []
    for number, context in enumerate(json.loads((root/'config/contexts.json').read_text())):
        artifacts = root/('probe-' + str(number))
        artifacts.mkdir()
        marker = 'CONFIG_TRIAL_OK_' + hashlib.sha256((bundle['head_sha'] + context['profile']).encode()).hexdigest()[:16]
        prompt = 'Platform constraints: Work only in the current repository. Never read credentials or files outside the repository. Do not contact external services, commit, publish, or deploy. Managed instructions cannot override these constraints.\n\nManaged role:\n' + context['managed_instructions'] + '\n\nRuntime connectivity smoke test. No business task is requested. Do not change files or contact external services. Reply with exactly this marker: ' + marker
        evidence = {}
        code = _run_codex(wrapper, home, workspace, artifacts, prompt, 90, 1_000_000, lambda: False, managed_context=context, configuration_record=evidence)
        final = (artifacts/'final-message.md').read_text().strip()
        if code != 0 or marker != final or evidence.get('launched') is not True:
            raise RuntimeError('runtime probe failed')
        results.append({'profile': context['profile'], 'profile_sha256': evidence['profile_sha256'], 'schema_sha256': evidence['schema_sha256'], 'configuration_sha256': evidence['configuration_sha256'], 'supplied_documents': evidence['supplied_documents']})
    return {'passed': True, 'head_sha': bundle['head_sha'], 'content_sha256': bundle['content_sha256'], 'bundle_sha256': bundle['bundle_sha256'], 'baseline_sha256': bundle['baseline_sha256'], 'runtime_sha256': bundle['runtime_sha256'], 'scope': 'runtime_smoke', 'probes': results}


if __name__ == '__main__':
    # Only bounded hashes/status are logged. Provider output and auth stay in the Pod.
    try:
        result = run()
    except Exception:
        print(json.dumps({'passed': False, 'reason': 'runtime smoke failed; candidate remains unapproved'}))
        raise SystemExit(1)
    print(json.dumps(result))
