#!/usr/bin/env bash

set -eu
export DEBIAN_FRONTEND=noninteractive

# special thanks!: https://gist.github.com/inductor/32116c486095e5dde886b55ff6e568c8

# region : script-usage

function usage() {
    echo "usage> k8s-node-setup.sh [COMMAND]"
    echo "[COMMAND]:"
    echo "  help        show command usage"
    echo "  k8s-cp-1    run setup script for k8s-cp-1"
    echo "  k8s-cp-2    run setup script for k8s-cp-2"
    echo "  k8s-cp-3    run setup script for k8s-cp-3"
    echo "  k8s-wk-*    run setup script for k8s-wk-*"
}

case $1 in
    k8s-cp-1|k8s-cp-2|k8s-cp-3|k8s-wk-*)
        ;;
    help)
        usage
        exit 255
        ;;
    *)
        usage
        exit 255
        ;;
esac

# endregion

# region : set variables

# Set global variables
TARGET_BRANCH=$2
KUBE_API_SERVER_VIP=172.16.40.100
VIP_INTERFACE=ens18
NODE_IPS=( 172.16.40.11 172.16.40.12 172.16.40.13 )
EXTERNAL_KUBE_API_SERVER="$(tr -dc '[:lower:]' </dev/urandom | head -c 1)$(tr -dc '[:lower:]0-9' </dev/urandom | head -c 7)-k8s-api.homelab.local"

# Auto-detect NIC if the specified interface does not exist
if ! ip -o link show "$VIP_INTERFACE" >/dev/null 2>&1; then
    DETECT_IF=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $5; exit}')
    if [ -n "$DETECT_IF" ]; then
        echo "[WARN] Interface '$VIP_INTERFACE' not found. Using detected interface '$DETECT_IF'"
        VIP_INTERFACE="$DETECT_IF"
    else
        echo "[WARN] Could not auto-detect default interface; keepalived may fail to bind VIP"
    fi
fi

# set per-node variables
case $1 in
    k8s-cp-1)
        KEEPALIVED_STATE=MASTER
        KEEPALIVED_PRIORITY=101
        KEEPALIVED_UNICAST_SRC_IP=${NODE_IPS[0]}
        KEEPALIVED_UNICAST_PEERS=( "${NODE_IPS[1]}" "${NODE_IPS[2]}" )
        ;;
    k8s-cp-2)
        KEEPALIVED_STATE=BACKUP
        KEEPALIVED_PRIORITY=100
        KEEPALIVED_UNICAST_SRC_IP=${NODE_IPS[1]}
        KEEPALIVED_UNICAST_PEERS=( "${NODE_IPS[0]}" "${NODE_IPS[2]}" )
        ;;
    k8s-cp-3)
        KEEPALIVED_STATE=BACKUP
        KEEPALIVED_PRIORITY=100
        KEEPALIVED_UNICAST_SRC_IP=${NODE_IPS[2]}
        KEEPALIVED_UNICAST_PEERS=( "${NODE_IPS[0]}" "${NODE_IPS[1]}" )
        ;;
    k8s-wk-*)
        ;;
    *)
        exit 1
        ;;
esac

# endregion

# region : setup for all-node

# Install Containerd
cat <<EOF | sudo tee /etc/modules-load.d/containerd.conf
overlay
br_netfilter
EOF

sudo modprobe overlay
sudo modprobe br_netfilter

# Setup required sysctl params, these persist across reboots.
cat <<EOF | sudo tee /etc/sysctl.d/99-kubernetes-cri.conf
net.bridge.bridge-nf-call-iptables  = 1
net.ipv4.ip_forward                 = 1
net.bridge.bridge-nf-call-ip6tables = 1
EOF

# Apply sysctl params without reboot
sudo sysctl --system

## Install containerd
apt-get update && apt-get install -y apt-transport-https curl gnupg2
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --yes --dearmor -o /etc/apt/keyrings/docker.gpg
 echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update && sudo apt-get install -y containerd.io

# Configure containerd
sudo mkdir -p /etc/containerd
sudo containerd config default | sudo tee /etc/containerd/config.toml > /dev/null

if grep -q "SystemdCgroup = true" "/etc/containerd/config.toml"; then
echo "Config found, skip rewriting..."
else
sed -i -e "s/SystemdCgroup \= false/SystemdCgroup \= true/g" /etc/containerd/config.toml
fi

sudo systemctl restart containerd

# Modify kernel parameters for Kubernetes
cat <<EOF | tee /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-ip6tables = 1
net.bridge.bridge-nf-call-iptables = 1
vm.overcommit_memory = 1
vm.panic_on_oom = 0
kernel.panic = 10
kernel.panic_on_oops = 1
kernel.keys.root_maxkeys = 1000000
kernel.keys.root_maxbytes = 25000000
net.ipv4.conf.*.rp_filter = 0
EOF
sysctl --system

# Install kubeadm
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.34/deb/Release.key | sudo gpg --yes --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.34/deb/ /" | sudo tee /etc/apt/sources.list.d/kubernetes.list
apt-get update
apt-get install -y kubelet kubeadm kubectl
apt-mark hold kubelet kubeadm kubectl

# Disable swap
swapoff -a

cat > /etc/crictl.yaml <<EOF
runtime-endpoint: unix:///var/run/containerd/containerd.sock
image-endpoint: unix:///var/run/containerd/containerd.sock
timeout: 10
EOF

# endregion

# Ends except worker-plane
case $1 in
    k8s-wk-*)
        exit 0
        ;;
    k8s-cp-1|k8s-cp-2|k8s-cp-3)
        ;;
    *)
        exit 1
        ;;
esac

# region : setup for all-control-plane node

# Install HAProxy（ディストリ版をインストール）
apt-get update
apt-get install -y --no-install-recommends haproxy

