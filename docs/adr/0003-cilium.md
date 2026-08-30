# ADR-0003: CNI に Cilium を採用し、kube-proxy を置き換える

- **状態**: 承認済み
- **日付**: 2026-08-30

## 背景

CNI の選択は、ネットワークポリシーの表現力（脅威 T2/T3 への対処能力）と
LoadBalancer IP の払い出し方式を同時に決める。

## 検討した選択肢

| 選択肢 | NetworkPolicy | L7 制御 | 可視化 | LB IP | 評価 |
| --- | --- | --- | --- | --- | --- |
| Flannel | ✗（別途 Calico 等が必要） | ✗ | ✗ | 別途 MetalLB | セキュリティ要件を満たさない |
| Calico | ✓ | △（限定的） | △ | 別途 MetalLB or Calico BGP | 有力。ただし L7 と可視化で劣る |
| **Cilium** ★採用 | ✓ | **✓（HTTP/DNS/Kafka）** | **✓ Hubble** | **✓ L2 Announcement 内蔵** | 採用 |

## 決定

**Cilium v1.20.1** を採用し、以下の構成とする。

```yaml
kubeProxyReplacement: true      # kube-proxy を完全に置き換える
k8sServiceHost: localhost       # KubePrism 経由
k8sServicePort: 7445
routingMode: native             # VLAN40 内の L2 なのでカプセル化不要
ipam.mode: kubernetes
securityContext.capabilities:   # Talos 用の必須設定
  ciliumAgent: [CHOWN, KILL, NET_ADMIN, NET_RAW, IPC_LOCK, SYS_ADMIN, SYS_RESOURCE, PERFMON, BPF, DAC_OVERRIDE, FOWNER, SETGID, SETUID]
cgroup.autoMount.enabled: false # Talos は /sys/fs/cgroup を既にマウント済み
cgroup.hostRoot: /sys/fs/cgroup
l2announcements.enabled: true   # MetalLB の代替
hubble.relay.enabled: true
hubble.ui.enabled: true
```

### 採用理由

1. **eBPF による L3-L7 ポリシー**
   「この Pod は cloudflared からの HTTP GET `/v1/*` のみ受け付ける」という
   粒度のポリシーが書ける。従来の L3/L4 NetworkPolicy より攻撃面を絞れる。

2. **Hubble による通信の可視化**
   default-deny を敷いたとき、最大の運用課題は「何が落ちているか分からない」
   ことである。Hubble は drop されたフローを送信元・宛先・理由付きで見せる。
   **default-deny を実運用可能にするための必須要件**として評価した。

3. **MetalLB が不要になる**
   L2 Announcement 機能により、`CiliumLoadBalancerIPPool` +
   `CiliumL2AnnouncementPolicy` で LoadBalancer IP を払い出せる。
   コンポーネントが 1 つ減る = 攻撃面と運用対象が減る。

4. **kube-proxy 置換による性能と可視性**
   iptables のルール爆発が無くなり、Service の解決が eBPF マップで完結する。
   Talos 側は `cluster.proxy.disabled: true` で kube-proxy を作らない。

5. **Talos との組み合わせが公式にサポートされている**
   Talos のドキュメントに Cilium の推奨 values が記載されており、
   `cni: none` + `proxy.disabled` の構成が標準的。

### KubePrism を使う理由

Cilium が kube-proxy を置き換えると、Cilium 自身が kube-apiserver に
到達する経路が必要になる（鶏と卵）。Talos の **KubePrism**（`localhost:7445`）は
各ノード上でローカルに動く apiserver へのロードバランサであり、
これを `k8sServiceHost` に指定することで、

- VIP（172.16.40.10）が単一障害点にならない
- CNI 起動前でも apiserver に到達できる

という利点が得られる。

## default-deny ポリシーの方針

```yaml
# 全 namespace に適用する基本ポリシー
# 1. 同一 namespace 内の通信は許可
# 2. DNS（kube-dns）への通信は許可
# 3. それ以外の ingress / egress は拒否
```

これに対し、必要な通信を `CiliumNetworkPolicy` で個別に許可する。
公開アプリの ingress は `fromEndpoints: cloudflared` に限定する。

## トレードオフ

- ❌ eBPF に起因する問題の切り分けには専門知識が要る
  → Hubble と `cilium status` / `cilium monitor` を運用手順に含める
- ❌ カーネルバージョン依存がある
  → Talos v1.13 系のカーネル（6.12+）で要件を十分満たす
- ❌ 機能が多く、設定を誤ると穴になる
  → values.yaml を最小限に保ち、有効化する機能を明示的に列挙する
