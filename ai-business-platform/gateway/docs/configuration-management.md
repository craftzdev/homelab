# 設定の参照・下書き管理

2026-09-21 更新。設定取得・下書き・基本検証に加え、固定した配布候補、専用 Config Controller、CI/実行試験の証跡、人間による Git 反映要求、戻す下書きの作成を実装した。本番の実行試験・署名済み image の digest 更新・Controller 配置は未接続。詳細は [Config Controller](../../config-controller/README.md)。

## 保存先と経路

```text
Control Plane /settings
  → Gateway /v1/config
    → Agent /v1/configuration
      → Worker /v1/configuration

編集した下書き → Gateway PostgreSQL config_drafts / config_draft_revisions
監査 → Gateway platform_events
```

Agent は registry に登録された profile、capabilities、入力 schema を返す。無効な役割も無効と明示する。Worker は `/etc/codex/skills` の管理対象テキストと、`CODEX_HOME/AGENTS.md` に実際に配置された共通指示を返す。Skill の scripts/references/assets/agents もテキスト形式のみ対象にする。symlink、隠しファイル、バイナリ、過大なファイルは対象外。

Worker の取得に失敗しても Agent 側の一覧は表示できる。取得状態を component ごとに返し、「未取得」と「設定なし」を区別する。この一覧は配置ファイルの観測であり、特定 Job が本文を読み込んだ証明ではない。

Worker 共通指示は現在 ConfigMap 内にあるため、`harness/AGENTS.md` は論理的な編集対象名。`source_mapping` が `deploy/kubernetes/codex-harness.yaml` の `data.AGENTS.md` を示す。存在しない Git ファイルへ適用済みとは表示しない。

## 使用する API

| API | 内容 |
| --- | --- |
| GET `/v1/config/inventory` | Agent/Worker の設定本文、digest、役割、取得状態 |
| GET/POST `/v1/config/drafts` | 最近100件の下書き一覧・作成 |
| GET/PATCH `/v1/config/drafts/{id}` | 本文、差分、履歴の取得・編集 |
| POST `/v1/config/drafts/{id}/validate` | 保存内容の基本検証と編集元の再確認 |

Gateway の通常認証に加え、作成・編集・検証は `X-Config-Admin-Token` が必要。`CONFIG_ADMIN_TOKEN` は32文字以上、`CONFIG_ADMIN_ACTOR` は監査に記録する固定主体。未設定時は更新を拒否する。Bot の Gateway token 単独では変更できない。

Control Plane には同じ編集用 token を `CONFIG_ADMIN_TOKEN` として設定する。ブラウザには返さない。Kubernetes では既存 runtime Secret の任意キー `config-admin-token` を参照する。未設定なら閲覧モード。ブラウザからの設定変更には session に結び付いた CSRF token と同一 origin の検証を行う。

## 下書きの契約

- 作成に `source_id / base_sha256 / content` と `Idempotency-Key` が必要。
- 同じ主体・キー・内容の再送では同じ下書きを返す。別内容は409。
- 編集と検証に `expected_revision` が必要。古い版は409になり、UI は入力内容を残す。
- UTF-8で64 KiB以下。作成時の編集元と各保存版の本文・digestを記録する。
- 下書き状態は `DRAFT / VALIDATED / VALIDATION_FAILED`。`VALIDATED` は構文等の基本検証のみを意味し、UI では「基本検証済み」と表示する。
- 編集すると過去の検証を無効化する。検証中に別更新が入った場合も古い検証を保存しない。
- 編集元の配置ファイルが更新・消失した場合は検証不合格。自動で新しい base に差し替えない。
- 検証内容は Markdown/Skill frontmatter、JSON、JSON Schema、YAML、Python 構文。スクリプトの実行や外部 schema の取得は行わない。未対応の script 形式は検証不合格とする。
- 参照先との整合性、実際の executor、Policy と権限、Git/CI/Release の検証は未実装。結果には `scope=syntax_only / activation_ready=false` と残りの検証項目を返す。
- UI の「保存して基本検証」は保存後の版を検証する。検証先に接続できなくても、保存が成功した本文は Gateway DB に残る。

すべての下書きに `applied=false` を返す。Agent/Worker のファイル、Git、イメージ、ConfigMap をこの API は書き換えない。

## Writer / Marketing の準備

