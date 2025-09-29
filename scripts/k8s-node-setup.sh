#!/bin/bash

# k8s-node-setup.sh - Kubernetes cluster setup script using Ansible
# This script sets up a Kubernetes cluster using Ansible playbooks

set -euo pipefail

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ANSIBLE_DIR="$PROJECT_ROOT/ansible"

# Node configuration
declare -A NODE_IPS=(
    [0]="172.16.40.11"  # k8s-cp-1
    [1]="172.16.40.12"  # k8s-cp-2
    [2]="172.16.40.13"  # k8s-cp-3
    [3]="172.16.40.21"  # k8s-wk-1
    [4]="172.16.40.22"  # k8s-wk-2
    [5]="172.16.40.23"  # k8s-wk-3
)

KUBE_API_SERVER_VIP="172.16.40.100"
EXTERNAL_KUBE_API_SERVER="k8s-api-$(date +%s).local"

# Function to log messages
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# Function to check if running on first control plane
is_first_control_plane() {
    local hostname=$(hostname)
    [[ "$hostname" == "k8s-cp-1" ]]
}

# Function to run Ansible playbook
run_ansible_playbook() {
    log "Running Ansible playbook..."
    cd "$ANSIBLE_DIR"
    
    # Check if ansible-playbook is available
    if ! command -v ansible-playbook &> /dev/null; then
        log "Installing Ansible..."
        apt-get update
        apt-get install -y ansible
    fi
    
    # Install Ansible collections if needed
    if [[ -f requirements.yaml ]]; then
        ansible-galaxy install -r requirements.yaml
    fi
    
    # Run the playbook
    ansible-playbook -i hosts/k8s-servers/inventory site.yaml
}

# Main execution
main() {
    log "Starting Kubernetes cluster setup..."
    log "Hostname: $(hostname)"
    log "IP Address: $(hostname -I | awk '{print $1}')"
    
    # Only run on first control plane node
    if is_first_control_plane; then
        log "Running on first control plane node - executing Ansible playbook"
        run_ansible_playbook
        log "Kubernetes cluster setup completed successfully"
    else
        log "This script should only be run on the first control plane node (k8s-cp-1)"
        log "Other nodes will be configured automatically via Ansible"
        exit 1
    fi
}

# Execute main function
main "$@"

# Ends except first-control-plane
case $1 in
    k8s-cp-1)
        ;;
    k8s-cp-2|k8s-cp-3)
        # Wait for first control plane to be ready and join configuration to be available
        echo "[INFO] Waiting for first control plane to be ready..."
        while ! curl -k https://${KUBE_API_SERVER_VIP}:8443/healthz >/dev/null 2>&1; do
            echo "[INFO] Waiting for API server to be available..."
            sleep 10
        done
        
        # Download join configuration from first control plane
        echo "[INFO] Downloading join configuration from k8s-cp-1..."
        scp -o StrictHostKeyChecking=no cloudinit@${NODE_IPS[0]}:/root/join_kubeadm_cp.yaml /root/join_kubeadm_cp.yaml || {
            echo "[ERROR] Failed to download join configuration"
            exit 1
        }
        
        # Join the cluster as control plane
        echo "[INFO] Joining cluster as control plane node..."
        kubeadm join --config /root/join_kubeadm_cp.yaml
        
        # Set up kubeconfig for root user
        mkdir -p "$HOME"/.kube
        if [ -f /etc/kubernetes/admin.conf ]; then
            cp /etc/kubernetes/admin.conf "$HOME"/.kube/config
            chown "$(id -u)":"$(id -g)" "$HOME"/.kube/config
        fi
        
        # Set up kubeconfig for cloudinit user
        sudo -u cloudinit mkdir -p /home/cloudinit/.kube
        if [ -f /etc/kubernetes/admin.conf ]; then
            sudo cp /etc/kubernetes/admin.conf /home/cloudinit/.kube/config
            sudo chown cloudinit:cloudinit /home/cloudinit/.kube/config
        fi
        
        # Persist KUBECONFIG for cloudinit user
        sudo bash -c "grep -q 'export KUBECONFIG=\$HOME/.kube/config' /home/cloudinit/.bashrc || echo 'export KUBECONFIG=\$HOME/.kube/config' >> /home/cloudinit/.bashrc"
        
        # Provide a system-wide default for interactive shells when admin.conf exists
        if [ -f /etc/kubernetes/admin.conf ]; then
            cat <<'EOP' | sudo tee /etc/profile.d/kubeconfig.sh >/dev/null
if [ -z "${KUBECONFIG:-}" ] && [ -f "$HOME/.kube/config" ]; then
  export KUBECONFIG="$HOME/.kube/config"
