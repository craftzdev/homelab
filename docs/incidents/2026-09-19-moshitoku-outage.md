# モシトク接続障害 — 2026-09-19

## 状態

2026-09-19 06:35 JST 時点で公開サイトと Kubernetes の管理機能は復旧。
バックアップ先 PBS の容量不足は未解消。再発防止の暫定措置として
Proxmox の `kubernetes-daily-pbs` を `enabled=0` にしている。
容量の対処と安全なバックアップ動作確認後に再開が必要。

## 確認できた事実

- 公開サイトは HTTP 503、上流への接続拒否。復旧中に一時 502。
- k8s-2 / VM1002 が停止していた。
- k8s-1 / VM1001 は稼働していたが `/var` に I/O エラーがあり、
  etcd は `mkdir /var: file exists` で起動できず、kubelet も異常。
- k8s-3 の etcd は稼働していたが、他の2台に接続できず quorum を失っていた。
- 02:30 の VM1001 バックアップは PBS へのチャンク書き込みで
  `No space left on device (os error 28)`。VM1002 のバックアップは
  02:30:57 に `VM 1002 not running` で失敗。同時刻の qmeventd ログにも停止がある。
- PBS の非 root 向け空き容量は約 1.3 MiB。データストアと OS のルートFSが同居。
  ext4 予約2%があり、表示上の約98%使用でも backup ユーザーは書き込めない。
- Proxmox01 の local-lvm thin pool には余裕があり、ホスト側の当該プール満杯ではない。
- VM1001 の guest fsfreeze 状態は `thawed`。復旧前の QEMU scsi0 統計に
  failed write operations 102 件を確認。

バックアップ失敗と管理VM障害の発生時刻は一致している。
PBS の書き込み失敗がゲスト障害を引き起こした詳細な経路は未確定であり、
単独のソフトウェア不具合やディスク破損を確定原因とはしていない。

## 復旧操作（JST）

1. 06:27 頃、VM1002 を既存ディスクのまま起動。k8s-2 と k8s-3 で etcd quorum が復旧。
2. 復旧後の etcd スナップショットをローカルに退避。秘密情報を含むため Git 対象外。
   `_out/moshitoku-recovery-20260919/etcd-before-node1-reboot.snapshot`
   （153,763,872 bytes、revision 15,734,277、mode 0600）。
3. 06:29 頃、k8s-1 に通常再起動を要求。公開サイトは 06:29:31 に HTTP 200 を確認。
4. PBS が満杯のまま次回バックアップを開始しないよう、日次ジョブを一時停止。
   変更前設定は `_out/moshitoku-recovery-20260919/backup-job-before.json` に保存。
5. k8s-1 は停止処理の volumeFinalize で進まなくなったため、06:34 頃に
   Talos API の force reboot を実行。ディスク初期化・etcd restore は行っていない。
6. 06:35 頃、全6ノード Ready、k8s-1 running/ready、`/var` が読み取り可能。
   etcd 3メンバーの raft/applied index が一致し、エラーなし。
7. LinkShare の既存更新ジョブを1回ずつ実行し、Oisix と Trip.com が各 `updated=1`。
   楽天の通常更新ジョブも完了。アプリの CronJob は停止していない。

## 検証

- 公開 HTTPS トップページが複数回 HTTP 200。
- ブラウザでトップページ、楽天検索（127件）、比較詳細ページを確認。
- トップページに最新の Oisix 広告リンクを確認（取得時刻 06:34）。
- Kubernetes API VIP の readiness が正常。
- Argo CD moshitoku は Synced / Healthy。
- etcd は3台とも同じ leader、term 52、raft/applied index 16,448,471、エラーなし。
- 本対応でアプリのコード・イメージ・公開設定は変更していない。

## 残作業と注意

- ✅ PBS の容量対策は 2026-09-19 に完了した（`docs/pbs-capacity-2026-09-19.md`）。
  1 晩のバックアップが 97 GiB を消費しており、`keep-daily=7` は 431 GiB に対して
  一度も成立していなかった。PBS の重複排除が構造的に効いていないことが根本原因で、
  容量の追加削減（ext4予約・LV拡張）では届かない。
  対処は「worker の scsi0 を対象外にする」＋「保持を `keep-daily=3` にする」。
  使用率 100% → 79%、バックアップは再開済み（`enabled 1`）。
  移行期間の見通しと残る構造的課題は同文書 §5-4 / §7。
- 管理VMのバックアップを同時刻に実行する構成、ゲストfreeze、失敗時のI/O挙動を
  別途検証し、管理クラスタの quorum を同時に失わない方式にする。
- 旧ディスクへの切替、物理ホスト再起動、既存バックアップ削除は行っていない。
- 以前からのイメージ署名検証の Audit 警告は更新ジョブでも出たが、ジョブは正常完了。
  本障害の直接原因と区別して既存の署名対応を継続する。

一時調査用 SSH トンネル2本は終了済み。
