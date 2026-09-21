"""Operator-applied RBAC and isolation policy, separate from runtime credentials."""
NAMESPACE = 'ai-config-trial'


def resources():
    account = {'kind': 'ServiceAccount', 'name': 'config-controller', 'namespace': NAMESPACE}
    docs = [
        {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': NAMESPACE, 'labels': {'pod-security.kubernetes.io/enforce': 'restricted'}}},
        {'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': {'name': 'config-controller', 'namespace': NAMESPACE}, 'automountServiceAccountToken': False},
        {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'config-controller-api', 'namespace': NAMESPACE, 'annotations': {'kubernetes.io/service-account.name': 'config-controller'}}, 'type': 'kubernetes.io/service-account-token'},
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role', 'metadata': {'name': 'config-trials', 'namespace': NAMESPACE}, 'rules': [
            {'apiGroups': ['batch'], 'resources': ['jobs'], 'verbs': ['get', 'create']},
            {'apiGroups': [''], 'resources': ['configmaps'], 'verbs': ['get', 'create', 'update']},
            {'apiGroups': [''], 'resources': ['pods'], 'verbs': ['get', 'list']},
            {'apiGroups': [''], 'resources': ['pods/log'], 'verbs': ['get']}]},
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding', 'metadata': {'name': 'config-trials', 'namespace': NAMESPACE}, 'subjects': [account], 'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': 'config-trials'}},
        {'apiVersion': 'v1', 'kind': 'ResourceQuota', 'metadata': {'name': 'config-trials', 'namespace': NAMESPACE}, 'spec': {'hard': {'pods': '64', 'count/jobs.batch': '64', 'configmaps': '64', 'requests.cpu': '4', 'requests.memory': '8Gi', 'limits.cpu': '16', 'limits.memory': '16Gi'}}},
        {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': {'name': 'config-trials', 'namespace': NAMESPACE}, 'spec': {
            'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': []}},
        {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': {'name': 'trial-to-broker', 'namespace': NAMESPACE}, 'spec': {
            'podSelector': {'matchLabels': {'app': 'config-trial'}}, 'policyTypes': ['Egress'],
            'egress': [{'to': [{'podSelector': {'matchLabels': {'app': 'config-trial-broker'}}}], 'ports': [{'protocol': 'TCP', 'port': 8080}]}]}},
        {'apiVersion': 'cilium.io/v2', 'kind': 'CiliumNetworkPolicy', 'metadata': {'name': 'config-trial-broker', 'namespace': NAMESPACE}, 'spec': {
            'endpointSelector': {'matchLabels': {'app': 'config-trial-broker'}},
            'ingress': [{'fromEndpoints': [{'matchLabels': {'app': 'config-trial'}}], 'toPorts': [{'ports': [{'port': '8080', 'protocol': 'TCP'}]}]}],
            'egress': [
                {'toEndpoints': [{'matchLabels': {'k8s:io.kubernetes.pod.namespace': 'kube-system', 'k8s:k8s-app': 'kube-dns'}}],
                 'toPorts': [{'ports': [{'port': '53', 'protocol': 'ANY'}], 'rules': {'dns': [{'matchName': 'api.openai.com'}, {'matchName': 'chatgpt.com'}]}}]},
                {'toFQDNs': [{'matchName': 'api.openai.com'}, {'matchName': 'chatgpt.com'}], 'toPorts': [{'ports': [{'port': '443', 'protocol': 'TCP'}]}]}]}}

    ]
    for ns, names in [('ai-agent', ['ai-business-agent', 'ai-business-workflow-controller']), ('ai-worker', ['ai-business-worker', 'ai-business-agent-edge'])]:
        docs.extend([
            {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role', 'metadata': {'name': 'config-rollout-reader', 'namespace': ns}, 'rules': [{'apiGroups': ['apps'], 'resources': ['deployments'], 'resourceNames': names, 'verbs': ['get']}]},
            {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding', 'metadata': {'name': 'config-rollout-reader', 'namespace': ns}, 'subjects': [account], 'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': 'config-rollout-reader'}}])
    return docs


def broker_resources(image, script):
    import hashlib
    labels = {'app': 'config-trial-broker'}
    return [
        {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'config-trial-broker', 'namespace': NAMESPACE}, 'data': {'broker.py': script}},
        {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'config-trial-broker', 'namespace': NAMESPACE}, 'spec': {'selector': labels, 'ports': [{'port': 8080, 'targetPort': 8080}]}},
        {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'config-trial-broker', 'namespace': NAMESPACE}, 'spec': {
            'replicas': 1, 'selector': {'matchLabels': labels}, 'template': {
                'metadata': {'labels': labels, 'annotations': {'config/broker-code': hashlib.sha256(script.encode()).hexdigest()}},
                'spec': {'automountServiceAccountToken': False, 'nodeSelector': {'homelab.craftz.dev/workload-plane': 'true'},
                         'dnsConfig': {'options': [{'name': 'ndots', 'value': '1'}]},
                         'securityContext': {'runAsNonRoot': True, 'runAsUser': 10002, 'runAsGroup': 10002, 'fsGroup': 10002, 'seccompProfile': {'type': 'RuntimeDefault'}},
                         'imagePullSecrets': [{'name': 'harbor-pull'}],
                         'containers': [{'name': 'broker', 'image': image, 'command': ['python', '/broker/broker.py'],
                             'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}},
                             'env': [{'name': 'PYTHONDONTWRITEBYTECODE', 'value': '1'}],
                             'resources': {'requests': {'cpu': '50m', 'memory': '64Mi'}, 'limits': {'cpu': '1', 'memory': '256Mi'}},
                             'readinessProbe': {'exec': {'command': ['python', '-c', "import socket; socket.create_connection(('127.0.0.1', 8080), timeout=2).close()"]}, 'timeoutSeconds': 3, 'periodSeconds': 5},
                             'volumeMounts': [{'name': 'code', 'mountPath': '/broker', 'readOnly': True}, {'name': 'auth', 'mountPath': '/auth', 'readOnly': True}]}],
                         'volumes': [{'name': 'code', 'configMap': {'name': 'config-trial-broker'}}, {'name': 'auth', 'secret': {'secretName': 'config-trial-codex-auth', 'defaultMode': 288}}]}}}}
    ]