cat > /etc/haproxy/haproxy.cfg <<EOF
global
    log /dev/log    local0
    log /dev/log    local1 notice
    chroot /var/lib/haproxy
    stats socket /run/haproxy/admin.sock mode 660 level admin expose-fd listeners
    stats timeout 30s
    user haproxy
    group haproxy
    daemon
defaults
    log     global
    mode    http
    option  httplog
    option  dontlognull
    timeout connect 5000
    timeout client  50000
    timeout server  50000
    errorfile 400 /etc/haproxy/errors/400.http
    errorfile 403 /etc/haproxy/errors/403.http
    errorfile 408 /etc/haproxy/errors/408.http
    errorfile 500 /etc/haproxy/errors/500.http
    errorfile 502 /etc/haproxy/errors/502.http
    errorfile 503 /etc/haproxy/errors/503.http
    errorfile 504 /etc/haproxy/errors/504.http
frontend k8s-api
    bind ${KUBE_API_SERVER_VIP}:8443
    mode tcp
    option tcplog
    default_backend k8s-api

backend k8s-api
    mode tcp
    option tcplog
    option tcp-check
    balance roundrobin
    default-server inter 10s downinter 5s rise 2 fall 2 slowstart 60s maxconn 250 maxqueue 256 weight 100
    server k8s-api-1 ${NODE_IPS[0]}:6443 check
    server k8s-api-2 ${NODE_IPS[1]}:6443 check
    server k8s-api-3 ${NODE_IPS[2]}:6443 check
EOF

# Install Keepalived
echo "net.ipv4.ip_nonlocal_bind = 1" >> /etc/sysctl.conf
sysctl -p

apt-get update && apt-get -y install keepalived

cat > /etc/keepalived/keepalived.conf <<EOF
# Define the script used to check if haproxy is still working
vrrp_script chk_haproxy { 
    script "sudo /usr/bin/killall -0 haproxy"
    interval 2 
    weight 2 
}

# Configuration for Virtual Interface
vrrp_instance LB_VIP {
    interface ${VIP_INTERFACE}
    state ${KEEPALIVED_STATE}
    priority ${KEEPALIVED_PRIORITY}
    virtual_router_id 51

    smtp_alert          # Enable Notifications Via Email

    authentication {
        auth_type AH
        auth_pass zaq12wsx	# Password for accessing vrrpd. Same on all devices
    }
    unicast_src_ip ${KEEPALIVED_UNICAST_SRC_IP} # Private IP address of master
    unicast_peer {
        ${KEEPALIVED_UNICAST_PEERS[0]}		# Private IP address of the backup haproxy
        ${KEEPALIVED_UNICAST_PEERS[1]}		# Private IP address of the backup haproxy
    }

    # The virtual ip address shared between the two loadbalancers
    virtual_ipaddress {
        ${KUBE_API_SERVER_VIP}
    }

    # Use the Defined Script to Check whether to initiate a fail over
    track_script {
        chk_haproxy
    }
}
EOF

# Create keepalived user (idempotent)
if ! getent group keepalived_script >/dev/null 2>&1; then
  groupadd -r keepalived_script
fi
if ! id -u keepalived_script >/dev/null 2>&1; then
  useradd -r -s /sbin/nologin -g keepalived_script -M keepalived_script
fi

if ! grep -qE '^keepalived_script\s+ALL=\(ALL\)\s+NOPASSWD:\s+/usr/bin/killall' /etc/sudoers 2>/dev/null; then
  echo "keepalived_script ALL=(ALL) NOPASSWD: /usr/bin/killall" >> /etc/sudoers
fi

# Enable VIP services (失敗しても kubeadm init まで進める)
set +e
systemctl enable keepalived --now || echo "[WARN] keepalived enable/start failed"
systemctl enable haproxy --now || echo "[WARN] haproxy enable/start failed"

# Reload VIP services (reload 失敗は致命ではない)
systemctl reload keepalived || systemctl restart keepalived || echo "[WARN] keepalived reload/restart failed"
systemctl reload haproxy || systemctl restart haproxy || echo "[WARN] haproxy reload/restart failed"
set -e

# Pull images first (失敗しても継続)
kubeadm config images pull || echo "[WARN] kubeadm config images pull failed; continuing"

# install k9s (最新安定版; 失敗しても継続)
K9S_VERSION="v0.50.13"
wget -qO- "https://github.com/derailed/k9s/releases/download/${K9S_VERSION}/k9s_Linux_amd64.tar.gz" | tar -zxvf - k9s && sudo mv -f ./k9s /usr/local/bin/ || echo "[WARN] k9s install failed; continuing"

# install velero client (失敗しても継続)
VELERO_VERSION="v1.17.0"
wget https://github.com/vmware-tanzu/velero/releases/download/${VELERO_VERSION}/velero-${VELERO_VERSION}-linux-amd64.tar.gz || true
[ -f velero-${VELERO_VERSION}-linux-amd64.tar.gz ] && tar -xvf velero-${VELERO_VERSION}-linux-amd64.tar.gz && sudo mv velero-${VELERO_VERSION}-linux-amd64/velero /usr/local/bin/ || echo "[WARN] velero install skipped"

# endregion

# Ends except first-control-plane
case $1 in
    k8s-cp-1)
        ;;
    k8s-cp-2|k8s-cp-3)
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
    --timeout 15m \
    --wait \
    --values https://raw.githubusercontent.com/craftzdev/homelab/"${TARGET_BRANCH}"/k8s-manifests/argocd-helm-chart-values.yaml
helm upgrade --install argocd-apps argo/argocd-apps \
    --version 2.0.2 \
    --namespace argocd \
    --timeout 15m \
    --wait \
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
