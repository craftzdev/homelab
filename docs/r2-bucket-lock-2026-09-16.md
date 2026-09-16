# R2 バケットロックで S6 を塞げるか — 2026-09-16

[ADR-0012](adr/0012-backup-strategy-revisited.md) は S6（ランサムウェア）に
ついて「R2 のバケットロック／オブジェクト保持で塞げる**可能性があるが
未確認**」と書いたまま残っていた。実測して確かめた。

## 結論（先に）

| | |
|---|---|
| 塞げるか | **塞げる。** 侵害されたクラスタの資格情報ではロックを解除できない |
| barman を壊さないか | **壊さない。** barman はオブジェクトを上書きしない |
| 保持期間 | **6 日。** 7 日にしてはいけない（§3） |
| 実施状況 | **未実施。** R2 Admin 権限のトークンが必要（§5） |

## 1. 侵害時にロックを解除できないこと

クラスタが持つのは Object Read & Write にスコープしたトークンである。
これでロック関連の API を叩けるなら、侵害された時点でロックごと外されて
意味が無い。実際に叩いて確かめた。

```
$ (SigV4 で GET /homelab-umami-postgres?<sub> を実行)
  ?object-lock   HTTP 403  AccessDenied
  ?versioning    HTTP 403  AccessDenied
  ?lifecycle     HTTP 403  AccessDenied
```

**3 つとも拒否される。** ロックの設定・解除には別の資格情報
（R2 Admin）が要り、それはクラスタに置かない。したがって
バケットロックは S6 に対して実効性がある。

## 2. barman はオブジェクトを上書きしない

ロックは削除だけでなく**上書きも止める。** barman が同じキーへ書き直す
オブジェクト（索引やメタデータ）を持っていれば、ロックを掛けた時点で
バックアップが壊れる。両バケットの全オブジェクトを列挙して確かめた。

```
[homelab-moshitoku-postgres]  89 objects
  x83   moshitoku-postgres-v1/wals/0000000100000002/<WAL>.gz
  x3    moshitoku-postgres-v1/base/<TS>/backup.info
  x3    moshitoku-postgres-v1/base/<TS>/data.tar.gz

[homelab-umami-postgres]  19 objects
  x15   umami-postgres-v1/wals/0000000100000000/<WAL>.gz
  x2    umami-postgres-v1/base/<TS>/backup.info
  x2    umami-postgres-v1/base/<TS>/data.tar.gz
```

**キーはすべて一意である。** WAL はセグメント名で、ベースバックアップは
開始時刻で分かれる。共有の索引も、書き直されるメタデータも無い。
ロックは書き込み経路に触れない。

## 3. なぜ 7 日ではなく 6 日なのか

ここが唯一の注意点である。

`retentionPolicy: 7d` により、barman は **7 日より古い**オブジェクトを
削除する。ロックの保持期間を N 日とすると、オブジェクトは作成から
N 日間削除できない。

| N | 起きること |
|---|---|
| **7 以上** | barman が消したい時刻とロックが切れる時刻が重なる、または越える。削除が失敗し、オブジェクトが溜まり続ける |
| **6** | 6 日目にロックが切れ、7 日目に barman が消す。**競合しない** |

6 日にしても防御は弱まらない。侵害された時点から遡って**6 日分の
バックアップは削除できない**。復旧に使える窓としては十分である。

> ⚠️ ここを 7 にすると、症状は「バックアップが壊れる」ではなく
> 「R2 の使用量が静かに増え続ける」という形で出る。気付きにくい。

この危険は既にアラートで手当てされている。`alerts.yaml` の
`CNPGRetentionNotEnforced` は、まさにこの状況を想定して書かれており、
コメントに Cloudflare のドキュメントの不備まで記されている。

```
# ⚠️ これは R2 のバケットロックを有効にしたときの検証手段でもある。
#    Cloudflare のドキュメントには
#      - 保持の起点がアップロード時刻か
#      - 期間経過後に通常どおり削除できるか
#    が明記されていない（2026-09-16 時点）。
#    バケットロックを入れたら 8〜10 日後にこのアラートが
#    鳴らないことを確認する。鳴ったらロックルールを外す。
```

**6 日にするのは、この不明点に対する保険でもある。** 起点が
アップロード時刻でなかった場合や、期間経過の判定に余裕が無い実装
だった場合でも、1 日ぶんの緩衝があれば barman の削除は通る。

`retentionPolicy` を変えるときは、ロックの日数も一緒に見直すこと。
**常に「ロック < retention」を保つ。**

## 4. 適用するコマンド

`wrangler` の構文は実物で確認した。

```
wrangler r2 bucket lock add <bucket> [name] [prefix]
  prefix    Prefix condition for the bucket lock rule (set to "" for all prefixes)
  --retention-days   Number of days which objects will be retained for
```

```sh
export CLOUDFLARE_ACCOUNT_ID=1a04b6f3502614c09cde09c933331300
export CLOUDFLARE_API_TOKEN=<R2 Admin Read & Write のトークン>

npx wrangler r2 bucket lock add homelab-umami-postgres \
  cnpg-retain-6d "" --retention-days 6

npx wrangler r2 bucket lock add homelab-moshitoku-postgres \
  cnpg-retain-6d "" --retention-days 6

# 確認
npx wrangler r2 bucket lock list homelab-umami-postgres
npx wrangler r2 bucket lock list homelab-moshitoku-postgres
```

prefix は `""`（全オブジェクト）にする。WAL だけ、ベースだけを守っても
復元できない。

## 5. 未実施の理由

必要なトークンが無い。1Password にある R2 のアイテム 2 つは、どちらも
`ACCESS_KEY_ID` / `SECRET_ACCESS_KEY` だけを持つ Object Read & Write の
資格情報で、§1 のとおりロックには使えない。

**R2 Admin Read & Write のトークンを別途発行する必要がある。**

> ⚠️ 発行したトークンをクラスタへ置かないこと。置いた時点で §1 の前提が
> 崩れ、バケットロックの意味が無くなる。手元（Keychain か 1Password）に
> 留め、適用のときだけ使う。

## 6. 適用後に確かめること

設定できたことは、効いていることを意味しない。

```sh
# 6 日以内のオブジェクトを、クラスタと同じトークンで消せないこと
#   → AccessDenied になれば効いている
# 8 日後、barman の retention が実際に古いものを消していること
#   → R2 のオブジェクト数が頭打ちになる。増え続けるなら §3 の失敗
```

2 つ目は待たないと分からない。`CNPGRetentionNotEnforced` は
`first_recoverability_point` が 10 日より古くなったら鳴る設定なので、
**8〜10 日後にこれが鳴らないこと**が確認になる。鳴ったら §3 の失敗で、
ロックルールを外すこと。
