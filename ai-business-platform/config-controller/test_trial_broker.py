import io
import json
from http.server import HTTPServer
from threading import Thread
import urllib.error
import urllib.request
import pytest
from trial_broker import Broker, NoRedirect, upstream_request
from deploy.resources import resources, broker_resources


def test_only_fixed_upstream_and_auth_headers_are_used():
    raw=json.dumps({'model':'test-model','stream':True,'tools':[{'type':'web_search'}],'store':True}).encode()
    request=upstream_request(raw, {'OPENAI_API_KEY':'test-only-api-key'})
    assert request.full_url=='https://api.openai.com/v1/responses'
    assert request.get_header('Authorization')=='Bearer test-only-api-key'
    assert json.loads(request.data)['tools']==[] and json.loads(request.data)['store'] is False
    request=upstream_request(raw, {'tokens':{'access_token':'test-only-access-token','account_id':'test-account'}})
    assert request.full_url=='https://chatgpt.com/backend-api/codex/responses'
    assert request.get_header('Chatgpt-account-id')=='test-account'
    assert NoRedirect().redirect_request(None,None,None,None,None,None) is None
    with pytest.raises(ValueError):upstream_request(b'[]',{})


def test_http_relay_never_returns_upstream_errors_or_credentials(tmp_path,monkeypatch):
    auth=tmp_path/'auth.json';auth.write_text(json.dumps({'OPENAI_API_KEY':'test-secret'}))
    monkeypatch.setattr(Broker,'auth_path',auth)
    Broker.calls=[]
    class Opener:
        def open(self,request,**kwargs):
            assert request.get_header('Authorization')=='Bearer test-secret'
            raise urllib.error.HTTPError(request.full_url,401,'upstream error',{},io.BytesIO(b'test-secret'))
    monkeypatch.setattr(Broker,'opener',Opener())
    server=HTTPServer(('127.0.0.1',0),Broker)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        url='http://127.0.0.1:'+str(server.server_port)
        for path,code in [('/v1/responses',502),('/arbitrary-destination',404)]:
            request=urllib.request.Request(url+path,data=b'{"model":"test","stream":true}')
            with pytest.raises(urllib.error.HTTPError) as error:urllib.request.urlopen(request,timeout=2)
            assert error.value.code==code and b'test-secret' not in error.value.read()
    finally:
        server.shutdown();server.server_close();thread.join()


def test_trial_has_no_internet_or_dns_and_credentials_belong_to_separate_pod():
    policies=resources()
    deny=next(p for p in policies if p['kind']=='NetworkPolicy' and p['metadata']['name']=='config-trials')
    assert deny['spec']['egress']==deny['spec']['ingress']==[]
    trial=next(p for p in policies if p['metadata']['name']=='trial-to-broker')
    assert trial['spec']['egress']==[{'to':[{'podSelector':{'matchLabels':{'app':'config-trial-broker'}}}],'ports':[{'protocol':'TCP','port':8080}]}]
    broker_policy=next(p for p in policies if p['kind']=='CiliumNetworkPolicy')
    assert broker_policy['spec']['egress'][1]['toFQDNs']==[{'matchName':'api.openai.com'},{'matchName':'chatgpt.com'}]
    pod=broker_resources('registry/worker@sha256:'+'a'*64,'print("broker")')[-1]['spec']['template']['spec']
    assert pod['automountServiceAccountToken'] is False
    assert any('secret' in v for v in pod['volumes'])
    assert all('emptyDir' not in v and 'persistentVolumeClaim' not in v for v in pod['volumes'])
    assert pod['containers'][0]['command']==['python','/broker/broker.py']
