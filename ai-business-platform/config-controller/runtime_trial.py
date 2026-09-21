"""Deterministic, isolated trial Jobs. Kubernetes credentials never enter a trial."""
import base64
import hashlib
import json
from pathlib import Path
import urllib.error
from urllib.parse import quote
import yaml


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Trials:
    def __init__(self, kube, gateway, github, settings):
        self.kube, self.gateway, self.github, self.settings = kube, gateway, github, settings
        self.ns = settings['namespace']
        self.api = '/apis/batch/v1/namespaces/' + self.ns + '/jobs'
        self.core = '/api/v1/namespaces/' + self.ns

    def reconcile(self, release, proof, target):
        from controller import Blocked
        inventory = self.gateway('GET', '/v1/config/inventory')
        baseline = sha(json.dumps({doc['id']: doc['sha256'] for doc in inventory['documents']}, sort_keys=True))
        runtime_version = sha(json.dumps(self.settings, sort_keys=True))
        prior = proof.get('runtime_trial', {})
        if prior.get('passed') is True:
            if (prior.get('head_sha') != proof['head_sha'] or prior.get('content_sha256') != release['content_sha256']
                    or prior.get('baseline_sha256') != baseline or prior.get('runtime_sha256') != runtime_version):
                raise Blocked('tested baseline changed; create a new release')
            return prior
        name = 'config-' + release['id'][:8] + '-' + proof['head_sha'][:16]
        try:
            job = self.kube('GET', self.api + '/' + name)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            docs = [dict(doc) for doc in inventory['documents']]
            source = next((d for d in docs if d['id'] == release['source']['id']), None)
            if source is None or source['sha256'] != sha(release['base_content']):
                raise Blocked('installed trial baseline changed; create a new release')
            if source['kind'] not in {'profile', 'schema', 'capabilities', 'skill', 'harness'}:
                raise Blocked('this file type has no runtime trial coverage')
            for doc in docs:
                if doc['component'] == 'agent' and doc.get('loaded_sha256') != doc['sha256']:
                    raise Blocked('Agent file and loaded baseline differ')
                # The tested component must exactly match the PR's base. Testing
                # installed files from an older, un-deployed main is not evidence.
                if doc['component'] == target['component']:
                    path = doc.get('source_mapping', {}).get('path', doc['path'])
                    item = self.github('GET', '/repos/' + target['repository'] + '/contents/' + path + '?ref=' + proof['base_sha'])
                    if item.get('type') != 'file' or item.get('encoding') != 'base64':
                        raise Blocked('trial baseline is not a regular Git file')
                    content = base64.b64decode(item['content']).decode()
                    if doc['kind'] == 'harness':
                        content = yaml.safe_load(content)['data']['AGENTS.md']
                    if sha(content) != doc['sha256']:
                        raise Blocked('Git base and installed trial baseline differ')
            source.update(content=release['content'], sha256=release['content_sha256'])
            bundle = {'source': release['source'], 'documents': docs, 'head_sha': proof['head_sha'], 'content_sha256': release['content_sha256'], 'baseline_sha256': baseline, 'runtime_sha256': runtime_version}
            bundle['bundle_sha256'] = sha(json.dumps(bundle, sort_keys=True))
            if len(json.dumps(bundle).encode()) > 900_000:
                raise Blocked('trial input exceeds the ConfigMap budget')
            cm = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': name, 'labels': {'app': 'config-trial'}}, 'immutable': True,
                  'data': {'bundle.json': json.dumps(bundle), 'prepare.py': Path(__file__).with_name('trial_prepare.py').read_text(), 'run.py': Path(__file__).with_name('trial_run.py').read_text()}}
            try:
                self.kube('POST', self.core + '/configmaps', cm)
            except urllib.error.HTTPError as e:
                if e.code != 409:
                    raise
                existing = self.kube('GET', self.core + '/configmaps/' + name)
                if existing['data'] != cm['data']:
                    raise Blocked('trial input changed after creation')
            job = self.job(name, bundle)
            try:
                job = self.kube('POST', self.api, job)
            except urllib.error.HTTPError as e:
                if e.code != 409:
                    raise
                job = self.kube('GET', self.api + '/' + name)
        annotations = job['metadata'].get('annotations', {})
        if annotations.get('config/head') != proof['head_sha'] or annotations.get('config/content') != release['content_sha256']:
            raise Blocked('trial identity does not match the candidate')
        cm = self.kube('GET', self.core + '/configmaps/' + name)
        owners = [{'apiVersion': 'batch/v1', 'kind': 'Job', 'name': name, 'uid': job['metadata']['uid']}]
        if cm['metadata'].get('ownerReferences') != owners:
            cm['metadata']['ownerReferences'] = owners
            self.kube('PUT', self.core + '/configmaps/' + name, cm)
        status = job.get('status', {})
        if status.get('failed'):
            raise Blocked('candidate runtime trial failed')
        if not status.get('succeeded'):
            return {'passed': False, 'status': 'RUNNING', 'job': name}
        pods = self.kube('GET', self.core + '/pods?labelSelector=' + quote('job-name=' + name, safe=''))['items']
        pods = [p for p in pods if any(o.get('uid') == job['metadata']['uid'] for o in p['metadata'].get('ownerReferences', []))]
        if len(pods) != 1 or pods[0].get('status', {}).get('phase') != 'Succeeded':
            raise Blocked('trial Pod completion cannot be established')
        log = self.kube('GET', self.core + '/pods/' + pods[0]['metadata']['name'] + '/log?container=trial&limitBytes=16384', raw=True)
        try:
            receipt = json.loads(log)
        except ValueError as error:
            raise Blocked('trial receipt is invalid') from error
        if (not isinstance(receipt, dict) or receipt.get('passed') is not True or receipt.get('head_sha') != proof['head_sha'] or receipt.get('content_sha256') != release['content_sha256']
                or receipt.get('bundle_sha256') != annotations.get('config/bundle') or receipt.get('baseline_sha256') != baseline or receipt.get('runtime_sha256') != runtime_version or receipt.get('scope') != 'runtime_smoke' or not receipt.get('probes')):
            raise Blocked('trial receipt does not match the immutable candidate')
        return {**receipt, 'status': 'SUCCEEDED', 'job': name, 'uid': job['metadata']['uid']}

    def job(self, name, bundle):
        security = {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}
        mounts = [{'name': 'candidate', 'mountPath': '/candidate', 'readOnly': True}, {'name': 'work', 'mountPath': '/work'}, {'name': 'tmp', 'mountPath': '/tmp'}]
        annotations = {'config/head': bundle['head_sha'], 'config/content': bundle['content_sha256'], 'config/bundle': bundle['bundle_sha256']}
        def container(name, image, script):
            return {'name': name, 'image': image, 'command': ['python', script], 'securityContext': security, 'volumeMounts': list(mounts),
                    'env': [{'name': 'PYTHONPATH', 'value': '/opt/agent' if name == 'prepare' else '/opt/worker'}, {'name': 'PYTHONDONTWRITEBYTECODE', 'value': '1'}],
                    'resources': {'requests': {'cpu': '100m', 'memory': '256Mi'}, 'limits': {'cpu': '2', 'memory': '2Gi'}}}
        trial = container('trial', self.settings['worker_image'], '/candidate/run.py')
        trial['volumeMounts'].append({'name': 'codex-auth', 'mountPath': '/auth', 'readOnly': True})
        return {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': name, 'labels': {'app': 'config-trial'}, 'annotations': annotations},
                'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 1800, 'ttlSecondsAfterFinished': 604800,
                         'template': {'metadata': {'labels': {'app': 'config-trial'}}, 'spec': {
                             'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                             'nodeSelector': {'homelab.craftz.dev/workload-plane': 'true'},
                             'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001, 'fsGroup': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}},
                             'imagePullSecrets': [{'name': 'harbor-pull'}],
                             'initContainers': [container('prepare', self.settings['agent_image'], '/candidate/prepare.py')], 'containers': [trial],
                             'volumes': [{'name': 'candidate', 'configMap': {'name': name}}, {'name': 'work', 'emptyDir': {'sizeLimit': '2Gi'}}, {'name': 'tmp', 'emptyDir': {'sizeLimit': '256Mi'}}, {'name': 'codex-auth', 'secret': {'secretName': 'config-trial-codex-auth', 'defaultMode': 288}}]}}}}
