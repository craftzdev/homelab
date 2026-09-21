"""Real Codex smoke probes in an ephemeral Worker image, without production data."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from app.build_executor import _run_codex


def run():
    bundle = json.loads(Path('/candidate/bundle.json').read_text())
    root = Path('/work')
    home = root/'codex-home'
    home.mkdir(mode=0o700)
    shutil.copyfile('/auth/auth.json', home/'auth.json')
    (home/'auth.json').chmod(0o600)
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
        code = _run_codex(Path('/usr/local/bin/codex'), home, workspace, artifacts, prompt, 90, 1_000_000, lambda: False, managed_context=context, configuration_record=evidence)
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
