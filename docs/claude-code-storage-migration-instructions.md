# Claude Codeへの指示書：既存NVMeを利用したDB・workerストレージ改善

作成日：2026-09-13（JST）

## 依頼と目的

既存の3筐体を使い、Webサービス用PostgreSQLとCIランナーのディスク性能を改善してください。機器は購入せず、control 3台＋worker 3台の構成と、各PostgreSQLクラスタの3インスタンス構成を維持します。

この指示書は調査だけでなく、以下の移行・設定変更・検証を実施するための依頼です。ただし、記載した観測値は作成時点のものです。実機を再確認し、各段階の前提条件を満たしてから進めてください。未検証のコマンドを推測して実行しないでください。

今回の指示書作成では、本番の移行・設定変更は実施していません。

## 作業場所と参照先

- homelabリポジトリ：`/Users/craftz/Developments/github/homelab`
- moshitokuリポジトリ：`/Users/craftz/Developments/github/moshitoku`
- kubeconfig：homelab内の `_out/kubeconfig`
- Talosconfig：homelab内の `_out/talosconfig`
- Grafana：<https://grafana.tailb6c7d.ts.net/>
- Proxmox：`ssh root@172.16.10.11`、`.12`、`.13`。作成時点では鍵認証が利用可能。
- PBS：`ssh root@172.16.10.51`。作成時点では鍵認証が利用可能。
- 既存調査：`docs/worker-1-disk-investigation-2026-09-13.md`

最初に適用される `AGENTS.md` 等の作業規約と、両リポジトリの作業ツリー差分を確認してください。他作業の変更を上書き・破棄・混入させないでください。認証情報、kubeconfig、Talosconfig、Terraform state、Secretの内容を報告書やGitに含めないでください。

主な変更候補：

- `tofu/10-proxmox-talos/vms.tf`、`variables.tf`、`machine-config.tf`、`talos.tf`
- `talos/patches/worker.yaml.tftpl`
- `kubernetes/infra/longhorn/values.yaml`
- `kubernetes/infra/longhorn/namespace-and-storageclass.yaml`
- `kubernetes/infra/umami/database.yaml`
- moshitoku側の `deploy/kubernetes/database.yaml`
- 必要に応じて監視、バックアップ、CIスケジュールの管理元

## 確認済みの構成

| 物理ホスト | 管理IP | control VM | worker VM | DBの現在の役割 |
|---|---|---|---|---|
| sv-proxmox-01 | 172.16.10.11 | k8s-1 / 1001 | k8s-worker-1 / 1101 | 両DBの待機系 |
| sv-proxmox-02 | 172.16.10.12 | k8s-2 / 1002 | k8s-worker-2 / 1102 | 両DBの待機系 |
| sv-proxmox-03 | 172.16.10.13 | k8s-3 / 1003 | k8s-worker-3 / 1103 | 両DBの主系 |

- controlのIPは `172.16.40.11–13`、workerは `172.16.40.21–23`。
- Kubernetes v1.34.3、Talos v1.13.9。ローカルtalosctlはサーバーより新しいため互換性を確認。
- LonghornのVolumeに記録されたエンジンイメージは v1.12.1、対象Volumeはv1 data engine。chart、manager、engineそれぞれの実バージョンを確認してから、そのバージョンの公式手順を使用する。
- controlには `node-role.kubernetes.io/control-plane:NoSchedule` taintがある。通常のアプリやDBをcontrolへ移す計画ではない。
- 各controlのOS 60GiBは、すでにNVMeの `local-lvm` に配置済み。
- 各workerのOS 60GiBとLonghorn用300GiBは、SATA/ZFSの `local-zfs` に配置されている。実機のディスク接続を再確認する。
- 各ホストの `pve/data` thin poolは約337.86GiB、Data%は17.76%。データ領域の未使用分は概算278GiB。thin metadata、ホスト側の他用途、予約容量も別途確認する。

### PostgreSQL

