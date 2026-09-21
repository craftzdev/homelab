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
            'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': [
                {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}}, 'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}], 'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]},
                {'to': [{'ipBlock': {'cidr': '0.0.0.0/0', 'except': ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', '169.254.0.0/16', '127.0.0.0/8']}}], 'ports': [{'protocol': 'TCP', 'port': 443}]}]}}
    ]
    for ns, names in [('ai-agent', ['ai-business-agent', 'ai-business-workflow-controller']), ('ai-worker', ['ai-business-worker', 'ai-business-agent-edge'])]:
        docs.extend([
            {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role', 'metadata': {'name': 'config-rollout-reader', 'namespace': ns}, 'rules': [{'apiGroups': ['apps'], 'resources': ['deployments'], 'resourceNames': names, 'verbs': ['get']}]},
            {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding', 'metadata': {'name': 'config-rollout-reader', 'namespace': ns}, 'subjects': [account], 'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': 'config-rollout-reader'}}])
    return docs
