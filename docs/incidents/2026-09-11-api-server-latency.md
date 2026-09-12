# APIサーバーの遅延と KubeAPIErrorBudgetBurn（2026-09-11）

## 原因と影響

control-plane のOSディスクに含まれる etcd データが、各Proxmoxホストの
SATA SSD上の `local-zfs` に置かれていた。低いCPU使用率・十分な空き容量でも、
同期書き込みが秒単位で停止していた。

調査時の証拠:

- etcd leader（k8s-2）に `slow fdatasync: 11.293386389s` と
  `leader failed to send out heartbeat on time ... slow disk` を確認。
- APIサーバーからetcdへのアクセスp99は約3.7〜4.9秒。
- 直近6時間のAPI 5xxには、Lease更新の504が約701回、Lease取得の500が約549回。
  推定回数はPrometheusの `increase` による。
- controller-manager、scheduler等に過去のリーダー更新失敗・再起動があった。
- etcd DBは55〜57MB、実使用約22MB、3メンバーのRaft適用位置は一致していた。
  容量枯渇やDBの肥大化を示す状態ではなかった。
- 移行先NVMe上で8KiB書き込みとfsyncを30回行い、中央値1.5〜2.6ms、最大4.7ms。
  これはホスト上の短い試験であり、etcdのp99とは別の計測。

[etcd公式のハードウェア指針](https://etcd.io/docs/v3.6/op-guide/hardware/#disks)でも、
同期書き込みの遅延はリクエスト遅延・ハートビート失敗につながると説明されている。

## 対応

1. etcd snapshotを取得し、Talosが返すhash、revision、キー数、サイズを確認。
   snapshotはGit対象外の `_out/api-server-repair-20260911/` に0600で保存。
2. VMの `scsi0` を `local-zfs` から同一ホストのNVMe `local-lvm` へオンライン移行。
   VM 1001 → 1003 → 1002 の順で1台ずつ実施。各移行後にVM稼働とetcd同期を確認。
3. 1001と1003の移行後に、k8s-2のetcd leadershipを正常な手順で引き継ぎ。
   k8s-1がleaderとなり、Raft termは47から48へ進んだ。
4. 元のOSディスクは削除せず、各VMの `unused0` に保持。
5. `controlplane_os_datastore_id = "local-lvm"` をOpenTofuに追加。
   worker OSとLonghornデータ用のディスク配置は従来どおり。
6. 既存 `tofu@pve` と `tofu@pve!provider` に、既存の `TofuProvisioner` ロールを
   `/storage/local-lvm` の範囲で付与。`/` や別VMのプールには付与していない。
7. 構築前チェックにcontrol-plane用ストレージのactive確認を追加。
8. 読み取り専用の `scripts/check-api-server-health.py` と、10分ごとの消失確認を設定。

Talos設定の書き換えやVM再起動は行わない移行方式を使用した。
ディスク暗号化、fsync、etcdのタイミング設定、アラート条件は維持した。

## 検証

2026-09-11 10:24 JST（01:24 UTC）に確認:

| 項目 | 結果 |
| --- | --- |
| control-plane OSディスク | 1001/1002/1003すべて `local-lvm`。元ディスクは各 `unused0` に保持 |
| etcd | 3メンバーのRaft適用位置が一致、エラーなし、k8s-1がleader |
| APIサーバーのreadiness | 3台すべて `/readyz` が `ok` |
| Kubernetesノード | 全6台Ready |
| APIサーバーの再起動増分 | 0（累計はk8s-1=0、k8s-2=1、k8s-3=0で不変） |
| SLO対象APIの5xx | 直近5分で0件相当（rate=0） |
| etcdアクセスp99 / 直近5分 | k8s-1=20.7ms、k8s-2=72.5ms、k8s-3=24.6ms |
| etcdアクセスp99 / 直近2分 | k8s-1=21.0ms、k8s-2=24.1ms、k8s-3=24.6ms |
| Prometheus監視・ルール評価 | 3台のスクレイプと全4ルールの評価が新鮮・正常 |
| OpenTofu | validate成功。暗号化stateをrefresh-onlyで更新後、対象3VMのplanはすべてno-op |
| 設定ファイル | シェル構文確認と差分の空白チェック成功 |

5分窓には最後のノードの移行直前のデータが一部含まれるため、2分窓も確認した。
詳細な計測JSONと移行ログはGit対象外の `_out/api-server-repair-20260911/` に保存。
新規APIエラーは止まったが、履歴を含むwarningの自然解除は引き続き定期確認する。

## アラートが残る時間

warningの2ルールは、2時間/1日と6時間/3日の集計を組み合わせている。
直近のI/Oが改善しても、過去の失敗率がしきい値を下回るまでwarningは残る。
詳細は [KubeAPIErrorBudgetBurn runbook](https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubeapierrorbudgetburn/) を参照。

定期確認では、全3台のスクレイプと全4ルールの評価が新鮮で、全ルールがinactiveに
なった場合だけ消失を通知して確認を停止する。監視不能は復旧と扱わない。

## 保持ディスクについて

`unused0` の元ディスクは移行完了時点のコピーで、その後のetcd更新を含まない。
単純な差し戻し起動は行わず、必要ならetcdの復旧・再同期手順に従う。
使用中のデータ、旧ディスク、snapshotの削除はこの対応には含めない。
