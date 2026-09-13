# 成果物に紐付く人間承認

## 境界

GatewayのPostgreSQLを承認記録の正とする。Botの会話メモリは利用しない。
この変更はGateway APIとDB migrationであり、実際の本番デプロイを行うものではない。
既存Control Planeにはこの新API用の承認フォームはまだない。
MinIOの設定・成果物の保存方法は変更しない。

保存する情報:

- `target`: candidate ID、リポジトリ、commit SHA、image digest、production環境、Build/QA Job ID、QA状態。
- `target_sha256`: 正規化したtarget JSONのSHA-256。署名や外部証明ではなく、確認対象の同一性チェック。
- `approved_by`: サーバー設定で人間専用トークンに結び付けた操作者ID。
- `requested_at` / `resolved_at`: 申請・承認（または取消等）のUTC時刻。
- `expires_at`: 申請時からの有効期限。既定1時間、指定可能60秒〜24時間。承認しても延長しない。
- `consumed_at`: 本番URL登録に使用した時刻。使用後は`CONSUMED`となり再利用できない。

## 人間IDの設定

Gateway public-apiへ既存の`HUMAN_APPROVAL_TOKEN`と次を設定する。

```dotenv
HUMAN_APPROVAL_ACTOR=craftz
```

トークンは秘密管理から投入し、Git・会話・Skills・Worker・Hermes/Grok Botに渡さない。
クライアント指定の`approved_by`や`X-Approver`は信用しない。
Actor未設定時は候補登録・承認・取消・本番URL登録が503で拒否される。

これは**単一操作者**モデルである。Actorはメール認証されたOIDC identityではない。
同じトークンを複数人で共有すると人間を区別できないため、共有しない。
操作者を変更するならActorとトークンを一緒に変更する。複数人運用は個別トークン
または検証済みOIDC subjectへのマッピングを別途実装する。

## 1. リリース候補を人間が登録

`PUT /v1/projects/{project_id}/release-candidate`

Cloudflare Access Service Auth、Gateway Bearer、人間専用トークンの3つが必要。
Body例（値はダミー。実際に確認した値に置き換える）:

```json
{
  "commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "image_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "environment": "production",
  "build_job_id": "11111111-1111-4111-8111-111111111111",
  "qa_job_id": "22222222-2222-4222-8222-222222222222"
}
```

commit（40/64桁のlowercase hex）とdigest（`sha256:`+64桁）の少なくとも一方は必須。
コンテナ配布ではimage digestを必須運用とし、可能ならcommitも両方登録する。
branch名、タグ`latest`、短縮SHAは受け付けない。

Gatewayは、その案件の現在の成功Build/QA Jobであること、QAの入力にある
`source_worker_job_id`がBuildのWorker Jobと一致すること、Build/QAが実行中でないことを確認する。
QA failは拒否、inconclusiveは明示的な人間判断が必要。

**commit/digest自体は現時点では人間が確認して登録する値**であり、
`binding_source=human_attested`として記録する。GitHub/Harborへの実在照合や、
そのdigestがそのcommitから生成されたという署名付き来歴の検証までは行わない。
Workerの`base_commit`は変更前のSHAなので、自動的に承認対象へ流用しない。
自動登録へ移す際は、CIから信頼できる最終commit/digestとBuild/QAの証拠を渡すこと。

再登録は新candidate IDを発行し、未使用の旧承認を無効化する（同じ値の再登録も含む）。
対象は承認レコード内へコピーするため、後からprojectを書き換えても承認内容は変わらない。

## 2. 承認を申請

`POST /v1/projects/{project_id}/approval`

Gateway認証が必要。人間専用トークンは不要。
Bodyは省略可能、指定時は`{"ttl_seconds": 3600}`。
候補の登録がない場合は409。返却値に`approval_id`、`target`、`target_sha256`、`expires_at`が含まれる。

MCPの`request_production_approval(project_id, ttl_seconds=3600)`も同じ処理を呼ぶ。

## 3. 対象を確認して人間が承認

`GET /v1/approvals/{approval_id}`で対象全体と期限を確認する。
一覧は`GET /v1/projects/{project_id}/approvals`。
MCPの`get_production_approval(approval_id)`は同じ情報を読み取り専用で提供する。

