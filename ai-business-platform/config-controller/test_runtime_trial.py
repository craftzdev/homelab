import json
import base64
from copy import deepcopy
import urllib.error
import pytest
from controller import Blocked
from runtime_trial import Trials, sha, runtime_digest

SETTINGS={'namespace':'trial','broker_url':'http://10.96.0.10:8080/v1','agent_image':'registry/agent@sha256:'+'a'*64,'worker_image':'registry/worker@sha256:'+'b'*64}
SOURCE={'id':'source','component':'agent','kind':'profile','path':'profiles/example.md','sha256':sha('old'),'content':'old','loaded_sha256':sha('old')}
RELEASE={'id':'12345678-abcd','source':SOURCE,'content':'new','content_sha256':sha('new'),'base_content':'old'}
PROOF={'head_sha':'a'*40,'base_sha':'b'*40}


def test_job_has_ephemeral_storage_and_no_gateway_or_github_credentials():
    trial=Trials(None,None,None,SETTINGS)
    job=trial.job('config-test',{'head_sha':'a'*40,'content_sha256':'b'*64,'bundle_sha256':'c'*64})
    pod=job['spec']['template']['spec']
    assert pod['automountServiceAccountToken'] is False
    assert all('persistentVolumeClaim' not in v and 'hostPath' not in v for v in pod['volumes'])
    assert not any('secret' in v for v in pod['volumes'])
    assert not any(v['mountPath']=='/auth' for c in pod['containers']+pod['initContainers'] for v in c['volumeMounts'])
    assert all(c['securityContext']['readOnlyRootFilesystem'] for c in pod['containers']+pod['initContainers'])
    assert all('GATEWAY' not in v['name'] and 'GITHUB' not in v['name'] for c in pod['containers'] for v in c['env'])
    assert job['spec']['backoffLimit']==0 and job['spec']['activeDeadlineSeconds']==1800


def test_success_is_bound_to_head_content_baseline_and_no_repeated_model_runs():
    baseline=sha(json.dumps({'source':SOURCE['sha256']},sort_keys=True))
    runtime=runtime_digest(SETTINGS)
    receipt={'passed':True,'scope':'runtime_smoke','head_sha':PROOF['head_sha'],'content_sha256':RELEASE['content_sha256'],'baseline_sha256':baseline,'runtime_sha256':runtime,'probes':[{'profile':'example'}]}
    def no_kube(*args,**kwargs):raise AssertionError('a passed immutable trial must not re-run')
    trial=Trials(no_kube,lambda *a:{'documents':[SOURCE]},None,SETTINGS)
    assert trial.reconcile(RELEASE,{**PROOF,'runtime_trial':receipt},{})==receipt
    changed=deepcopy(receipt);changed['head_sha']='other'
    with pytest.raises(Blocked):trial.reconcile(RELEASE,{**PROOF,'runtime_trial':changed},{})
    trial.gateway=lambda *a:{'documents':[{**SOURCE,'sha256':'changed'}]}
    with pytest.raises(Blocked):trial.reconcile(RELEASE,{**PROOF,'runtime_trial':receipt},{})


def test_failed_job_never_becomes_success():
    def kube(method,path,body=None,**kwargs):
        if '/jobs/' in path:return {'metadata':{'name':'job','uid':'uid','annotations':{'config/head':PROOF['head_sha'],'config/content':RELEASE['content_sha256']}},'status':{'failed':1}}
        return {'metadata':{'ownerReferences':[{'apiVersion':'batch/v1','kind':'Job','name':'config-12345678-'+PROOF['head_sha'][:16],'uid':'uid'}]}}
    trial=Trials(kube,lambda *a:{'documents':[SOURCE]},None,SETTINGS)
    with pytest.raises(Blocked,match='failed'):trial.reconcile(RELEASE,PROOF,{})


@pytest.mark.parametrize('source_mapping', [None, {}])
def test_created_job_is_recovered_and_receipt_must_match_candidate(source_mapping):
    stored = {}
    receipt = {}
    writes = []
    def kube(method, path, body=None, **kwargs):
        if path.endswith('/log?container=trial&limitBytes=16384'):
            return json.dumps(receipt)
        if '/pods?' in path:
            return {'items': [{'metadata': {'name': 'pod', 'ownerReferences': [{'uid': 'job-uid'}]}, 'status': {'phase': 'Succeeded'}}]}
        if method == 'POST':
            writes.append(path)
            item = deepcopy(body)
            item['metadata']['uid'] = 'job-uid'
            stored[path + '/' + body['metadata']['name']] = item
            return item
        if method == 'PUT':
            stored[path] = deepcopy(body)
            return body
        if path not in stored:
            raise urllib.error.HTTPError(path, 404, 'absent', {}, None)
        return deepcopy(stored[path])
    github = lambda *a: {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(b'old').decode()}
    trial = Trials(kube, lambda *a: {'documents': [{**SOURCE, 'source_mapping': source_mapping}]}, github, SETTINGS)
    target = {'component': 'agent', 'repository': 'owner/repo'}
    assert trial.reconcile(RELEASE, PROOF, target)['status'] == 'RUNNING'
    assert len(writes) == 2
    assert trial.reconcile(RELEASE, PROOF, target)['status'] == 'RUNNING'
    assert len(writes) == 2  # restart never creates a second execution
    job = next(v for k,v in stored.items() if '/jobs/' in k)
    cm = next(v for k,v in stored.items() if '/configmaps/' in k)
    bundle = json.loads(cm['data']['bundle.json'])
    assert bundle['documents'][0]['content'] == RELEASE['content']
    assert cm['metadata']['ownerReferences'][0]['uid'] == 'job-uid'
    job['status'] = {'succeeded': 1}
    receipt.update({k: bundle[k] for k in ('head_sha','content_sha256','bundle_sha256','baseline_sha256','runtime_sha256')}, passed=True, scope='runtime_smoke', probes=[{'profile':'example'}])
    assert trial.reconcile(RELEASE, PROOF, target)['passed'] is True
    receipt['head_sha'] = 'wrong'
    with pytest.raises(Blocked, match='receipt'):
        trial.reconcile(RELEASE, PROOF, target)
    receipt = []
    with pytest.raises(Blocked, match='receipt'):
        trial.reconcile(RELEASE, PROOF, target)
