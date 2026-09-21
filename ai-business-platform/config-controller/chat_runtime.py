"""Recoverable, isolated model jobs for immutable configuration-chat turns."""
import json
from pathlib import Path
import urllib.error
from urllib.parse import quote

from controller import Blocked, digest
from runtime_trial import Trials


class ChatJobs(Trials):
    def reconcile(self, turn):
        name = 'chat-' + turn['id']
        fingerprint = digest(json.dumps(turn['context'],sort_keys=True))
        if fingerprint != turn['request_sha256']:
            raise Blocked('chat request identity mismatch')
        bundle = {'broker_url':self.settings['broker_url'],'model':self.settings.get('chat_model','gpt-5.6-sol'),
                  'context':turn['context'],'request_sha256':fingerprint}
        if len(json.dumps(bundle).encode()) > 800_000:
            raise Blocked('chat input exceeds the budget')
        bundle_hash = digest(json.dumps(bundle,sort_keys=True))
        annotations = {'chat/request':fingerprint,'chat/bundle':bundle_hash}
        try:
            job = self.kube('GET',self.api+'/'+name)
        except urllib.error.HTTPError as failure:
            if failure.code != 404:
                raise
            if turn['state'] != 'QUEUED':
                raise Blocked('chat runner disappeared; resend the message')
            cm = {'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':name,'labels':{'app':'config-trial'}},'immutable':True,
                  'data':{'bundle.json':json.dumps(bundle),'chat.py':Path(__file__).with_name('chat_run.py').read_text()}}
            try:
                self.kube('POST',self.core+'/configmaps',cm)
            except urllib.error.HTTPError as failure:
                if failure.code != 409:
                    raise
                prior = self.kube('GET',self.core+'/configmaps/'+name)
                if prior['data'] != cm['data']:
                    raise Blocked('chat input changed')
            job = self.job(name,{'head_sha':fingerprint,'content_sha256':fingerprint,'bundle_sha256':bundle_hash})
            job['metadata']['annotations'] = annotations
            spec = job['spec']['template']['spec']
            spec.pop('initContainers')
            spec['containers'][0]['command'] = ['python','/candidate/chat.py']
            job['spec'].update(activeDeadlineSeconds=180,ttlSecondsAfterFinished=3600)
            try:
                job = self.kube('POST',self.api,job)
            except urllib.error.HTTPError as failure:
                if failure.code != 409:
                    raise
                job = self.kube('GET',self.api+'/'+name)
        if job['metadata'].get('annotations') != annotations:
            raise Blocked('chat job identity mismatch')
        cm = self.kube('GET',self.core+'/configmaps/'+name)
        if digest(json.dumps(json.loads(cm['data']['bundle.json']),sort_keys=True)) != bundle_hash:
            raise Blocked('chat bundle identity mismatch')
        owners = [{'apiVersion':'batch/v1','kind':'Job','name':name,'uid':job['metadata']['uid']}]
        if cm['metadata'].get('ownerReferences') != owners:
            cm['metadata']['ownerReferences'] = owners
            self.kube('PUT',self.core+'/configmaps/'+name,cm)
        state = job.get('status',{})
        if state.get('failed'):
            raise Blocked('chat runner failed')
        if not state.get('succeeded'):
            return {'state':'RUNNING','request_sha256':fingerprint}
        pods = self.kube('GET',self.core+'/pods?labelSelector='+quote('job-name='+name,safe=''))['items']
        pods = [p for p in pods if any(o.get('uid') == job['metadata']['uid'] for o in p['metadata'].get('ownerReferences',[]))]
        if len(pods) != 1 or pods[0].get('status',{}).get('phase') != 'Succeeded':
            raise Blocked('chat completion unavailable')
        raw = self.kube('GET',self.core+'/pods/'+pods[0]['metadata']['name']+'/log?container=trial&limitBytes=600000',raw=True)
        result = json.loads(raw)
        if result.get('request_sha256') != fingerprint or result.get('state') not in {'COMPLETED','FAILED'}:
            raise Blocked('chat result identity mismatch')
        return result


def tick_chat(gateway, runner):
    for turn in gateway('GET','/v1/config/chat/controller/work')['turns']:
        try:
            result = runner.reconcile(turn)
        except Blocked:
            result = {'state':'FAILED','request_sha256':turn['request_sha256'],'failure':'応答の生成を完了できませんでした。再送信してください。'}
        gateway('POST','/v1/config/chat/controller/turns/'+turn['id']+'/report',result)