| namespace / cluster | 主系 | worker-1 | worker-2 | worker-3 |
|---|---|---|---|---|
| analytics / umami-postgres | umami-postgres-1 | instance 3 | instance 2 | instance 1 |
| moshitoku / moshitoku-postgres | moshitoku-postgres-1 | instance 2 | instance 3 | instance 1 |

- 両クラスタとも3/3 Ready。各インスタンスのPVCは10GiB。
- StorageClassは `longhorn-cnpg-retain`。各PVCのLonghornレプリカ数は1、`dataLocality: best-effort`、reclaim policyはRetain。
- 6個のDB Volumeはhealthy。冗長化はPostgreSQLの3インスタンスで担う設計であり、恒久的にLonghornまで3重化して9コピーに増やさない。
- **重要：ライブのCNPG設定は `minSyncReplicas: 0`、`maxSyncReplicas: 0`、新しい `postgresql.synchronous` 設定は未指定。同期複製を保証していると扱わない。** StorageClassのコメントには同期複製との記述があるが、実設定と一致していない。SQLで実際の `synchronous_standby_names`、`synchronous_commit`、`pg_stat_replication` を確認し、必要ならコメントを実態に合わせる。
- 今回、性能改善を理由に耐久性設定を弱めない。同期複製への設計変更も、移行とは分けて影響を報告する。
- CNPGのバックアップにはcompletedの記録があり、Umamiは2026-09-12 18:30 UTC、moshitokuは同日19:00 UTCの完了を確認済み。ただし、これだけで復元可能性を保証しない。

### ディスク遅延の調査結果

worker-1の直接の負荷源として、PrometheusのLonghornレプリカ再構築と、その失敗・再試行を確認済み。OSとLonghornが同じSATA SSD/ZFSプールを使うため、ランナーやOSにも遅延が波及していた。

- 対象Volume：`pvc-364e7ae1-a18b-4dd1-9162-388d0c803454`
- namespace：`monitoring`
- PVC：`prometheus-kube-prometheus-stack-prometheus-db-prometheus-kube-prometheus-stack-prometheus-0`
- 指定レプリカ数3、状態degraded。直近のエンジン状態ではRWレプリカは1個のみ、rebuildStatusは空だった。
- 直近で正常なレプリカ名：`pvc-364e7ae1-a18b-4dd1-9162-388d0c803454-r-b78ec33b`。以前の観測ではworker-3に存在。**名前・配置・正常性は必ず再確認する。**
- Grafana、Harbor DB、Tempo、AI control-planeのVolumeにもdegradedを確認。DB移行前に関連する複製不足とディスク競合を整理する。
- 全体の同時再構築数は既にノードごと1に制限済み。設定を再適用するだけで改善したと扱わない。
- v1エンジンにv2専用の再構築帯域制限を設定して効果があると扱わない。
- SMART/ZFSの明確なメディアエラーは当時未確認。SSD故障、autotrim未設定、DB主系の偏りを根本原因と断定しない。

## 目標構成

各物理筐体で、既存NVMeを次のように使用します。

| 用途 | 目安 |
|---|---:|
| control OS（既存） | 60GiB |
| worker OS（SATAから移行） | 60GiB |
| workerのDB専用仮想ディスク（新設） | 64GiB |
| 上記確保後のthin poolデータ領域の余裕 | 概算154GiB |

64GiBは計画値です。DBの現容量、成長量、Longhornの予約・空き容量制限、移行中の一時コピーを含めて収まることを確認してください。thin provisionの仮想空きだけで判断しないでください。

