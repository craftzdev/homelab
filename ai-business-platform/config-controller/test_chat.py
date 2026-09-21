import io,json,urllib.error
import pytest
from chat_run import parse_response
from chat_runtime import ChatJobs
from controller import Blocked,digest

def event(value):return ('data: '+json.dumps(value)+'\n\n').encode()
def complete(value):return event({'type':'response.completed','response':{'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(value)}]}]}})

def test_bounded_completed_response():
    assert parse_response(io.BytesIO(complete({'reply':'説明','proposal':None})))=={'reply':'説明','proposal':None}
    for raw in [event({'type':'response.output_text.delta','delta':'partial'}),event({'type':'response.failed'}),complete({'reply':'','proposal':None}),complete({'reply':'ok','proposal':'文'*30000}),complete({'reply':'ok','proposal':None,'command':'deploy'})]:
        with pytest.raises(ValueError):parse_response(io.BytesIO(raw))

def test_recoverable_isolated_job():
    values={};posts=[]
    def kube(method,path,body=None,raw=False):
        if method=='GET':
            if path not in values:raise urllib.error.HTTPError(path,404,'missing',{},None)
            return values[path]
        if method=='POST':
            posts.append(body);body['metadata']['uid']='job-uid';values[path+'/'+body['metadata']['name']]=body;return body
        if method=='PUT':values[path]=body;return body
    runner=ChatJobs(kube,None,None,{'namespace':'trial','worker_image':'worker@sha256:abc','agent_image':'agent@sha256:abc','broker_url':'http://10.96.0.10:8080/v1'})
    context={'source':{},'content':'# Rules','message':'Explain','history':[]}
    turn={'id':'00000000-0000-0000-0000-000000000001','context':context,'request_sha256':digest(json.dumps(context,sort_keys=True)),'state':'QUEUED'}
    assert runner.reconcile(turn)['state']=='RUNNING'
    assert runner.reconcile({**turn,'state':'RUNNING'})['state']=='RUNNING' and len(posts)==2
    pod=posts[1]['spec']['template']['spec']
    assert pod['automountServiceAccountToken'] is False and 'initContainers' not in pod
    assert pod['containers'][0]['command']==['python','/candidate/chat.py']
    assert not any('secret' in v for v in pod['volumes']) and posts[1]['spec']['backoffLimit']==0
    with pytest.raises(Blocked):runner.reconcile({**turn,'request_sha256':'a'*64})


def test_account_stream_without_terminal_output_is_assembled_once():
    answer=json.dumps({'reply':'説明','proposal':None})
    stream=event({'type':'response.output_text.delta','output_index':0,'content_index':0,'delta':answer[:10]})
    stream+=event({'type':'response.output_text.delta','output_index':0,'content_index':0,'delta':answer[10:]})
    stream+=event({'type':'response.output_text.done','output_index':0,'content_index':0,'text':answer})
    with pytest.raises(ValueError):parse_response(io.BytesIO(stream))
    stream+=event({'type':'response.completed','response':{'status':'completed','output':[]}})
    assert parse_response(io.BytesIO(stream))=={'reply':'説明','proposal':None}
