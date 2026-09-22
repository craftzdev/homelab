# PBS 容量枯渇の再発対策 — 2026-09-22

## 対象と原因

PBS `172.16.10.51` の `gateway-backup`。476.9 GiB SSD 上の root ext4
（ファイルシステム約444 GiB）を OS と datastore が共用している。
開始時は空き約1.2 MiB、worker-2（VM 1102）の9月22日バックアップが容量不足で失敗。

`keep-daily=3` とバックアップ主体の prune 権限は既に有効だった。
保持期間を設定するだけでは、削除された index が参照していたチャンクは消えない。
prune と GC が両方 `daily`（00:00 JST）だったため、GC の既定の安全猶予
24時間5分に対して翌日同時刻の GC が5分早く、さらに次の GC まで残る。
以前の worker OS バックアップ、失敗で世代交代できない古い VM 1102、
この回収待ちが重なり、物理使用量が増加した。

公式の [Prune / Garbage Collection の説明](https://pbs.proxmox.com/docs/maintenance.html)
に従い、チャンクの手動削除や GC の安全猶予短縮は行わない。
ext4 の root 向け予約領域（2%）も維持する。

## 適用内容

| 項目 | 設定 |
|---|---|
| PVE 日次バックアップ | 02:30 JST、6台、snapshot、50 MiB/s/ノード |
| PVE / PBS 保持 | `keep-daily=3` のみ |
| PBS prune | 毎日04:00 JST |
| PBS GC | 毎日00:15 / 06:15 / 12:15 / 18:15 JST |
| ジョブ開始時の容量ガード | 空き80 GiB以上を要求 |
| 個別 VM 開始時の容量ガード | 空き16 GiB以上を要求 |
| 監視追加 | GC完了が12時間以上ない / 空き80 GiB未満 |

`keep-daily=3` は成功したバックアップがある日を3日分保持する。
厳密な72時間 TTL ではない。VM 1200 の唯一の手動バックアップも維持する。

`scripts/reconcile-pbs-maintenance.sh --apply` で PBS の保持と時刻を再適用できる。
引数なしは読み取りのみ。変更前設定は PBS の root 専用ディレクトリへ保存する。

`scripts/reconcile-pbs-kubernetes-backup.sh --apply` は容量ガードを3台の PVE に配布し、
共有バックアップジョブへ `/usr/local/libexec/homelab-pbs-capacity-guard` を設定する。
ガードは標準 hook の `STOREID` を使い、PBS が無効・API不明・空き不足なら
書き込み前に失敗を返す。cleanup hook は妨げない。
実行中の全書き込みを制限する仕組みではなく、容量計画と GC に追加する防御である。

## 回収と容量計画

12:32 JST の標準 GC は正常終了し、57.634 GiB（24,243 chunks）を回収した。
apt の再取得可能なキャッシュ625 MiBも削除し、空きは約59 GiBへ回復。
この時点の参照中チャンクは248.796 GiB、回収猶予中は123.872 GiB。
猶予中の容量はまだ空き領域ではなく、安全条件を満たした後の定期 GC で回収される。

直近2日間の新規チャンクは全ノード合計で約49 GiB/日。
3世代の参照中データだけでなく、回収猶予中の1〜2日分と次回バックアップ分も必要。
80 GiB の開始条件は現在の実測増分に約31 GiBの余裕を加えたもの。
データ量や増分が増えれば SSD の増設・datastore 分離が必要になる。
不足を放置して OS まで満杯にする代わりに、ジョブ失敗とアラートで検出する。

VM 1102 は12:35 JSTに snapshot モードで再取得を開始した。
稼働 VM の停止・再起動は不要。再取得結果は完了後に追記する。

## 検証と保管

- 容量不足、API失敗、不正レスポンスで中止する unittest 5件。
- 変更した shell script の ShellCheck / `bash -n`、監視 Kustomize build。
- PrometheusRule の Kubernetes server-side dry-run。
- 全 PVE ノードに hook 配布済み。現実の空き容量で batch 拒否・単体許可を確認。
- PBS の保持と GC 時刻を API から再読込して一致を確認。
- 初期設定と snapshot / GC メタデータは PBS `/root/pbs-capacity-20260922/` に保管。
- PBS のバックアップ本体・秘密鍵を Mac に退避する操作は行っていない。