`POST /v1/approvals/{approval_id}/approve`

人間専用トークンが必要。Body:

```json
{
  "target_sha256": "確認画面で取得した64桁のtarget_sha256",
  "accept_inconclusive_qa": false
}
```

QAがinconclusiveの場合だけ、内容を確認した人間が`accept_inconclusive_qa=true`を送る。
Bodyのhashと固定済み対象が異なる場合、期限切れ、取消済み、旧候補の場合は409。
`approved_by`はサーバーが付与するためBodyへ含めない（追加フィールドは422）。

## 4. 本番URL登録と一回限りの消費

既存の`PUT /v1/projects/{project_id}/production`は以下のBodyが必須になる。

```json
{
  "approval_id": "申請時のUUID",
  "commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "image_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "environment": "production",
  "production_url": "https://example.invalid/"
}
```

人間専用トークンが必要。承認済みで期限内、案件・候補・commit/digestが完全一致する場合だけ
本番URL登録と承認消費を同じDB transactionで行う。両方承認した場合は両方の一致が必要。
二重使用・別案件への流用・承認後の対象変更は409。

**このAPIは本番URLの記録であってデプロイ実行APIではない。**
外部で勝手に行われたGitHub/Argo操作を防ぐことや、実際に公開されたdigestを検証することはできない。
Gatewayのproduction Job実行は引き続き無効（403）。将来デプロイexecutorを追加するときは、
副作用の前に承認を原子的に予約し、同じdigestだけを実行し、再試行を同じdeployment IDで追跡する。
「デプロイ後にこのAPIを呼べば事前承認の強制になる」とは扱わない。

## 失効・取消・競合

- 新しいBuild/Fix/QAの登録と、それらの完了時に既存候補・未使用承認を無効化する。
- 成功/最終失敗のJobへの新しいcallbackは拒否し、承認の根拠を後から上書きさせない。
- 冪等性キーによる同一Job再送では、新しい無効化を行わない。
- 有効期限は承認時・本番URL登録時に必ず確認する。GETの`effective_state`は期限切れを`EXPIRED`として返す。
- バックグラウンドの期限切れスイーパーは不要。再申請時に期限切れ行を`EXPIRED`へ更新して新規申請を作る。
- `POST /v1/approvals/{id}/cancel`はPENDINGまたは未使用APPROVEDを取り消せる。
- project→approvalのロック順で同時承認・本番URL登録・候補変更を直列化する。
- 監査イベントと状態更新は同じtransactionに保存する。DB管理者からの改ざん耐性を提供するWORM監査ではない。

## 導入とロールバック

1. Gateway DBのバックアップを取得し、復元できることを確認する。
2. 人間トークンの所有者を確認し、public-apiへ`HUMAN_APPROVAL_ACTOR`を設定する。
3. API呼び出し側を新しいリクエスト形式へ更新する（旧approveのBodyなし呼び出しは422）。
4. public/callback両APIを更新する。起動時に冪等migrationが走る。
5. target/期限がない旧PENDING/APPROVEDは`INVALIDATED`になり、その案件は再レビュー状態へ戻る。
   既存LIVE案件の公開状態、成果物、履歴は削除しない。
6. ダミー案件で申請・取消・期限切れの拒否を確認する。本番公開はこの変更の検証に含めない。

古いバイナリへ戻すと旧APIが新しい検証を迂回するため、単純なイメージ差し戻しは不可。
ロールバック時は承認・本番URL登録エンドポイントを停止し、DBバックアップと整合する手順で復旧する。

## テスト

```bash
cd ai-business-platform/gateway
docker compose -p gateway-approval-tests -f compose.test.yaml up --abort-on-container-exit --exit-code-from tests
docker compose -p gateway-approval-tests -f compose.test.yaml down
```

PostgreSQL 17の一時DBだけを使う。ホストへのポート公開、永続ボリューム、本番資格情報は使わない。
CIはGateway関連変更時だけ実行する。現ARC `homelab-runner`にはDockerがないため、このDB統合テストのみ
GitHub-hosted runnerを使用し、同一refの古い実行はキャンセルする。
