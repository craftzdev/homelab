# 設定の参照・下書き管理

2026-09-20 時点の実装。設計書の Phase 3A のうち、実行環境の設定取得、下書き編集、差分、基本検証を追加した。Git/CI の変更作成、Release の適用・rollback は未実装。

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

## 次の実装境界

下書きから Git change set を作る Config Controller と、consumer の observed digest を確認する activation を追加する。それまでは設定の「適用」操作を公開しない。Writer/Marketing の実行開始は別段階の専用 pool 実装で行う。

## 検証記録

2026-09-20、ローカル変更に対して以下を確認した。本番へのデプロイは行っていない。

| 対象 | 結果 |
| --- | --- |
| Control Plane | Python 3.13 の Docker build 内で全57テスト成功 |
| Gateway | 全体221成功・1スキップ。その後の追加を含む設定管理9テスト成功 |
| Agent | 全体53テスト成功。その後の追加を含む設定取得・役割定義4テスト成功 |
| Worker | 全71テスト成功 |

設定管理では、認証分離、CSRF、HTML escaping、更新競合、再送、履歴、編集による検証失効、検証中の競合、取得本文の digest、保存後の検証障害からの再試行を確認した。ブラウザ接続が利用できず、画面の目視検証は未完了。実際のモデルによる Writer/Marketing 実行と Git/CI 適用は検証対象に含まれない。