- DB用NVMeディスクを各workerへ1本追加し、既存SATAとは別マウント・別Longhornディスクとして登録。
- DB専用タグと新しいStorageClass（例：`longhorn-cnpg-nvme-retain`）で保存先を限定。
- `Retain`、Longhornレプリカ数1、必要な既存mkfs設定を維持。NVMeへ一般の監視・ログVolumeが勝手に配置されないよう、タグ未指定Volumeのスケジューリング挙動も確認する。
- 各DBの3インスタンスと、その実データが別々の物理筐体に存在することを検証。Podの配置だけでは判定しない。`best-effort` はデータのローカル配置を保証しない。
- 主系の配置目標はmoshitokuをworker-3、Umamiをworker-2とする。実負荷に応じて変更可能。障害時の自動切り替えを妨げる固定をせず、主系が同居した場合は監視で分かるようにする。
- 大容量の監視・ログデータは当面SATAに残す。workerのLonghorn用300GiB全体をNVMeへ移さない。

## 実施手順と進行条件

### 0. 現状保全・測定・復旧準備

1. 実バージョン、VMディスク構成、Talosの認識デバイス、Longhornのノード・ディスク・Volume・レプリカ、CNPGとアプリの状態を確認する。
2. Proxmox構成、Talos設定、関係する宣言設定とライブ設定の復旧用記録を安全な場所へ保存する。Secret等は出力やコミットに混入させない。
3. Argo CDとOpenTofuの管理元を特定する。一時的なライブ変更が自動で戻されないよう、変更と同期の順序を決める。同期を止めた場合は対象・元設定・復帰手順を記録する。
4. DBの新しいバックアップとWAL保存を確認し、可能なら隔離した復元先で復元・簡単な読み取りを検証する。クラスタ内MinIOだけを、物理障害から独立したバックアップと扱わない。PBSの空き容量も再確認する。以前の観測では使用率約91%で、余裕が少ない。
5. Webの代表的な応答時間・エラー率、DBの書き込み/クエリ待ち・複製遅延、全筐体のSATA/NVMe待ち時間、etcd/APIの状態を記録する。本番に破壊的なfio/pgbenchを実行しない。Webが未デプロイなら、未測定と明記してDB側の測定を行う。
6. 次段階へ進む判断基準、悪化時の停止基準、書き込み停止・再開方法を記録する。ノードNotReady、DB複製異常、継続するWebエラー増加、etcd警告、コピー失敗があれば新しい移行を開始しない。

### 1. SATA上の再構築ループと複製不足を解消

1. Prometheusの唯一の正常コピーを特定し、削除・退避・そのホストの再起動の対象から外す。
2. ログ、実容量と予約容量、Longhornの配置制約、タイムアウト、ネットワーク、ディスク待ちから、複製が増えない原因を調べる。単に再試行を連打しない。
3. 健全な保存先へ2個目の正常コピーを確保し、次に指定数まで戻す。他のdegraded Volumeも負荷を見ながら1件ずつ復旧する。worker-2が候補だが、容量・配置条件を確認せず決め打ちしない。
4. 必要に応じて新規CIジョブやバックアップの重複開始を抑える。稼働中のジョブは原則完了を待つ。バックアップの保持数削減や一括停止で容量問題を隠さない。
5. 全Volumeのレプリカ数を一括で減らす、再構築数を増やす、空き容量の安全条件を一括で緩める、といった対処はしない。
6. 正常コピーを確保できない場合、DBの移行を強行しない。何が不足しているかと代替案を報告する。

### 2. NVMe領域とTalos/Longhornの準備

**最重要の既存設定の注意点：** `talos/patches/worker.yaml.tftpl` のUserVolumeConfigは、コメントにserialで識別すると書かれているが、実際の条件は `match: '!system_disk'`。OS以外が1本という前提であり、3本目を追加すると曖昧になる。

