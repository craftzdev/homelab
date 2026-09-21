"""Deployment completion is observed from Kubernetes, not inferred from a merge."""
import base64
from datetime import datetime, timezone
import yaml


class Rollouts:
    def __init__(self, kube, github, *, now=lambda: datetime.now(timezone.utc)):
        self.kube, self.github, self.now = kube, github, now

    def reconcile(self, release, target):
        proof = dict(release['evidence'])
        now = self.now()
        proof.setdefault('deployment_started_at', now.isoformat())
        api = lambda path: self.github('GET', '/repos/' + target['repository'] + path)
        def content(path, ref):
            item = api('/contents/' + path + '?ref=' + ref)
            if item.get('type') != 'file' or item.get('encoding') != 'base64':
                raise ValueError('deployment source unavailable')
            return base64.b64decode(item['content']).decode()
        def pending(reason, *, failed=False):
            elapsed = (now - datetime.fromisoformat(proof['deployment_started_at'])).total_seconds()
            proof['deployment'] = {'ready': False, 'reason': reason, 'resources': []}
            return ('DEPLOYMENT_FAILED' if failed or elapsed > target.get('rollout_timeout_seconds', 1800) else 'DEPLOYING'), proof
        head = api('/git/ref/heads/' + target['branch'])['object']['sha']
        compare = api('/compare/' + proof['merge_sha'] + '...' + head)
        if compare.get('status') not in {'identical', 'ahead'}:
            return pending('Git target no longer contains the approved merge', failed=True)
        mapping = release['source'].get('source_mapping')
        path = mapping['path'] if mapping else release['source']['path']
        current = content(path, head)
        if mapping:
            current = yaml.safe_load(current)['data']['AGENTS.md']
        if current != release['content']:
            return pending('configuration was superseded in Git', failed=True)
        if not mapping:
            runs = api('/actions/workflows/container.yaml/runs?event=push&head_sha=' + proof['merge_sha'] + '&per_page=100')['workflow_runs']
            runs = [r for r in runs if r['head_sha'] == proof['merge_sha'] and r.get('path') == '.github/workflows/container.yaml']
            if not runs:
                return pending('waiting for the signed image pipeline')
            run = max(runs, key=lambda r: r['id'])
            proof['deployment_workflow_url'] = run['html_url']
            if run['status'] != 'completed':
                return pending('signed image pipeline is running')
            if run['conclusion'] != 'success':
                return pending('signed image pipeline failed', failed=True)
        resources = []
        for item in target['deployments']:
            desired = yaml.safe_load(content(item['path'], head))
            live = self.kube('GET', '/apis/apps/v1/namespaces/' + item['namespace'] + '/deployments/' + item['name'])
            def images(deployment):
                spec = deployment['spec']['template']['spec']
                return {kind + ':' + c['name']: c['image'] for kind in ('containers','initContainers') for c in spec.get(kind, [])}
            expected, actual = images(desired), images(live)
            wanted_annotations = desired['spec']['template']['metadata'].get('annotations', {})
            live_annotations = live['spec']['template']['metadata'].get('annotations', {})
            key, value = ('ai-business/config-release', release['id']) if mapping else ('ai-business/source-revision', proof['merge_sha'])
            if wanted_annotations.get(key) != value or live_annotations.get(key) != value:
                return pending('waiting for the approved revision in ' + item['name'])
            count = desired['spec'].get('replicas', 1)
            status = live.get('status', {})
            ready = (count > 0 and expected == actual and all('@sha256:' in image for image in expected.values())
                     and live['spec'].get('replicas', 1) == count
                     and status.get('observedGeneration') == live['metadata']['generation']
                     and status.get('updatedReplicas', 0) == status.get('availableReplicas', 0) == status.get('replicas', 0) == count)
            if not ready:
                failed = any(c.get('type') == 'Progressing' and c.get('status') == 'False' for c in status.get('conditions', []))
                return pending('waiting for healthy replicas in ' + item['name'], failed=failed)
            resources.append({'namespace': item['namespace'], 'name': item['name'], 'generation': live['metadata']['generation'], 'images': actual, 'replicas': count})
        if not resources:
            return pending('no deployment targets configured', failed=True)
        proof['deployment'] = {'ready': True, 'resources': resources, 'git_revision': head, 'observed_at': now.isoformat()}
        return 'DEPLOYED', proof
