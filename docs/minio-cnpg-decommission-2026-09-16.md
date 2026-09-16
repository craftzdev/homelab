# MinIO の CNPG バケットを廃止する — 2026-09-16

CloudNativePG のバックアップ先はクラスタ内 MinIO から Cloudflare R2 へ
移した（[ADR-0012](adr/0012-backup-strategy-revisited.md)）。MinIO 側に
残った CNPG 用のバケットをどう扱うかを決める。

## 結論（先に）

| | 判断 |
|---|---|
| MinIO そのもの | **残す。** Loki 2.3 GB と Tempo が現役で使っている |
| CNPG 用バケット 2 つ（244 MB） | **廃止するが、削除は 2026-09-23 頃まで待つ** |
| bucket 作成 CronJob 2 つ | **今回削除した** |

## 1. MinIO は消せない

```
$ du -sh /data/*
4.0K  loki-admin          260K  tempo-traces
4.0K  loki-ruler          2.3G  loki-chunks     ← 現役
39M   umami-postgres      205M  moshitoku-postgres   ← 廃止対象
                                                合計 2.5G
```

廃止できるのは **244 MB** だけである。`minio-bucket-bootstrap`（Loki 用の
3 バケットを作る）は残す。

## 2. 本当に書き込みが止まっているかを確かめた

「R2 に切り替えた」という設定を信用せず、両側の実物を見た。

```
MinIO   moshitoku wals 最新 … 000000010000000200000002B.gz   Sep 16 04:23
MinIO   umami base     最新 … 20260916T032657                Sep 16 03:27
R2      moshitoku wals 最新 … 00000001000000020000005E.gz     Sep 16 07:41
現在時刻                                                      Sep 16 07:45
```

Postgres のログが示す当時の LSN は `2/5E` で、**R2 の最新 WAL と一致する。**
MinIO は 3 時間 25 分にわたり 1 バイトも受け取っていない。

さらに重要なのは**継ぎ目に欠落が無い**ことである。

```
MinIO  … 2/00 (00:48) … 2/2B (04:23)      ここで停止
R2       2/2C          … 2/5E (07:41)      ここから継続   計 51 個
```

`2/2B` の次が `2/2C` で、重複も欠落もない。切り替えは WAL チェーンを
切らずに完了している。

> 調査の途中、MinIO のディレクトリ更新時刻が当日だったため
> 「R2 へ切り替えたのに MinIO へも書いている」と一度疑った。
> 実際には 00:48〜04:23 が移行の過渡期で、その後 05:28 に R2 への
> ベースバックアップが完走している。**ディレクトリの mtime だけでは
> 書き込み中か否かを判断できない。**

## 3. なぜ今すぐ消さないのか

R2 の保持期間は 7 日だが、**運用を始めたのが今日**である。

| | 世代数 | 期間 |
|---|---:|---|
| MinIO umami | 8 | 09-11 〜 09-16 |
| MinIO moshitoku | 4 | 09-12 〜 09-15 |
| **R2 umami** | **2** | **09-16 のみ** |
| **R2 moshitoku** | **3** | **09-16 のみ** |

今 MinIO を消すと、**復元可能な過去は当日分だけになる。**
数日前に混入した論理的な破損に後から気づいた場合、戻る先が無い。

R2 が 7 日分を蓄えれば MinIO の深さを上回る。それまで 244 MB を
Longhorn 上に置いておく方が、得られる安全余裕に対して安い。

**削除予定日: 2026-09-23 頃**（R2 の最古世代が 09-16 で、7 日保持に達する頃）

## 4. 今回やったこと

| ファイル | 変更 |
|---|---|
| `minio-umami-bootstrap.yaml` | 削除 |
| `minio-moshitoku-bootstrap.yaml` | 削除 |
| `storage/kustomization.yaml` | 上記 2 件の参照を削除 |
| `storage/networkpolicy.yaml` | bootstrap Job 用の egress 2 件と、`minio-access` の ingress 2 件を削除 |

CronJob はバケットとユーザーを作り直すだけで、バックアップを書いては
いない。削除しても**既存のバケットとユーザーは MinIO 側に残る**ため、
§3 の冷凍コピーは読める。

副次的に、`pgsty/mc` を使う Pod が 3 つから 1 つに減る。このイメージは
Critical 2 件（`CVE-2025-68121` / `CVE-2026-33186`）を抱えており、
VulnerabilityReport 3 件分がそのまま消える。

`logging` app は `prune: true` なので、マージ時に ArgoCD が実体を消す。

### 4-1. NetworkPolicy は一部あえて残した

`minio-access` の ingress にある

- `io.kubernetes.pod.namespace: analytics`
- `cnpg.io/cluster` を持つ `moshitoku` の Pod

は**残している**。§3 の冷凍コピーから復元する経路だからである。

