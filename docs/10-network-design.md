# 10. ネットワーク設計

## 1. 既設 VLAN（変更しない）

| VLAN | サブネット | 用途 | ゲートウェイ | 備考 |
| --- | --- | --- | --- | --- |
| 10 | 172.16.10.0/24 | 管理 | 172.16.10.1 (IX2215) | Proxmox ホスト、PBS |
| 20 | 172.16.20.0/24 | （旧 Ceph public） | (L2 のみ) | **Ceph 廃止により未使用**。将来 TrueNAS / ストレージ用途に転用予定 |
| 30 | 172.16.30.0/24 | （旧 Ceph cluster） | (L2 のみ) | **Ceph 廃止により未使用** |
| 40 | 172.16.40.0/24 | VM / Kubernetes | 172.16.40.1 (IX2215) | Kubernetes ノードの主系 |

Proxmox ホストのブリッジ構成（実測）:

```
vmbr0 : enp3s0 (1GbE)  → VLAN10 untagged, 172.16.10.1{1,2,3}/24
vmbr1 : enp1s0 (10GbE) → vlan-aware, bridge-vids 20 30 40
        └ vmbr1.20 : 172.16.20.1{1,2,3}/24  (旧 Ceph public / 現在未使用)
        └ vmbr1.30 : 172.16.30.1{1,2,3}/24  (旧 Ceph cluster / 現在未使用)
```

## 2. Kubernetes ノードのアドレス設計

**1物理ノード = control-plane 1台 + worker 1台**の6 VM構成。
NIC は VLAN40 の 1 枚のみ（Ceph 廃止により VLAN20 への接続は不要になった）。
役割分離の理由は [ADR-0011](adr/0011-dedicated-worker-plane.md) を参照。

| ホスト名 | 役割 | 配置ノード | VMID | IP (VLAN40) | vCPU | RAM | OS Disk | Longhorn Disk |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `k8s-1` | control-plane | sv-proxmox-01 | 1001 | 172.16.40.11/24 | 8 | 24 GiB | 60 GiB | 300 GiB（配置停止） |
| `k8s-2` | control-plane | sv-proxmox-02 | 1002 | 172.16.40.12/24 | 8 | 24 GiB | 60 GiB | 300 GiB（配置停止） |
| `k8s-3` | control-plane | sv-proxmox-03 | 1003 | 172.16.40.13/24 | 8 | 24 GiB | 60 GiB | 300 GiB（配置停止） |
| `k8s-worker-1` | worker | sv-proxmox-01 | 1101 | 172.16.40.21/24 | 6 | 20 GiB | 60 GiB | 300 GiB |
| `k8s-worker-2` | worker | sv-proxmox-02 | 1102 | 172.16.40.22/24 | 6 | 20 GiB | 60 GiB | 300 GiB |
| `k8s-worker-3` | worker | sv-proxmox-03 | 1103 | 172.16.40.23/24 | 6 | 20 GiB | 60 GiB | 300 GiB |

**物理ノードあたりのKubernetes割当**: 14 vCPU / 44 GiB。
Proxmoxと補助VM用の余裕を残す。control-planeの既存Longhorn diskは退避済みで、
新規レプリカの配置は禁止している。

> VMを6台にしても物理障害ドメインは3つのままです。分離の目的は障害ドメインを
> 増やすことではなく、AIコード実行とLonghornの負荷・権限をcontrol-planeから
> 切り離すことです。control-planeは`NoSchedule`です。

### 予約アドレス

| アドレス | 用途 | 備考 |
| --- | --- | --- |
| 172.16.40.10 | **kube-apiserver VIP** | Talos 内蔵 VIP 機能（control-plane 間で自動フェイルオーバー） |
| 172.16.40.11 - .13 | Kubernetes control-plane | |
| 172.16.40.21 - .23 | Kubernetes worker | AI Worker / registry / Longhorn replicas |
| 172.16.40.24 - .29 | 追加worker用に予約 | |
| 172.16.40.200 - .239 | **Cilium L2 LoadBalancer プール** | `homelab.io/lan-exposed: "true"` ラベルを持つ Service にのみ払い出す |
| 172.16.40.240 - .254 | 予約（将来用） | |

### 割当済み LoadBalancer IP（固定）

| IP | サービス | 公開範囲 | 定義箇所 |
| --- | --- | --- | --- |
| 172.16.40.201 | Grafana | 宅内のみ | `kubernetes/infra/monitoring/values.yaml` |

> **Gateway には LoadBalancer IP を割り当てていません。**
> 当初は ingress-nginx を 172.16.40.200 で L2 公開していましたが、それは
> **Cloudflare Access を迂回できる第 2 の入口**を作ることを意味していました。
> VLAN40 に到達できる者が Host ヘッダを指定すれば、Access の認可も
> cloudflared の JWT 検証も通らずにアプリへ到達できます。
> 「外部公開は Cloudflare Tunnel のみ」を構成そのもので保証するため
> ClusterIP にしています。
>
> なお ingress-nginx は 2026 年 3 月に保守終了したため、
> Cilium の Gateway API へ移行しました（[ADR-0010](adr/0010-gateway-api.md)）。