`content-writer-v1` と `marketing-strategist-v1`、`content.draft` と `marketing.plan` の入力 schema を Agent に登録した。両 action は無効で、設定画面で参照・下書き編集するための定義である。

専用 executor、Skill bundle、pool、Artifact 引き継ぎ、実行検証が揃うまで有効化しない。既存の `technical-writer-v1 / content.article` と `growth.plan` は保持する。

## 配布 API と現在の境界

| API | 内容 |
|---|---|
| POST `/v1/config/drafts/{id}/release` | 基本検証した保存版を固定。expected_revision 必須。同じ版は再送可能 |
| GET `/v1/config/releases` / `/{id}` | 配布状態・履歴・差分。詳細は配置中と読み込み版のハッシュも返す |
| POST `/v1/config/releases/{id}/promote` | CI と実行試験が合格した版について人間が Git 反映を要求 |
| POST `/v1/config/releases/{id}/rollback` | 配置元の一致を確認して変更前の内容を新しい下書きにする |
| POST `/v1/config/workers/{id}/intake` | 管理者による新規受付の停止・再開。expected_revision 必須 |
| GET `/v1/config/controller/work` | 専用 Controller が未完了候補を取得 |
| POST `/v1/config/controller/releases/{id}/report` | 専用 Controller が版を指定して CI/Git 証跡を報告 |

人間の書き込みには `X-Config-Admin-Token`、Controller の取得と報告には
`X-Config-Controller-Token` が必要。通常の Gateway 認証も必須。
Controller は人間の適用操作を代行できず、MCP の tool 一覧にも適用操作は追加しない。
旧 `/v1/workers/{id}/commands` も設定管理者の資格情報を必須に変更した。
旧クライアントは `X-Config-Admin-Token` を追加するか、版を確認できる新 API へ移行する。

`QUEUED → REVIEW → VERIFIED → PROMOTE_REQUESTED → MERGED` の遷移を記録。
固定 head の試験が不合格になれば VERIFIED から REVIEW に戻る。
Git の base/head 競合等は BLOCKED。新しい下書きの保存・再検証からやり直す。
配置観測は `MATCH / DIFFERENT / UNKNOWN`。Agent の `loaded_match` も別項目で返す。
これらは全 pool の反映・実行成功の証明ではない。

Writer/Marketing の実行開始、複数ファイルの一括配布、自動カナリアと排出は次段階。


## 検証記録

2026-09-20、ローカル変更に対して以下を確認した。本番へのデプロイは行っていない。

| 対象 | 結果 |
| --- | --- |
| Control Plane | Python 3.13 の Docker build 内で全57テスト成功 |
| Gateway | 全体221成功・1スキップ。その後の追加を含む設定管理9テスト成功 |
| Agent | 全体53テスト成功。その後の追加を含む設定取得・役割定義4テスト成功 |
| Worker | 全71テスト成功 |

設定管理では、認証分離、CSRF、HTML escaping、更新競合、再送、履歴、編集による検証失効、検証中の競合、取得本文の digest、保存後の検証障害からの再試行を確認した。ブラウザ接続が利用できず、画面の目視検証は未完了。実際のモデルによる Writer/Marketing 実行と Git/CI 適用は検証対象に含まれない。


## Production management API (2026-09-21)

`GET /v1/config/contract` は API version 1.0 と capabilities を返す。
`GET /v1/config/openapi.json` は通常の Gateway 認証を通した管理 API 定義。
UI は release detail の `available_commands` を表示し、状態から適用操作を推測しない。
v1 は追加的なフィールド変更を許容し、未知の状態は表示を維持して書き込みを無効にする。

Gateway が設定・監査・承認状態の正本、Control Plane が管理 UI、
専用 Controller が GitHub 連携、container CI と Argo CD が配置を担当する。
UI の変更は Controller や Worker の再実装を必要としない。

本番 Gateway の管理認証を有効化し、API でワーカー停止・適用観測を確認した。
Agent / Worker / UI の署名済みイメージを GitOps に反映する CI を接続した。
Controller の専用 GitHub 資格情報と候補 PR の実モデル試験は別途必要で、
それらが揃うまでは管理画面の候補を自動適用しない。

検証: Gateway 267 passed / 1 skipped、Worker 110 passed、Agent 70 passed、
Control Plane 67 passed。イメージ昇格は一時 Git repository で古いビルドの拒否と
YAML・他イメージの維持を確認した。本番 smoke の結果は deployment-verification.md に記録する。
