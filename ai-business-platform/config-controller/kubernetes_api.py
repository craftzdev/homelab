"""Small Kubernetes adapter. The controller's ServiceAccount is namespace-scoped."""
import json
import os
from pathlib import Path
import ssl
import subprocess
import urllib.error
import urllib.request


class Kubernetes:
    def __init__(self):
        self.kubeconfig = os.environ.get('KUBECONFIG')
        if not self.kubeconfig:
            root = Path('/var/run/secrets/kubernetes.io/serviceaccount')
            self.token_file = Path(os.environ.get('KUBERNETES_TOKEN_FILE', str(root / 'token')))
            self.context = ssl.create_default_context(cafile=os.environ.get('KUBERNETES_CA_FILE', str(root / 'ca.crt')))
            self.base = os.environ.get('KUBERNETES_API_SERVER') or ('https://' + os.environ['KUBERNETES_SERVICE_HOST'] + ':' + os.environ.get('KUBERNETES_SERVICE_PORT', '443'))
            if not self.base.startswith('https://'):
                raise ValueError('Kubernetes API requires TLS')

    def __call__(self, method, path, body=None, *, raw=False):
        if self.kubeconfig:
            commands = {'GET': ['get'], 'POST': ['create'], 'DELETE': ['delete'], 'PUT': ['replace']}
            command = ['kubectl', '--kubeconfig', self.kubeconfig] + commands[method] + ['--raw=' + path]
            if body is not None:
                command += ['-f', '-']
            result = subprocess.run(command, input=json.dumps(body).encode() if body is not None else None, capture_output=True, timeout=30)
            if result.returncode:
                code = 404 if b'NotFound' in result.stderr else 409 if b'AlreadyExists' in result.stderr else 503
                raise urllib.error.HTTPError(path, code, 'Kubernetes request failed', {}, None)
            value = result.stdout
        else:
            request = urllib.request.Request(self.base + path, method=method,
                headers={'Authorization': 'Bearer ' + self.token_file.read_text().strip(), 'Content-Type': 'application/json'},
                data=json.dumps(body).encode() if body is not None else None)
            with urllib.request.urlopen(request, context=self.context, timeout=30) as response:
                value = response.read(2_097_153)
        if len(value) > 2_097_152:
            raise ValueError('Kubernetes response exceeds limit')
        return value.decode() if raw else json.loads(value)
