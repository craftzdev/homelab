#!/usr/bin/env bash
# Run on ai-gateway-01: sudo bash install.sh (with this directory alongside it).
set -euo pipefail
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
test "$(hostname)" = ai-gateway-01
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install --yes --no-install-recommends prometheus-node-exporter
install -d -m 0755 /usr/local/lib/ai-gateway-monitoring /var/lib/prometheus/node-exporter
install -m 0644 "$source_dir/collect-gateway-metrics.py" /usr/local/lib/ai-gateway-monitoring/
install -m 0644 "$source_dir/ai-gateway-metrics.service" "$source_dir/ai-gateway-metrics.timer" /etc/systemd/system/
install -d -m 0755 /etc/systemd/system/prometheus-node-exporter.service.d
cat > /etc/systemd/system/prometheus-node-exporter.service.d/homelab.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/prometheus-node-exporter --web.listen-address=172.16.40.30:9100 --collector.textfile.directory=/var/lib/prometheus/node-exporter
EOF
# Prometheus egress is SNATed to its Kubernetes node. No subnet-wide access.
for suffix in 11 12 13 21 22 23; do
  ufw allow from "172.16.40.$suffix" to 172.16.40.30 port 9100 proto tcp comment 'Kubernetes Prometheus metrics'
done
systemctl daemon-reload
systemctl enable --now prometheus-node-exporter
systemctl restart prometheus-node-exporter
systemctl start ai-gateway-metrics.service
systemctl enable --now ai-gateway-metrics.timer