fi
EOP
            sudo chmod 0644 /etc/profile.d/kubeconfig.sh
        fi
        
        echo "[INFO] Control plane node setup completed successfully"
        exit 0
        ;;
    *)
        exit 1
        ;;
esac

# region : setup for first-control-plane node

# Set kubeadm bootstrap token using openssl
KUBEADM_BOOTSTRAP_TOKEN=$(openssl rand -hex 3).$(openssl rand -hex 8)

# Set init configuration for the first control plane
# Detect the kubeadm version so ClusterConfiguration matches installed binaries
K8S_VERSION="$(kubeadm version -o short 2>/dev/null || true)"
if [ -z "$K8S_VERSION" ]; then
  echo "[WARN] Failed to detect kubeadm version; proceeding without explicit kubernetesVersion"
fi
cat > "$HOME"/init_kubeadm.yaml <<EOF
apiVersion: kubeadm.k8s.io/v1beta4
kind: InitConfiguration
bootstrapTokens:
- token: "$KUBEADM_BOOTSTRAP_TOKEN"
  description: "kubeadm bootstrap token"
  ttl: "24h"
nodeRegistration:
  criSocket: "unix:///var/run/containerd/containerd.sock"
---
apiVersion: kubeadm.k8s.io/v1beta4
kind: ClusterConfiguration
networking:
  serviceSubnet: "10.96.0.0/16"
  podSubnet: "10.128.0.0/16"
$( [ -n "$K8S_VERSION" ] && echo "kubernetesVersion: \"$K8S_VERSION\"" )
controlPlaneEndpoint: "${KUBE_API_SERVER_VIP}:8443"
apiServer:
  certSANs:
  - "${EXTERNAL_KUBE_API_SERVER}" # generate random FQDN to prevent malicious DoS attack
controllerManager:
  extraArgs:
  - name: bind-address
    value: "0.0.0.0"
scheduler:
  extraArgs:
  - name: bind-address
    value: "0.0.0.0"
---
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
cgroupDriver: "systemd"
protectKernelDefaults: true
EOF

# Install Kubernetes without kube-proxy (idempotent)
if [ -f /etc/kubernetes/manifests/kube-apiserver.yaml ]; then
  echo "[INFO] kubeadm already initialized; skipping kubeadm init"
else
  set +e
  kubeadm init --config "$HOME"/init_kubeadm.yaml --skip-phases=addon/kube-proxy --ignore-preflight-errors=NumCPU,Mem | tee /root/kubeadm-init.log
  INIT_STATUS=${PIPESTATUS[0]}
  set -e
  if [ $INIT_STATUS -ne 0 ]; then
    echo "[ERROR] kubeadm init failed. See /root/kubeadm-init.log"
    exit $INIT_STATUS
  fi
fi

mkdir -p "$HOME"/.kube
if [ -f /etc/kubernetes/admin.conf ]; then
  cp /etc/kubernetes/admin.conf "$HOME"/.kube/config
  chown "$(id -u)":"$(id -g)" "$HOME"/.kube/config
fi

# Set up kubeconfig for cloudinit user as well
sudo -u cloudinit mkdir -p /home/cloudinit/.kube
if [ -f /etc/kubernetes/admin.conf ]; then
  sudo cp /etc/kubernetes/admin.conf /home/cloudinit/.kube/config
  sudo chown cloudinit:cloudinit /home/cloudinit/.kube/config
fi

# Persist KUBECONFIG for cloudinit user
sudo bash -c "grep -q 'export KUBECONFIG=\$HOME/.kube/config' /home/cloudinit/.bashrc || echo 'export KUBECONFIG=\$HOME/.kube/config' >> /home/cloudinit/.bashrc"

# Also set KUBECONFIG immediately for the current session
sudo -u cloudinit bash -c 'export KUBECONFIG=/home/cloudinit/.kube/config'

# Provide a system-wide default for interactive shells when admin.conf exists
if [ -f /etc/kubernetes/admin.conf ]; then
  cat <<'EOP' | sudo tee /etc/profile.d/kubeconfig.sh >/dev/null
if [ -z "${KUBECONFIG:-}" ] && [ -f "$HOME/.kube/config" ]; then
  export KUBECONFIG="$HOME/.kube/config"
fi
EOP
  sudo chmod 0644 /etc/profile.d/kubeconfig.sh
fi

# クラスタ初期セットアップ時に helm　を使用して CNI と ArgoCD をクラスタに導入する
# それ以外のクラスタリソースは ArgoCD によって本リポジトリから自動で導入される

# Install Helm CLI
curl https://raw.githubusercontent.com/helm/helm/master/scripts/get-helm-3 | bash

# Install Cilium Helm chart (idempotent)
helm repo add cilium https://helm.cilium.io/
helm repo update
helm upgrade --install cilium cilium/cilium \
    --namespace kube-system \
    --set kubeProxyReplacement=true \
    --set k8sServiceHost=${KUBE_API_SERVER_VIP} \
    --set k8sServicePort=8443