> **固定 IP を使う理由**: Cilium の IP プールは動的に払い出せるが、
> ブックマークや監視設定が IP に依存するため、人が直接アクセスする
> サービスは固定する。新しく固定 IP を割り当てたら、必ずこの表に追記すること。

> **重要**: cloudflared は Kubernetes 内の Pod として動作し、Ingress へは
> ClusterIP / 内部 LB 経由で到達する。**LoadBalancer IP をインターネットに
> 露出させることは一切しない。**

## 3. クラスタ内部のアドレス

| 項目 | CIDR | 備考 |
| --- | --- | --- |
| Pod CIDR | `10.244.0.0/16` | Cilium が管理（kubernetes IPAM） |
| Service CIDR | `10.96.0.0/12` | Kubernetes 既定 |
| クラスタ DNS | `10.96.0.10` | CoreDNS |

いずれも 172.16.0.0/16 と重複しないことを確認済み。

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
  │ ② http://cilium-gateway-external.gateway.svc.cluster.local へ転送
  ▼
Gateway → HTTPRoute → アプリ Pod
```

**この経路で自宅側に開くポートは 0 個**。cloudflared からの outbound（UDP/443, TCP/443）
のみで成立する。

### 4.2 ストレージ（Longhorn）

```
Pod → Longhorn CSI → iSCSI（ノード内） → /var/mnt/longhorn（ローカルディスク）
                          └─ レプリカ同期 → VLAN40 経由で他ノードへ
```

Longhorn のレプリカ同期は VLAN40（10GbE）内で完結します。
外部のストレージネットワークは不要になりました。

### 4.3 運用端末 → クラスタ

```
MacBook ──Tailscale──▶ 172.16.10.0/24（Proxmox / PBS）
              └──────▶ 172.16.40.0/24（Talos API :50000, kube-apiserver :6443）
```

Talos API と kube-apiserver は **VLAN40 内および Tailscale 経由でのみ**到達可能とし、
Talos の ingressFirewall（`NetworkDefaultActionConfig` + `NetworkRuleConfig`）で
送信元 CIDR を明示的に制限する（[docs/20-security-design.md](20-security-design.md) 参照）。

## 5. ファイアウォール方針（Talos ingressFirewall）

既定は `block`。その上で、**信頼境界ごとに粒度を変えて**許可する。

| 送信元 | 許可範囲 | 理由 |
| --- | --- | --- |
| **VLAN40**（172.16.40.0/24） | TCP/UDP 全ポート | このセグメントには Kubernetes ノードしか居ない。etcd(2379-2380) / kubelet(10250) / KubePrism(7445) / Cilium(4240,4244,4245) / trustd(50001) / VXLAN(8472) など必要なポートが多岐にわたり、1 つでも漏らすとクラスタが起動しない。**同一信頼境界の内側は開ける**方が、穴だらけの許可リストを維持し続けるより確実で安全と判断した |
| **Pod CIDR**（10.244.0.0/16） | TCP/UDP 全ポート | Cilium が native routing のため Pod のトラフィックがホストのスタックを通る。遮断すると CNI が機能しない |
| **管理経路**（172.16.10.0/24, 100.64.0.0/10） | **50000/tcp と 6443/tcp のみ** | Talos API と kube-apiserver。到達できても mTLS のクライアント証明書が無ければ何もできないが、攻撃面を減らす |
| 上記以外 | **全て block** | Longhorn のレプリカ同期は VLAN40 内で完結するため影響を受けない |

> **設計判断の明示**: VLAN40 内を全ポート許可にしていることは、
> 「ノード間は必要最小ポートのみ」という一般的な推奨とは異なる。
> これは *意図的な選択* であり、ingressFirewall の設定ミスが
> 「SSH の無い OS への到達不能」という復旧困難な状態を招くリスクを
> 重く見た結果である。
>
> より厳格にする場合は、まず 1 ノードだけにポート限定版を適用し、
> `talosctl -n <node> health` と `cilium status` で問題が無いことを
> 確認してから展開すること。

> ICMP は管理経路からのみ許可している（`talosctl docs config` で
> NetworkRuleConfig が icmp に対応することを確認済み）。ping が通ることは
> 「ノードが生きているか」を証明書無しで確認できるため運用上の価値がある。

> ⚠️ **Service CIDR は送信元として指定しない。** ClusterIP は宛先アドレスで
> あり、DNAT 後もパケットの送信元は Pod IP のままである。送信元 CIDR に
> 書いても効果はなく、「許可しているつもり」の無効なルールが増えるだけになる。

## 6. DNS

| ゾーン | 解決先 | 用途 |
| --- | --- | --- |
| `home.arpa` | IX2215 (172.16.40.1) | 宅内ノードの名前解決 |
| `cluster.local` | CoreDNS (10.96.0.10) | クラスタ内部 |
| 外部公開ホスト名 | Cloudflare DNS（Tunnel の CNAME） | `tofu/20-cloudflare` が管理 |

Talos ノードの upstream DNS は `172.16.40.1` と `1.1.1.1` を設定する。
`machine.features.hostDNS` を有効化し、ノード上の名前解決を Talos の
内蔵 DNS キャッシュ経由に統一する。