> ⚠️ 消す順序を間違えないこと。ここを先に消すと、データが残っていても
> 復元できない。この罠は以前に一度踏んでおり、`networkpolicy.yaml` の
> コメントに経緯が残っている（復元は別名のクラスタとして起動するため、
> クラスタ名で固定すると通らない）。

## 5. やっていないこと

### 5-1. バケットのデータ削除

§3 のとおり 2026-09-23 頃まで待つ。その日の手順：

**① 前提の確認（これが通らなければ以降は実施しない）**

```sh
# R2 が 7 世代前後を持ち、最古が 7 日前まで遡れること
kubectl -n moshitoku get backups.postgresql.cnpg.io
kubectl -n analytics  get backups.postgresql.cnpg.io
```

**② `mc` を持つ一時 Pod を立てる**

`mc` は MinIO のイメージに入っていない（`minio-0` に exec しても
`command not found` で終わる）。`minio-bucket-bootstrap` の Pod は
固定のスクリプトを実行して終了するため、対話的には使えない。
同じイメージで Pod を立てる。

> ⚠️ **ラベルを `app.kubernetes.io/name: minio-bucket-bootstrap` に
> 合わせること。** `minio-access` はこのラベルにしか ingress を許して
> おらず、付け忘れると `mc` が i/o timeout で止まる。原因が
> NetworkPolicy だと気付きにくい。
>
> ⚠️ `logging` は PSA `restricted` である。securityContext を省くと
> Pod の作成そのものが拒否される。

```sh
kubectl -n logging run mc-cleanup --rm -it --restart=Never \
  --image=docker.io/pgsty/mc:RELEASE.2026-03-13T08-57-32Z \
  --labels=app.kubernetes.io/name=minio-bucket-bootstrap \
  --overrides='{
    "spec": {
      "securityContext": {
        "runAsNonRoot": true, "runAsUser": 1000, "runAsGroup": 1000,
        "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}
      },
      "containers": [{
        "name": "mc",
        "image": "docker.io/pgsty/mc:RELEASE.2026-03-13T08-57-32Z",
        "command": ["/bin/sh"], "stdin": true, "tty": true,
        "securityContext": {
          "allowPrivilegeEscalation": false,
          "capabilities": {"drop": ["ALL"]}
        },
        "env": [
          {"name": "MINIO_ROOT_USER", "valueFrom": {"secretKeyRef":
            {"name": "minio-root-credentials", "key": "root-user"}}},
          {"name": "MINIO_ROOT_PASSWORD", "valueFrom": {"secretKeyRef":
            {"name": "minio-root-credentials", "key": "root-password"}}}
        ]
      }]
    }
  }'
```

> ⚠️ Secret を `envFrom` で丸ごと入れないこと。キー名が `root-user` /
> `root-password` でハイフンを含み、シェル変数として参照できない。
> 上のように `env` で名前を付け替える。

**③ Pod 内で削除する**

```sh
mc alias set local http://minio:9000 "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}"

mc ls local                      # loki-* と tempo-traces が残ることを確認
mc rb --force local/umami-postgres
mc rb --force local/moshitoku-postgres

mc admin user ls local           # 消す access key を確認してから
mc admin user remove local <umami の access key>
mc admin user remove local <moshitoku の access key>
mc admin policy rm local umami-postgres-backup
mc admin policy rm local moshitoku-postgres-backup

exit                             # --rm なので Pod は消える
```

access key は Secret にある。**値は表示せず**参照すること。

```sh
kubectl -n analytics get secret umami-s3-credentials \
  -o jsonpath='{.data.ACCESS_KEY_ID}' | base64 -d
```

**④ 後片付け**

```
1. §4-1 で残した NetworkPolicy の許可 2 件を消す
   （minio-access の ingress: analytics 名前空間 / cnpg.io/cluster）
2. umami-s3-credentials / moshitoku-s3-credentials（MinIO 用）を消す
   ⚠️ *-r2-credentials と取り違えないこと。消すのは -s3- の方である
```

### 5-2. ObjectStore の名前

R2 を指しているのに名前が `umami-minio` / `moshitoku-minio` のままである。
誤解を招くが、**改名は見送った。** `barmanObjectName` と CR 名を同時に
変える必要があり、稼働中の WAL アーカイブを一瞬でも切る危険に対して
得られるものが表示上の分かりやすさだけだからである。

代わりに `database.yaml` の該当箇所へ、R2 を指していること・認証情報は
`*-r2-credentials` であって `*-s3-credentials`（MinIO 用）ではないことを
明記してある。§5-1 を実施して MinIO 用の Secret が消えれば、
取り違えの余地自体が無くなる。

### 5-3. Longhorn の容量回復

244 MB は MinIO の PVC 内で空くだけで、Longhorn のボリューム自体は
縮まない。容量目的の作業ではない。**主目的は、有効なバックアップに
見える陳腐なコピーを残さないことである。**