# Install ArgoCD Helm chart
helm repo add argo https://argoproj.github.io/argo-helm
helm upgrade --install argocd argo/argo-cd \
    --version 8.5.7 \
    --create-namespace \
    --namespace argocd \
    --values https://raw.githubusercontent.com/craftzdev/homelab/"${TARGET_BRANCH}"/k8s-manifests/argocd-helm-chart-values.yaml
helm upgrade --install argocd-apps argo/argocd-apps \
    --version 2.0.2 \
    --namespace argocd \
    --values https://raw.githubusercontent.com/craftzdev/homelab/"${TARGET_BRANCH}"/k8s-manifests/argocd-apps-helm-chart-values.yaml


cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Namespace
metadata:
  name: cluster-wide-apps
---
apiVersion: v1
kind: Secret
metadata:
  name: external-k8s-endpoint
  namespace: cluster-wide-apps
type: Opaque
stringData:
  fqdn: "${EXTERNAL_KUBE_API_SERVER}"
  port: "8443"
EOF

# Generate control plane certificate
KUBEADM_UPLOADED_CERTS=$(kubeadm init phase upload-certs --upload-certs | tail -n 1)

# Set join configuration for other control plane nodes
cat > "$HOME"/join_kubeadm_cp.yaml <<EOF
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
cgroupDriver: "systemd"
protectKernelDefaults: true
---
apiVersion: kubeadm.k8s.io/v1beta4
kind: JoinConfiguration
nodeRegistration:
  criSocket: "unix:///var/run/containerd/containerd.sock"
discovery:
  bootstrapToken:
    apiServerEndpoint: "${KUBE_API_SERVER_VIP}:8443"
    token: "$KUBEADM_BOOTSTRAP_TOKEN"
    unsafeSkipCAVerification: true
controlPlane:
  certificateKey: "$KUBEADM_UPLOADED_CERTS"
EOF

# Set join configuration for worker nodes
cat > "$HOME"/join_kubeadm_wk.yaml <<EOF
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
cgroupDriver: "systemd"
protectKernelDefaults: true
---
apiVersion: kubeadm.k8s.io/v1beta4
kind: JoinConfiguration
nodeRegistration:
  criSocket: "unix:///var/run/containerd/containerd.sock"
discovery:
  bootstrapToken:
    apiServerEndpoint: "${KUBE_API_SERVER_VIP}:8443"
    token: "$KUBEADM_BOOTSTRAP_TOKEN"
    unsafeSkipCAVerification: true
EOF

# ---

# Generate control plane certificate
KUBEADM_UPLOADED_CERTS=$(kubeadm init phase upload-certs --upload-certs | tail -n 1)

# Set join configuration for other control plane nodes
cat > "$HOME"/join_kubeadm_cp.yaml <<EOF
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
cgroupDriver: "systemd"
protectKernelDefaults: true
---
apiVersion: kubeadm.k8s.io/v1beta4
kind: JoinConfiguration
nodeRegistration:
  criSocket: "unix:///var/run/containerd/containerd.sock"
discovery:
  bootstrapToken:
    apiServerEndpoint: "${KUBE_API_SERVER_VIP}:8443"
    token: "$KUBEADM_BOOTSTRAP_TOKEN"
    unsafeSkipCAVerification: true
controlPlane:
  certificateKey: "$KUBEADM_UPLOADED_CERTS"
EOF

# Set join configuration for worker nodes
cat > "$HOME"/join_kubeadm_wk.yaml <<EOF
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
cgroupDriver: "systemd"
protectKernelDefaults: true
---
apiVersion: kubeadm.k8s.io/v1beta4
kind: JoinConfiguration
nodeRegistration:
  criSocket: "unix:///var/run/containerd/containerd.sock"
discovery:
  bootstrapToken:
    apiServerEndpoint: "${KUBE_API_SERVER_VIP}:8443"
    token: "$KUBEADM_BOOTSTRAP_TOKEN"
    unsafeSkipCAVerification: true
EOF

# ---

# install ansible
sudo apt-get install -y ansible git sshpass

# clone repo
git clone -b "${TARGET_BRANCH}" https://github.com/craftzdev/homelab.git "$HOME"/homelab

# export ansible.cfg target
export ANSIBLE_CONFIG="$HOME"/homelab/ansible/ansible.cfg

# run ansible-playbook
ansible-galaxy role install -r "$HOME"/homelab/ansible/roles/requirements.yaml
ansible-galaxy collection install -r "$HOME"/homelab/ansible/roles/requirements.yaml
ansible-playbook -i "$HOME"/homelab/ansible/hosts/k8s-servers/inventory "$HOME"/homelab/ansible/site.yaml

# endregion