1. Talosが現在のSATAディスクに対して返すserial・デバイス属性・既存Volumeの関連付けを確認する。Proxmox側の既存serialは `longhorn` だが、ゲストへの見え方を推測しない。
2. ディスクを追加する前に、既存SATAと新規NVMeを一意に識別できるUserVolumeConfigの設計を確定し、使用中のTalosバージョンで検証する。既存Volumeの再フォーマットや作り直しを起こさない適用方法を選ぶ。
3. `!system_disk` という広い条件を残したまま、新ディスクの自動プロビジョニングを開始しない。NVMeというホストの物理名でゲストから識別できるとも仮定しない。
4. OpenTofu側にDB専用ディスクの容量・データストア設定を追加する。worker OS用のデータストア変数は既存SATAデータ用と分離し、共通の `vm_datastore_id` を一括変更しない。
5. planでVM置換、既存ディスク削除・縮小、意図しないTalos再適用や再起動がないことを確認する。stateのバックアップを保持し、手動操作を併用した場合も最後に宣言設定とstateを整合させる。
6. workerごとに64GiBの専用ディスクを追加し、固有の識別子で別UserVolumeを作る。マウント例は `/var/mnt/longhorn-nvme`。実際のTalos仕様に従う。
7. kubelet/Longhornから正しい永続マウントが見えること、異なるファイルシステムであることを確認してから、Longhornディスクとして登録する。未マウントの空ディレクトリを登録してOSディスクにデータを書かせない。
8. 予約容量、タグ、専用StorageClassを設定する。小さな検証用PVCで、実際に指定NVMeへ配置されることとデータの再読み取りを確認する。

### 3. PostgreSQLを待機系から移行

1. **StorageClassの追加やCNPGのstorageClass変更だけでは、既存PVCのデータは移らない。** 使用中のCNPG/Longhornがサポートする実際のデータ移行方法を公式資料で確認する。
2. 既存Volumeのレプリカ移動、またはCNPG管理下の待機インスタンス再作成と新PVCへの再同期から、保存先・復旧性・一時容量を検証できる方法を選ぶ。選んだ具体的なコマンド、各確認点、復旧方法を実行前に作業記録へ書く。
3. まず1つのDBクラスタの待機系1台だけを移す。正常な主系と、もう1台の待機系を維持する。同じクラスタで2台同時に作業しない。
4. 移行後にDBがReady、WAL再生が追いついていること、実際のLonghornレプリカが意図した筐体のNVMeにあることを確認する。
5. 各クラスタの主系切り替えでは、Web、バッチ、スクレイパー、定期ジョブ等の全書き込み元を特定する。短い書き込み停止時間を設け、トランザクションを完了させ、最終WAL位置まで移行先が再生済みであることを確認する。
6. 使用バージョンが対応するCNPGの計画的スイッチオーバーで切り替える。独立したSQLのpromote等でoperatorと競合させない。接続先Serviceの追従、アプリ再接続、旧主系の待機系化を確認してから書き込みを再開する。
7. 残りを順番に移し、最終的に両DBとも3/3 Ready、データが3筐体に分散し、主系が別筐体となることを確認する。
8. 長時間稼働中のDBファイルを単純コピーして整合性があると扱わない。旧データ/PVの整理は移行確認後の別作業とし、今回一括削除しない。

### 4. worker OSをNVMeへ移動

1. worker-1を優先し、1101 → 1102 → 1103の順で1台ずつ進める。Proxmoxの対応するディスク移動機能を使用し、元ディスクを保持できる手順を選ぶ。
2. OSのディスクだけが対象であることをVM設定で確認する。SATAのLonghorn用ディスク、新NVMeのDB用ディスクを誤って移動・削除しない。
3. オンライン移動の可否、コピーの帯域制御と中止手順を実バージョンで確認する。オンラインでも無影響と保証しない。controlとNVMeを共有するため、低い負荷から始める。
4. 再起動が必要な場合は、そのworker上のDB主系、Longhornの唯一のコピー、稼働中CI、PodDisruptionBudget等を確認し、サービス継続条件を整えてから実施する。全workerを同時にdrainしない。
5. 移動完了と、計画した再起動後の起動ディスク・Talos・Longhorn・DBの正常性を確認する。コピー済みというだけで起動確認済みと扱わない。
6. 元OSディスクは切り離した復旧候補として保持する。ただし移行後の書き込みは反映されないので、古いOSコピーへ無条件に戻せるとは扱わない。保持対象と削除判断条件を報告する。

### 5. 運用設定と完了確認

