# ADR-0007: Kubernetes ノードに VLAN40 + VLAN20 のデュアル NIC を持たせる

- **状態**: ⛔ **Superseded**（[ADR-0009](0009-drop-ceph-adopt-longhorn.md) により置き換え）

> **この ADR はもう有効ではありません。**
> Ceph 廃止により VLAN20 への接続が不要になったため、ここに書かれた判断は現在の構成には適用されません。
> 「当時どう考えたか」の記録として残しています。判断の前提が実測で
> 覆った経緯は [ADR-0009](0009-drop-ceph-adopt-longhorn.md) を参照してください。

- **当時の状態**: 承認済み
- **日付**: 2026-08-30

## 背景

Kubernetes ノードは VLAN40（172.16.40.0/24）に配置される。
一方、Ceph の public network は VLAN20（172.16.20.0/24）にある。
ceph-csi がボリュームをマウントするには、**ノードから Ceph MON/OSD への
到達性が必要**である。

実測した事実:

- Proxmox ホストは `vmbr1.20` / `vmbr1.30` を持ち、VLAN20/30 に IP を持つ
- Proxmox ホストは **VLAN40 に IP を持たない**（`vmbr1` は vlan-aware で
  `bridge-vids 20 30 40` だが、ホスト自身のインタフェースは 20/30 のみ）
- IX2215 が VLAN 間のルーティングを担っている（VLAN40 GW = 172.16.40.1 は疎通確認済み）

## 検討した選択肢

### A. シングル NIC（VLAN40 のみ）+ IX2215 経由のルーティング

```
Pod → ノード eth0 (VLAN40) → IX2215 → VLAN20 の Ceph
```

- ✅ 構成が単純。NIC が 1 枚で済む
- ❌ **全てのストレージ I/O が L3 ルータを通過する**。
  IX2215 のルーティング性能（実効数百 Mbps〜1Gbps 程度）が
  ストレージ帯域の上限になる。10GbE の Ceph が全く活かせない
- ❌ ルータがストレージ経路の単一障害点になる
- ❌ ルータの CPU 負荷が上がり、他の通信にも影響する

### B. デュアル NIC（VLAN40 + VLAN20）★採用

```
Pod → ノード eth1 (VLAN20) → L2 スイッチ → VLAN20 の Ceph
```

- ✅ **ストレージ I/O が 10GbE の L2 内で完結**する。ルータを経由しない
- ✅ ストレージトラフィックと通常トラフィックが物理的に分離され、
  互いに干渉しない
- ✅ Proxmox ホスト自身も同じ経路（`vmbr1.20`）で Ceph に接続しており、
  既存構成と一貫する
- ❌ Kubernetes ノードが Ceph public network に直接繋がる = 攻撃面が増える
- ❌ VM 定義とアドレス管理が少し複雑になる

### C. VLAN30（cluster network）にも接続する

- ❌ **絶対に行わない**。cluster network は OSD 間のレプリケーション専用であり、
  クライアントが触る必要は一切ない。接続すれば攻撃面が増えるだけ

## 決定

**B（VLAN40 + VLAN20 のデュアル NIC）を採用する。VLAN30 には接続しない。**

| NIC | VLAN | 用途 | アドレス |
| --- | --- | --- | --- |
| `eth0` | 40 | Kubernetes 全般（API, Pod, Service, LB） | 172.16.40.11-13 / .21-23 |
| `eth1` | 20 | Ceph public（ceph-csi のみが使う） | 172.16.20.41-43 / .51-53 |

`eth1` には**デフォルトゲートウェイを設定しない**。VLAN20 は L2 で
Ceph MON/OSD に到達できれば十分であり、そこから先へルーティングする必要はない。

## 「攻撃面が増える」ことへの対処

選択肢 B の唯一の欠点は、Kubernetes ノードが Ceph public network に
直接接続されることである。これに対して次の 3 点で対処する。

1. **cephx の最小権限**
   Kubernetes に渡すのは `client.k8s-rbd` / `client.k8s-cephfs` であり、
   `cephrdb_k8s` と `cephfs01:/volumes/csi` 以外には触れない
   （[ADR-0004](0004-ceph-csi.md)、[docs/30-storage-design.md](../30-storage-design.md) §4）。
   ノードから Ceph に到達できることと、Ceph を破壊できることは別である。

2. **Talos ingressFirewall による VLAN20 側インバウンドの遮断**
   `NetworkDefaultActionConfig.ingress = block` が全インタフェースに適用される。
   VLAN20 から Kubernetes ノードへ入る通信は既定で遮断される。
   Ceph への通信はノード発の outbound であり、影響を受けない。

3. **VLAN30 に接続しない**
   OSD 間レプリケーション経路には一切アクセスできない。
   仮に Kubernetes 側が完全に侵害されても、Ceph の内部通信は分離されている。

## 実装

`tofu/10-proxmox-talos` の VM 定義に 2 つの `network_device` を持たせ、
Talos の machine config で以下のように設定する。

```yaml
machine:
  network:
    interfaces:
      - deviceSelector: { busPath: "0*" }   # eth0 相当
        addresses: [ "172.16.40.11/24" ]
        routes:
          - network: 0.0.0.0/0
            gateway: 172.16.40.1
        vip: { ip: 172.16.40.10 }           # control-plane のみ
      - deviceSelector: { busPath: "0*" }   # eth1 相当（2枚目）
        addresses: [ "172.16.20.41/24" ]
        # デフォルトゲートウェイは設定しない
```

> 実装上は、Proxmox の VM で NIC の順序が MAC アドレス順・PCI バス順に
> 依存するため、`deviceSelector` は `hardwareAddr`（MAC）で指定する。
> OpenTofu 側で MAC を明示的に払い出し、machine config に渡す。
