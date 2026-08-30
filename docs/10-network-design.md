# 10. ネットワーク設計

## 1. 既設 VLAN（変更しない）

| VLAN | サブネット | 用途 | ゲートウェイ | 備考 |
| --- | --- | --- | --- | --- |
| 10 | 172.16.10.0/24 | 管理 | 172.16.10.1 (IX2215) | Proxmox ホスト、PBS |
| 20 | 172.16.20.0/24 | Ceph public | (L2 のみ / ルータ経由可) | Proxmox ホストが `vmbr1.20` を保持 |
| 30 | 172.16.30.0/24 | Ceph cluster | (L2 のみ) | OSD 間レプリケーション専用。**K8s からは触れない** |
| 40 | 172.16.40.0/24 | VM / Kubernetes | 172.16.40.1 (IX2215) | Kubernetes ノードの主系 |

Proxmox ホストのブリッジ構成（実測）:

```
vmbr0 : enp3s0 (1GbE)  → VLAN10 untagged, 172.16.10.1{1,2,3}/24
vmbr1 : enp1s0 (10GbE) → vlan-aware, bridge-vids 20 30 40
        └ vmbr1.20 : 172.16.20.1{1,2,3}/24  (Ceph public)
        └ vmbr1.30 : 172.16.30.1{1,2,3}/24  (Ceph cluster)
```

## 2. Kubernetes ノードのアドレス設計

各ノードは **2 枚の NIC** を持つ。理由は [ADR-0007](adr/0007-dual-nic-topology.md) を参照。

| ホスト名 | 役割 | 配置ノード | VMID | eth0 (VLAN40) | eth1 (VLAN20 / Ceph) | vCPU | RAM | Disk |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `k8s-cp-1` | control-plane | sv-proxmox-01 | 1001 | 172.16.40.11/24 | 172.16.20.41/24 | 4 | 8 GiB | 60 GiB |
| `k8s-cp-2` | control-plane | sv-proxmox-02 | 1002 | 172.16.40.12/24 | 172.16.20.42/24 | 4 | 8 GiB | 60 GiB |
| `k8s-cp-3` | control-plane | sv-proxmox-03 | 1003 | 172.16.40.13/24 | 172.16.20.43/24 | 4 | 8 GiB | 60 GiB |
| `k8s-wk-1` | worker | sv-proxmox-01 | 1101 | 172.16.40.21/24 | 172.16.20.51/24 | 6 | 20 GiB | 120 GiB |
| `k8s-wk-2` | worker | sv-proxmox-02 | 1102 | 172.16.40.22/24 | 172.16.20.52/24 | 6 | 20 GiB | 120 GiB |
| `k8s-wk-3` | worker | sv-proxmox-03 | 1103 | 172.16.40.23/24 | 172.16.20.53/24 | 6 | 20 GiB | 120 GiB |

**物理ノードあたりの割当**: 10 vCPU / 28 GiB（16 vCPU / 58 GiB に対し十分な余裕を残す）。
ノード 1 台が停止しても、残り 2 台で全 Pod を収容できる余力を確保する意図。

### 予約アドレス

| アドレス | 用途 | 備考 |
| --- | --- | --- |
| 172.16.40.10 | **kube-apiserver VIP** | Talos 内蔵 VIP 機能（control-plane 間で自動フェイルオーバー） |
| 172.16.40.11 - .13 | control-plane ノード | |
| 172.16.40.21 - .23 | worker ノード | |
| 172.16.40.200 - .239 | **Cilium L2 LoadBalancer プール** | Service type=LoadBalancer に払い出す |
| 172.16.40.240 - .254 | 予約（将来用） | |

### 割当済み LoadBalancer IP（固定）

| IP | サービス | 公開範囲 |
| --- | --- | --- |
| 172.16.40.200 | `ingress-nginx-internal` | 宅内のみ。cloudflared の origin もここを向く |

> **重要**: cloudflared は Kubernetes 内の Pod として動作し、Ingress へは
> ClusterIP / 内部 LB 経由で到達する。**LoadBalancer IP をインターネットに
> 露出させることは一切しない。**

