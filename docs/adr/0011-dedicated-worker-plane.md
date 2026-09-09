# ADR-0011: control-plane 3台とworker 3台を分離する

- **状態**: 承認済み
- **日付**: 2026-09-06
- **決定者**: クラフト
- **影響**: [ADR-0009](0009-drop-ceph-adopt-longhorn.md) のノード集約判断を一部置換

## 背景

3台のcontrol-plane兼worker構成でもetcdクォーラムとLonghorn 3レプリカは
成立する。しかしAI Workerは生成コードの実行、ブラウザ操作、テストを行い、
CPU・メモリ・一時ディスクを大きく消費しうる。アプリ負荷がAPI serverやetcdと
同居すると、物理ホスト障害とは別に、リソース枯渇の影響範囲が広がる。

Kubernetesの本番環境ガイドはcontrol-planeをワークロードから分離する構成を
推奨し、kubeadmのHAリファレンスも3台以上のcontrol-planeと3台以上のworkerを
例示する。ただし「3+3」はあらゆる規模で必須の固定則ではない。今回採用する
理由はノード数の形式ではなく、信頼境界とリソース競合の分離である。

## 決定

各Proxmoxホストにcontrol-plane VMとworker VMを1台ずつ配置する。

| Proxmox | control-plane | worker |
| --- | --- | --- |
| `sv-proxmox-01` | `k8s-1` / `172.16.40.11` | `k8s-worker-1` / `172.16.40.21` |
| `sv-proxmox-02` | `k8s-2` / `172.16.40.12` | `k8s-worker-2` / `172.16.40.22` |
| `sv-proxmox-03` | `k8s-3` / `172.16.40.13` | `k8s-worker-3` / `172.16.40.23` |

- control-planeは`NoSchedule`とし、一般ワークロードを載せない。
- AI Worker、内部レジストリ、Longhornのデータレプリカと管理コンポーネントは
  `homelab.craftz.dev/workload-plane=true` のworkerだけへ配置する。
- Longhornの3レプリカは3台のworkerへ1つずつ配置する。
- 物理障害ドメインは依然3つであり、VMを6台にしても物理ホスト2台同時障害へ
  耐えられるようになるわけではない。

## 構成管理の責務分離

| 層 | 管理手段 | 管理するもの |
| --- | --- | --- |
| Proxmox VM | OpenTofu | VMID、CPU/RAM、ディスク、NIC、Cloud-Init drive |
| Cloud-Init / NoCloud | Proxmox `initialization` | 初回Talos API到達用のIP・gateway・DNSのみ |
| Talos OS | Talos machine config API | 永続network、hostname、暗号化、kubelet、証明書、node label |
| Kubernetes基盤 | Helm / Kustomize / Argo CD | Cilium、Longhorn、Tailscale等のmiddleware |
| 生成済みLonghornリソース | `reconcile-longhorn-worker-plane.sh` | 既存レプリカの安全な退避とworker selectorの再調整 |

TalosにはSSH、POSIX shell、Python、パッケージ管理が無いため、ノード内へ
Ansibleを接続して変更する方式は採用しない。OpenTofu/Talos APIは差分適用、
Helm/Kubernetes APIは宣言的reconcileを提供し、同じ冪等性の目的をTalosの
セキュリティモデルを崩さずに満たす。

machine configにはクラスタCA秘密鍵等が含まれるためCloud-Init user-dataへは
保存しない。TLSで保護されたTalos APIから適用し、暗号化されたSTATE
パーティションへ保存する。内部レジストリの公開CA証明書だけはGit管理し、
秘密鍵はKubernetes Secretだけに置く。

## 結果

- AI実行負荷がcontrol-planeの可用性へ直接波及しにくくなる。
- worker VMはcontrol-plane/etcdに触れず個別に再作成できる。
- 3物理ホストに各役割を分散するため、1ホスト障害時もcontrol-plane 2台と
  worker 2台が残る。
- VM数とメモリ使用量は増える。物理障害ドメイン数は増えない。

## 参考

- [Kubernetes: Production environment](https://kubernetes.io/docs/setup/production-environment/)
- [Kubernetes: Creating Highly Available Clusters with kubeadm](https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/high-availability/)
- [Kubernetes: Control plane node isolation](https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/create-cluster-kubeadm/)
- [Talos: NoCloud platform](https://docs.siderolabs.com/talos/v1.13/platform-specific-installations/cloud-platforms/nocloud)
- [Talos: Acquiring machine configuration](https://docs.siderolabs.com/talos/v1.13/configure-your-talos-cluster/system-configuration/acquire)
- [Ansible: Managed node requirements](https://docs.ansible.com/projects/ansible/latest/installation_guide/intro_installation.html)
