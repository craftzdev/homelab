#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

sudo install -d -m 0755 /usr/share/keyrings

curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg \
  | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list \
  | sudo tee /etc/apt/sources.list.d/tailscale.list >/dev/null

curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
printf '%s\n' 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null

sudo apt-get update
sudo apt-get install --yes \
  ca-certificates \
  cloudflared \
  curl \
  docker-compose-v2 \
  docker.io \
  jq \
  qemu-guest-agent \
  tailscale \
  ufw \
  unattended-upgrades

sudo install -m 0644 /dev/stdin /etc/sysctl.d/99-ai-gateway.conf <<'EOF'
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
EOF

sudo sysctl --system >/dev/null
sudo systemctl enable --now docker
sudo systemctl enable --now qemu-guest-agent
sudo systemctl enable --now tailscaled
sudo usermod -aG docker craftz
sudo install -d -o craftz -g craftz -m 0750 /opt/ai-business-gateway

sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 172.16.10.0/24 to any port 22 proto tcp comment 'management SSH'
sudo ufw allow in on tailscale0 comment 'Tailnet policy enforced by Tailscale'
sudo ufw allow 41641/udp comment 'Tailscale direct connections'
sudo ufw --force enable

docker --version
docker compose version
tailscale version
cloudflared --version