## 3. クラスタ内部のアドレス

| 項目 | CIDR | 備考 |
| --- | --- | --- |
| Pod CIDR | `10.244.0.0/16` | Cilium が管理（kubernetes IPAM） |
| Service CIDR | `10.96.0.0/12` | Kubernetes 既定 |
| クラスタ DNS | `10.96.0.10` | CoreDNS |

いずれも VLAN20/30 および 172.16.0.0/16 と重複しないことを確認済み。

## 4. 通信経路

### 4.1 外部 SaaS → 宅内サービス

```
Workers (SaaS)
  │ HTTPS + CF-Access-Client-Id / CF-Access-Client-Secret
  ▼
Cloudflare Access（Service Token を検証、JWT を発行）
  │ Cf-Access-Jwt-Assertion 付きで転送
  ▼
Cloudflare Tunnel エッジ
  │ ※ cloudflared が張った outbound QUIC/443 コネクション上を流れる
  ▼
cloudflared Pod（k8s 内、2 レプリカ）
  │ ① originRequest.access で JWT を再検証（多層防御）
  │ ② http://ingress-nginx-internal.ingress-nginx.svc.cluster.local へ転送
  ▼
Ingress → Service → アプリ Pod
```

**この経路で自宅側に開くポートは 0 個**。cloudflared からの outbound（UDP/443, TCP/443）
のみで成立する。

### 4.2 Kubernetes ノード → Ceph

```
Pod（ceph-csi）→ ノードの eth1 (172.16.20.5x) → L2 スイッチ → Ceph MON/OSD (172.16.20.1x)
```

L3 ルータ（IX2215）を経由しないため、ストレージ I/O がルータの転送性能に律速されない。

### 4.3 運用端末 → クラスタ

```
MacBook ──Tailscale──▶ 172.16.10.0/24（Proxmox / PBS）
              └──────▶ 172.16.40.0/24（Talos API :50000, kube-apiserver :6443）
```

Talos API と kube-apiserver は **VLAN40 内および Tailscale 経由でのみ**到達可能とし、
Talos の ingressFirewall（`NetworkDefaultActionConfig` + `NetworkRuleConfig`）で
送信元 CIDR を明示的に制限する（[docs/20-security-design.md](20-security-design.md) 参照）。

## 5. ファイアウォール方針（Talos ingressFirewall）

| ポート | 用途 | 許可する送信元 |
| --- | --- | --- |
| 50000/tcp | Talos API | 172.16.40.0/24, 172.16.10.0/24, 100.64.0.0/10 (Tailscale CGNAT) |
| 6443/tcp | kube-apiserver | 172.16.40.0/24, 172.16.10.0/24, 100.64.0.0/10 |
| 2379-2380/tcp | etcd | 172.16.40.11-13 のみ（control-plane 相互） |
| 10250/tcp | kubelet | 172.16.40.0/24 のみ |
| 4240/tcp, 8472/udp 等 | Cilium | 172.16.40.0/24 のみ |
| 上記以外 | — | **既定で block** |

> Talos の `ingressFirewall` は VLAN20 側（Ceph）にも適用される。Ceph への
> **アウトバウンド**は制限対象外だが、VLAN20 からノードへの**インバウンド**は
> 既定 block により遮断される。

## 6. DNS

| ゾーン | 解決先 | 用途 |
| --- | --- | --- |
| `home.arpa` | IX2215 (172.16.40.1) | 宅内ノードの名前解決 |
| `cluster.local` | CoreDNS (10.96.0.10) | クラスタ内部 |
| 外部公開ホスト名 | Cloudflare DNS（Tunnel の CNAME） | `tofu/20-cloudflare` が管理 |

Talos ノードの upstream DNS は `172.16.40.1` と `1.1.1.1` を設定する。
`machine.features.hostDNS` を有効化し、ノード上の名前解決を Talos の
内蔵 DNS キャッシュ経由に統一する。