- 重いCI、PBSバックアップ、DBバックアップ、ストレージ保守が重ならないよう、管理元を特定して開始時間や同時実行数を必要最小限調整する。バックアップの頻度・復旧目標を勝手に下げない。
- NVMe/SATAの待ち時間、NVMe thin poolのデータ・metadata容量、Longhornの複製不足、CNPGの複製遅延、主系配置をGrafanaで確認できるようにする。既存パネルを活用し、重複を避ける。
- 同等の負荷条件で移行前後を比較する。瞬間値だけで高速化を断定せず、通常のWeb処理と少なくとも1回の代表的なCI実行を観測する。
- 一時停止したジョブやArgo CD同期を元に戻し、戻した後に配置やディスク設定が巻き戻らないことを確認する。
- 関連マニフェストの検証とOpenTofu planを実施し、残る差分はすべて理由を説明できる状態にする。無関係な差分を解消するための一括applyはしない。

## 中止・切り戻しの原則

- 次の対象へ進む前に、直前の対象が正常であることを確認する。異常時は新規コピー・移行を止め、残っている正常系を保護する。
- NVMeの容量逼迫、etcdの遅延や警告、継続するWebエラー、DBの複製停止、追加のレプリカ喪失を放置して続行しない。
- 主系切り替え後に書き込みが再開されている場合、古い主系や古いPVをそのまま昇格させて戻さない。最新のタイムライン/WALを確認し、再同期後の計画的切り替え、または検証済みバックアップから復元する。
- DBの `fsync`、`full_page_writes`、ZFSの同期書き込み保証を弱めて高速化しない。Talos reset/reinstall、VM再作成、既存ディスクのフォーマットで移行を代用しない。
- 安全に次へ進めない場合は、その段階を未完了として、観測事実・保存できた正常系・再開に必要な条件を報告する。完了扱いにしない。

## 完了条件と納品物

以下を満たした範囲を証拠付きで報告してください。

- [ ] control 3台＋worker 3台がReadyで、各筐体に1組ずつ存在する。
- [ ] 対象の複製不足が解消し、再構築の失敗ループが継続していない。
- [ ] 両DBが3/3 Readyで、各インスタンスの実データが3筐体のNVMeへ分散している。
- [ ] 両DBの主系が別筐体で動作し、Webの読み書きと再接続を確認できた。
- [ ] worker OSがNVMe上にあり、計画した起動確認が完了している。
- [ ] NVMe容量に余裕があり、control/etcdの性能を悪化させていない。
- [ ] DBバックアップ・WAL保存と復旧確認の結果が記録されている。
- [ ] 通常負荷での前後比較と、未測定・未改善の項目が明記されている。
- [ ] 宣言設定、ライブ設定、OpenTofu stateが整合し、一時変更が復帰済み。
- [ ] 保存した旧ディスク/PV、一時リソース、後日削除候補と削除条件が一覧化されている。

作業記録はhomelabの `docs/` にMarkdownで残してください。変更ファイル一覧、実施順、主要な測定値、移行方法、実際のサービス中断、切り戻し方法、残作業を含めます。コード変更はレビュー可能な差分として残し、commit/push/PRは適用されるリポジトリ運用に従ってください。第三者へのメッセージ送信はこの依頼に含みません。

## 公式資料

必ず実際の導入バージョンに対応するページへ切り替えて確認してください。

- Longhorn 複数ディスク：<https://longhorn.io/docs/1.12.1/nodes-and-volumes/nodes/multidisk/>
- Longhorn StorageClassパラメータ：<https://longhorn.io/docs/1.12.1/references/storage-class-parameters/>
- Longhorn 設定：<https://longhorn.io/docs/1.12.1/references/settings/>
- CloudNativePG 複製：<https://cloudnative-pg.io/docs/current/replication/>

TalosのUserVolumeConfig、CNPGのストレージ移行・スイッチオーバー、Proxmoxのディスク移動についても、実行前に導入バージョンの公式資料を確認してください。
