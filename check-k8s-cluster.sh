#!/bin/bash

# Kubernetesクラスタの状態を確認するスクリプト

echo "=== Kubernetes Cluster Health Check ==="

# kubectlが利用可能かチェック
if ! command -v kubectl &> /dev/null; then
    echo "❌ kubectl is not installed or not in PATH"
    echo "Run the following to install kubectl:"
    echo "curl -LO \"https://dl.k8s.io/release/\$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl\""
    echo "sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl"
    exit 1
fi

echo "✅ kubectl is available"

# kubeconfig の確認
if [[ ! -f ~/.kube/config ]]; then
    echo "❌ kubeconfig not found at ~/.kube/config"
    echo "If you are on a control plane node, run:"
    echo "mkdir -p ~/.kube"
    echo "sudo cp -i /etc/kubernetes/admin.conf ~/.kube/config"
    echo "sudo chown \$(id -u):\$(id -g) ~/.kube/config"
    exit 1
fi

echo "✅ kubeconfig found"

echo ""
echo "=== 1. Cluster Info ==="
kubectl cluster-info

echo ""
echo "=== 2. Node Status ==="
kubectl get nodes -o wide

echo ""
echo "=== 3. System Pods Status ==="
kubectl get pods -n kube-system

echo ""
echo "=== 4. All Namespaces Overview ==="
kubectl get all -A

echo ""
echo "=== 5. Storage Classes ==="
kubectl get storageclass

echo ""
echo "=== 6. Recent Events ==="
kubectl get events --sort-by=.metadata.creationTimestamp -A | tail -10

echo ""
echo "=== 7. Node Readiness Check ==="
NOT_READY=$(kubectl get nodes --no-headers | awk '{print $2}' | grep -v Ready | wc -l)
if [[ $NOT_READY -eq 0 ]]; then
    echo "✅ All nodes are Ready"
else
    echo "❌ $NOT_READY nodes are not Ready"
    kubectl get nodes --no-headers | grep -v Ready
fi

echo ""
echo "=== 8. System Pods Health Check ==="
NOT_RUNNING=$(kubectl get pods -n kube-system --no-headers | awk '{print $3}' | grep -v Running | grep -v Completed | wc -l)
if [[ $NOT_RUNNING -eq 0 ]]; then
    echo "✅ All system pods are Running or Completed"
else
    echo "❌ $NOT_RUNNING system pods are not Running"
    kubectl get pods -n kube-system --no-headers | grep -v Running | grep -v Completed
fi

echo ""
echo "=== 9. DNS Test ==="
if kubectl run test-dns --image=busybox --rm -it --restart=Never -- nslookup kubernetes.default &>/dev/null; then
    echo "✅ DNS resolution is working"
else
    echo "❌ DNS resolution failed"
fi

echo ""
echo "=== Cluster Health Check Complete ==="