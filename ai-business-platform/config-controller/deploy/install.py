"""Provision the controller on the Gateway VM without putting secrets in argv/logs.

Requires an explicit App identity and either a trial credential file or the
operator's --reuse-worker-auth choice. Does not read a user's gh login token.
"""
import argparse
import base64
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

from resources import NAMESPACE, resources

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from github_auth import GitHubAuth
from controller import HTTP

FILES = ['controller.py','github_auth.py','kubernetes_api.py','runtime_trial.py','trial_prepare.py','trial_run.py','rollout.py','requirements.txt']

REMOTE_INSTALL = r'''
import base64,grp,json,os,pathlib,subprocess,sys
payload=json.load(sys.stdin)
if subprocess.run(['id','-u','config-controller'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
    subprocess.run(['useradd','--system','--home-dir','/nonexistent','--shell','/usr/sbin/nologin','config-controller'],check=True)
gid=grp.getgrnam('config-controller').gr_gid
for folder in ['/opt/ai-config-controller','/etc/ai-config-controller']:
    pathlib.Path(folder).mkdir(exist_ok=True)
    os.chown(folder,0,gid);os.chmod(folder,0o750)
for name,raw in payload['code'].items():
    if pathlib.Path(name).name!=name: raise ValueError('invalid code filename')
    p=pathlib.Path('/opt/ai-config-controller')/name;p.write_bytes(base64.b64decode(raw));p.chmod(0o644)
for name,raw in payload['config'].items():
    if pathlib.Path(name).name!=name: raise ValueError('invalid config filename')
    p=pathlib.Path('/etc/ai-config-controller')/name;p.write_bytes(base64.b64decode(raw));os.chown(p,0,gid);p.chmod(0o640)
unit=pathlib.Path('/etc/systemd/system/ai-config-controller.service');unit.write_text(payload['unit']);unit.chmod(0o644)
subprocess.run(['python3','-m','venv','/opt/ai-config-controller/venv'],check=True,stdout=subprocess.DEVNULL)
subprocess.run(['/opt/ai-config-controller/venv/bin/pip','install','--quiet','-r','/opt/ai-config-controller/requirements.txt'],check=True,stdout=subprocess.DEVNULL)
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable','ai-config-controller.service'],check=True,stdout=subprocess.DEVNULL)
subprocess.run(['systemctl','restart','ai-config-controller.service'],check=True)
print('Controller installed and started')
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubeconfig',required=True)
    parser.add_argument('--host',default='craftz@172.16.40.30')
    parser.add_argument('--github-app-id',required=True)
    parser.add_argument('--github-installation-id',required=True)
    parser.add_argument('--github-app-key',type=Path,required=True)
    auth=parser.add_mutually_exclusive_group(required=True)
    auth.add_argument('--trial-auth-file',type=Path)
    auth.add_argument('--reuse-worker-auth',action='store_true')
    parser.add_argument('--check',action='store_true',help='validate App identity and inputs without provisioning')
    args=parser.parse_args()
    if not args.github_app_id.isdigit() or not args.github_installation_id.isdigit():
        parser.error('GitHub App and installation IDs must be numeric')
    key=args.github_app_key.read_bytes()
    targets=json.loads((ROOT/'targets.example.json').read_text())
    authenv={'GITHUB_APP_ID':args.github_app_id,'GITHUB_INSTALLATION_ID':args.github_installation_id,'GITHUB_APP_PRIVATE_KEY_FILE':str(args.github_app_key)}
    github=HTTP('https://api.github.com',GitHubAuth([t['repository'] for t in targets.values()],env=authenv))
    for t in targets.values():github('GET','/repos/'+t['repository'])
    kub=['kubectl','--kubeconfig',args.kubeconfig]
    def kub_json(*command,body=None):
        p=subprocess.run(kub+list(command),input=json.dumps(body).encode() if body is not None else None,capture_output=True,check=True)
        return json.loads(p.stdout) if p.stdout else None
    secret=kub_json('-n','ai-control-plane','get','secret','ai-business-control-plane-runtime','-o','json')['data']
    values={k:base64.b64decode(secret[k]).decode() for k in ('gateway-api-token','cf-access-client-id','cf-access-client-secret')}
    gateway=HTTP('https://gateway.craftz.dev',{'Authorization':'Bearer '+values['gateway-api-token'],'CF-Access-Client-Id':values['cf-access-client-id'],'CF-Access-Client-Secret':values['cf-access-client-secret']})
    capabilities=gateway('GET','/v1/config/contract')['capabilities']
    if not {'automatic_promotion','deployment_observation'} <= set(capabilities):
        raise ValueError('deploy the automatic-promotion Gateway API before starting this controller')
    trial_auth = args.trial_auth_file.read_bytes() if args.trial_auth_file else base64.b64decode(kub_json('-n','ai-worker','get','secret','ai-business-worker-codex-auth','-o','json')['data']['auth.json'])
    parsed_auth=json.loads(trial_auth)
    if not parsed_auth.get('OPENAI_API_KEY') and not (isinstance(parsed_auth.get('tokens'),dict) and parsed_auth['tokens'].get('access_token')):
        raise ValueError('trial auth.json does not contain a Codex credential')
    if args.check:
        print('App repository access, Gateway contract, and trial credential input validated; no changes made')
        return
    # These resources grant job creation only in the isolated trial namespace,
    # and read-only access to the four named production Deployments.
    for resource in resources():
        kub_json('apply','-f','-','-o','json',body=resource)
    def apply_secret(name,data,kind='Opaque'):
        kub_json('apply','-f','-','-o','json',body={'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':NAMESPACE},'type':kind,'data':data})
    apply_secret('config-trial-codex-auth',{'auth.json':base64.b64encode(trial_auth).decode()})
    pull=kub_json('-n','ai-worker','get','secret','harbor-pull','-o','json')
    apply_secret('harbor-pull',pull['data'],pull['type'])
    for _ in range(30):
        token=kub_json('-n',NAMESPACE,'get','secret','config-controller-api','-o','json').get('data',{})
        if token.get('token'):break
        time.sleep(1)
    else:raise RuntimeError('controller ServiceAccount token was not issued')
    server=subprocess.check_output(kub+['config','view','--minify','-o','jsonpath={.clusters[0].cluster.server}']).decode()
    # Only the dedicated controller token is transferred, never the admin kubeconfig.
    read_gateway="from pathlib import Path; import json; d=dict(x.split('=',1) for x in Path('/opt/ai-business-gateway/.env').read_text().splitlines() if '=' in x and not x.startswith('#')); print(json.dumps({'token':d['CONFIG_CONTROLLER_TOKEN']}))"
    controller_token=json.loads(subprocess.check_output(['ssh','-o','BatchMode=yes',args.host,'sudo python3 -c '+shlex.quote(read_gateway)]))['token']
    env={'GATEWAY_URL':'https://gateway.craftz.dev','GATEWAY_API_TOKEN':values['gateway-api-token'],'CF_ACCESS_CLIENT_ID':values['cf-access-client-id'],'CF_ACCESS_CLIENT_SECRET':values['cf-access-client-secret'],'CONFIG_CONTROLLER_TOKEN':controller_token,
         'GITHUB_APP_ID':args.github_app_id,'GITHUB_INSTALLATION_ID':args.github_installation_id,'GITHUB_APP_PRIVATE_KEY_FILE':'/etc/ai-config-controller/github-app.pem',
         'KUBERNETES_API_SERVER':server,'KUBERNETES_TOKEN_FILE':'/etc/ai-config-controller/kubernetes.token','KUBERNETES_CA_FILE':'/etc/ai-config-controller/kubernetes-ca.crt','TRIAL_SETTINGS_FILE':'/etc/ai-config-controller/trial-settings.json'}
    encoded=lambda value:base64.b64encode(value if isinstance(value,bytes) else value.encode()).decode()
    config={'runtime.env':encoded('\n'.join(k+'='+json.dumps(v) for k,v in env.items())+'\n'),'github-app.pem':encoded(key),'kubernetes.token':token['token'],'kubernetes-ca.crt':token['ca.crt'],
            'targets.json':encoded((ROOT/'targets.example.json').read_bytes()),'trial-settings.json':encoded((ROOT/'trial-settings.example.json').read_bytes())}
    payload={'code':{name:encoded((ROOT/name).read_bytes()) for name in FILES},'config':config,'unit':(ROOT/'deploy/config-controller.service').read_text()}
    result=subprocess.run(['ssh','-o','BatchMode=yes',args.host,'sudo python3 -c '+shlex.quote(REMOTE_INSTALL)],input=json.dumps(payload).encode(),capture_output=True)
    if result.returncode:
        # Do not dump subprocess args or environment-file contents on failure.
        raise RuntimeError('remote installation failed; inspect systemd and package prerequisites on the VM')
    print(result.stdout.decode().strip())


if __name__=='__main__':
    main()
