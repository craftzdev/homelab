import base64
from copy import deepcopy
from datetime import datetime, timezone
import yaml
import pytest
from rollout import Rollouts


SHA = 'c'*40
RELEASE = {'id': 'release', 'source': {'path': 'profiles/example.md'}, 'content': 'candidate', 'evidence': {'merge_sha': SHA}}
TARGET = {'repository': 'owner/repo', 'branch':'main', 'deployments':[{'path':'deploy/kubernetes/deployment.yaml','namespace':'ai-agent','name':'agent'}]}


def setup():
    image = 'registry/agent@sha256:' + 'a'*64
    deployment = {'apiVersion':'apps/v1','kind':'Deployment','metadata': {'generation': 2, 'name':'agent', 'namespace':'ai-agent'}, 'spec': {'replicas':1,'template':{'metadata':{'annotations':{'ai-business/source-revision':SHA}},'spec':{'containers':[{'name':'agent','image':image}]}}}, 'status':{'observedGeneration':2,'replicas':1,'updatedReplicas':1,'availableReplicas':1}}
    run = {'id':1,'head_sha':SHA,'path':'.github/workflows/container.yaml','html_url':'https://github.com/owner/repo/actions/runs/1','status':'completed','conclusion':'success'}
    main_content = ['candidate']
    def api(method,path,body=None):
        if '/git/ref/' in path:return {'object':{'sha':'d'*40}}
        if '/compare/' in path:return {'status':'ahead'}
        if '/runs?' in path:return {'workflow_runs':[run]}
        if '/edge.yaml?' in path:
            edge = deepcopy(deployment)
            edge['metadata'].update(name='edge', namespace='ai-worker')
            text = yaml.safe_dump_all([{'apiVersion':'v1','kind':'Service','metadata':{'name':'edge'}}, edge])
        else:
            text = yaml.safe_dump(deployment) if '/deployment.yaml?' in path else main_content[0]
        return {'type':'file','encoding':'base64','content':base64.b64encode(text.encode()).decode()}
    live = deepcopy(deployment)
    observer = Rollouts(lambda *args:live,api,now=lambda:datetime(2026,9,21,tzinfo=timezone.utc))
    return observer,live,run,main_content


def test_merge_or_image_build_alone_does_not_mean_deployed():
    o,live,run,_=setup()
    run.update(status='in_progress',conclusion=None)
    assert o.reconcile(RELEASE,TARGET)[0]=='DEPLOYING'
    run.update(status='completed',conclusion='success')
    live['status']['observedGeneration']=1
    assert o.reconcile(RELEASE,TARGET)[0]=='DEPLOYING'
    live['status']['observedGeneration']=2
    live['spec']['template']['metadata']['annotations']['ai-business/source-revision']='old'
    assert o.reconcile(RELEASE,TARGET)[0]=='DEPLOYING'
    live['spec']['template']['metadata']['annotations']['ai-business/source-revision']=SHA
    live['status']['terminatingReplicas']=1
    assert o.reconcile(RELEASE,TARGET)[0]=='DEPLOYING'
    live['status']['terminatingReplicas']=0
    state,proof=o.reconcile(RELEASE,TARGET)
    assert state=='DEPLOYED' and proof['deployment']['resources'][0]['generation']==2


def test_failed_pipeline_or_superseded_configuration_never_passes():
    o,_,run,content=setup()
    run['conclusion']='failure'
    assert o.reconcile(RELEASE,TARGET)[0]=='DEPLOYMENT_FAILED'
    run['conclusion']='success';content[0]='different configuration'
    assert o.reconcile(RELEASE,TARGET)[0]=='DEPLOYMENT_FAILED'


def test_rollout_timeout_does_not_reset_at_each_poll():
    o,live,_,_=setup();live['status']['availableReplicas']=0
    row=deepcopy(RELEASE); row['evidence']['deployment_started_at']='2026-09-20T00:00:00+00:00'
    assert o.reconcile(row,TARGET)[0]=='DEPLOYMENT_FAILED'


def test_every_deployment_must_be_ready():
    observer, live, _, _ = setup()
    second = deepcopy(live)
    second['status']['updatedReplicas'] = 0
    observer.kube = lambda method,path: second if path.endswith('/edge') else live
    target = {**TARGET, 'deployments': TARGET['deployments'] + [{'namespace':'ai-worker','name':'edge','path':'deploy/kubernetes/edge.yaml'}]}
    assert observer.reconcile(RELEASE,target)[0] == 'DEPLOYING'
    second['status']['updatedReplicas'] = 1
    state,proof = observer.reconcile(RELEASE,target)
    assert state == 'DEPLOYED' and len(proof['deployment']['resources']) == 2


@pytest.mark.parametrize('duplicate', [False, True])
def test_missing_or_ambiguous_deployment_is_not_accepted(duplicate):
    observer, live, _, _ = setup()
    original = observer.github
    def github(method, path):
        if '/deployment.yaml?' not in path:
            return original(method, path)
        docs = [live, live] if duplicate else [{'apiVersion':'v1','kind':'Service','metadata':{'name':'agent'}}]
        return {'type':'file','encoding':'base64','content':base64.b64encode(yaml.safe_dump_all(docs).encode()).decode()}
    observer.github = github
    assert observer.reconcile(RELEASE, TARGET)[0] == 'DEPLOYMENT_FAILED'
