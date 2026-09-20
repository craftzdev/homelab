# AI Business Control Plane：エージェント管理・Worker 管理・タスクかんばん設計

- 作成日：2026-09-20
- 状態：実装に向けた提案設計。既存環境への変更・デプロイは含まない。
- 対象：`ai-business-control-plane`、`ai-business-agent`、`ai-business-worker`、`homelab` 内の Business Gateway
- 想定利用者：初期は管理者１名。人間と Grok Bot が同じ仕事の状況を共有する。
- 設計の到達点：画面仕様、責務、データモデル、状態遷移、API、設定反映、運用、移行、受け入れ条件を実装可能な粒度で定義する。

読む順番：画面と操作は §6–9、担当と進行は §10–12、Agent/Worker の Harness・Skill 更新は §13、実装契約は §14–18、導入と検証は §19–25、Writer/Marketing Worker の追加は §27 を参照する。

実装進捗：設定の取得・下書き編集・基本検証については [設定管理の実装メモ](../../ai-business-platform/gateway/docs/configuration-management.md) を参照する。本書の完成形と、現時点で実装済みの範囲を分けて記録する。

## 1. 目指す体験

管理者は、エージェントの役割・スキル・実行設定を編集し、Grok Bot または UI から届いた依頼をかんばんで追跡する。タスクを開くと「誰が、何を根拠に、どの設定で、どこまで進めたか」が分かり、その場で追加指示、担当変更、停止、再試行、レビューを行える。

一枚のカードはユーザーの一つの依頼を表す。Product、Developer、QA の個々の実行はカード内の工程と実行履歴として表示する。モデル呼び出しや再試行のたびにカードを増やさない。

最初に実現する代表例は次の通り。

1. Grok が「このアイデアの MVP を実装して」と依頼する。
2. かんばんの「受付」にカードが現れ、選ばれたワークフローと担当を確認できる。
3. Product が仕様を作り、Developer が実装し、QA が同一成果物を検証する。
4. QA 不合格時は修正理由を添えて Developer に戻す。所定回数を超えたら管理者に判断を求める。
5. 管理者が差分と検証結果を確認し、成果物を受け入れる。
6. カードが「完了」になる。本番公開は独立した承認・実行の契約で扱う。

## 2. 設計上の決定

| ID | 決定 | 理由 |
| --- | --- | --- |
| D01 | 既存 Control Plane を管理 UI として拡張する | 既存の認証・配置・Gateway 接続を活かす |
| D02 | Gateway の PostgreSQL を Task、Workflow Run、Job、承認、操作履歴の正とする | Grok と UI のどちらからでも同じ状態を扱い、二重管理を避ける |
| D03 | Agent が担当選択とワークフローの進行判断を実装する | 役割・能力・実行コンテキストの責務に揃える |
| D04 | Agent の進行判断は Gateway の内部 API で検証・確定する | Agent に別の正となる業務 DB を作らず、Policy と状態更新を統一する |
| D05 | Worker は渡された一回の仕事を実行し、後続工程を起動しない | 実行と進行判断を分離する |
| D06 | カードの列は実行状態から導出し、直接編集しない | ドラッグ操作と実処理を一致させる |
| D07 | 役割・Skill・Harness・Workflow の有効設定は Git と不変な Release で管理する | 現在の Git 管理、イメージ固定、Argo CD と整合する |
| D08 | UI 編集は Draft → 検証 → Release → 適用確認を通す | 保存済み・反映待ち・反映済みを区別する |
| D09 | 実行開始時に設定と入力成果物を固定する | 設定編集で進行中の仕事が変わらないようにする |
| D10 | まず定型ワークフローを実装し、動的 Planner は後続段階とする | 失敗・再開・引き継ぎの挙動を先に確定する |
| D11 | 成果物の受け入れと、本番公開の承認を別種の Decision とする | 「完了」に移動するだけで本番権限が発生しないようにする |
| D12 | 初期 UI は既存 FastAPI を維持し、テンプレートと JavaScript を分離する | 全面移行を必須にせず、かんばんと非同期操作に対応する |
| D13 | Writer と Marketing は専用 Worker pool として追加する | 制作・企画に必要な実行能力、Skill、設定、受付枠を個別に管理する |

既存 Control Plane の architecture 文書には「将来、business lifecycle authority を Control Plane に移す」方針がある。本設計では、その移行を延期し D02 を採用する。実装時に同文書を更新し、二つの方針が併存しないようにする。

## 3. 現状と不足点

### 3.1 確認範囲

2026-09-20 時点のローカル作業ツリーを参照した。未コミットの変更も含む。稼働中 Pod、デプロイされたイメージ、接続先サービスとの一致は未確認であり、以下の「既存」はローカル実装上の意味である。

この章は設計を書いた時点の棚卸しであり、その後の実装状況は
`ai-business-platform/gateway/docs/task-ledger.md`（Phase 1/2 の実装範囲と、まだ
保証していないこと）を参照する。Phase 1/2 で解消した項目も本章の記述は当時の
ままにしてある。

### 3.2 実装の棚卸し

| 対象 | 既存 | 本設計で追加・変更すること |
| --- | --- | --- |
| Control Plane | プロジェクト一覧・詳細、取り込み、手動同期、Job 登録 API | タスクかんばん、設定編集、承認 UI、自動同期、複数 Job の追跡 |
| Control Plane DB | SQLite の `projects`、`events`、プロジェクトごとの `current_job_id` | Task/Step/Attempt の読み取りモデル。現 DB を業務状態の正にはしない |
| Agent | action → executor/profile/schema/skills の固定ルーティング、profile digest、dispatch 重複判定 | バージョン付きカタログ、複数担当候補、Workflow Controller、設定の適用状態 |
| Worker | Codex 実行、分析レポート、ローカル成果物、状態照会、cancel endpoint | durable queue/outbox、正確な cancel 状態、実行枠、成果物 manifest、設定スナップショット |
| Gateway | MCP、Job と Project、callback、QA 判定、本番承認の基礎 | Task/Workflow/Command API、イベント購読、永続 scheduler、カタログ集約、認証主体の分離 |
| 設定 | Agent profiles と capabilities、Worker skills と共通 AGENTS.md | UI Draft と Git 変更の連携、Release manifest、Agent/Worker の互換性確認 |

### 3.3 実装時に解消する既存の差分

- Agent の capability には Product、QA、Growth が有効登録されているが、`agent-runtime.md` の有効一覧は古い。UI の表示根拠は実機から報告された Release と capability にする。
- Gateway の MCP enum、Control Plane の `SAFE_ACTIONS`、Agent registry、Worker executor の一覧が一致していない。公開可能な能力は後述する積集合で算出する。
- `submit_business_idea` は Project 登録であり、ワークフロー開始ではない。
- Control Plane の `current_job_id` だけでは、一つの事業内の複数タスクや複数工程を表せない。
- Gateway callback は Job 完了で直接 Project の単一状態・成果物欄を書き換える。新しい Task 同士が互いの Project 状態や成果物を上書きしないよう変更が必要。
- 一般 Job の dispatch は API の background task に依存している。動画用の永続 runner は存在するが、一般 Job の scheduler が完成しているとは扱わない。
- Worker の callback は best effort で、再起動時は実行中 Job を失敗化する。通知復旧と安全な再試行を追加する必要がある。
- Worker の cancel API は、実処理が継続中でも応答に `CANCELLED` と返す経路がある。Gateway まで含めて「停止要求済み」と「停止完了」を分ける。
- QA の入力は `source_worker_job_id` に依存し、同一 Worker のローカル workspace をコピーしている。複数 Worker への配分には成果物の移送が必要。
- QA レポートの実行成功と `pass/fail/inconclusive` は既存実装でも分離されている。この区別を UI まで維持する。
- 現行の role/Skill 制約にはプロンプトによる指示が含まれる。文書を置くだけで OS のアクセス制御が成立するとは扱わない。
- `technical-writer-v1` と `content.article` の入力 schema は存在するが、Agent registry の同 action は無効。Writer 専用 Worker は未実装であり、フラグ変更だけで稼働可能とは扱わない。
- `growth.plan` は既存の Growth 分析であり、Marketing 全般の企画・制作指示とは分けて拡張する。Marketing 専用 Worker は新設対象。

## 4. スコープ

### 4.1 最初の完成形に含めるもの

- 依頼単位のかんばんと、工程・実行履歴の詳細。
- UI/MCP 共通のタスク登録、進捗照会、入力回答、停止、再試行。
- Product、Developer、QA、Writer、Marketing、Growth の担当・説明・Skill 関連付けの編集。
- Skill と Harness の管理対象ファイル編集、バージョン差分、検証・適用状況。
- Worker の能力、稼働状態、実行枠、受付停止、適用設定の確認。
- MVP 作成、修正、分析を始点とする定型 Workflow。
- 人間の判断待ち一覧と成果物レビュー。
- 権限に基づいた UI 操作、変更・実行の監査記録。
- Writer/Marketing 専用 pool と、企画 → 制作 → 内容レビューの Workflow。詳細は §27。

### 4.2 後続段階

- Planner が任意の工程グラフを生成する動的な計画。
- 同一 pool 内での複数 Worker による並列実行と負荷分散。Writer/Marketing 間の成果物引き継ぎは今回の対象。
- Git 由来 Skill の追加依存関係の自動導入。
- ビジュアルな Workflow グラフエディタ。
- 組織単位のマルチテナント運用。

### 4.3 本設計に含めない操作

UI からの任意 shell、任意 Kubernetes manifest 適用、秘密情報の本文編集、汎用リモート実行は提供しない。既存の本番公開契約を、汎用かんばん操作として追加しない。これらは現行基盤の実行境界を維持するためのスコープ決定である。

## 5. 用語と責務

### 5.1 ドメインモデル

| 用語 | 意味 | 例 |
| --- | --- | --- |
| Project | 継続して育てる事業・アプリ | 家計管理サービス |
| Task | ユーザーの一つの依頼。かんばんカード | CSV 取り込みを追加する |
| Workflow Definition | 再利用できる工程と遷移規則 | `feature-build-v1` |
| Workflow Run | Task に対する一回の計画・進行 | 初回実装、要件変更後の第２回 |
| Step | 入出力と担当が定義された工程 | 仕様化、実装、QA |
| Attempt | 同じ工程を実行し直した一回 | QA 指摘後の２回目の修正 |
| Job | Gateway が認可・受理した一回の実行要求 | `code.fix` の一件 |
| Worker Execution | Worker が受理した実行 | `worker_job_id` |
| Agent Definition | 論理的な担当。常駐プロセス数ではない | Developer |
| Skill | 特定作業の手順、資料、スクリプト | 回帰検証の証拠を作る |
| Harness | 指示と実行制御の組 | 作業範囲、ツール、時間、検証規則 |
| Config Release | 互換性を検証した不変な設定の集合 | profiles/skills/workflows の固定版 |
| Artifact | 版と出所を持つ成果物 | PRD、patch、QA レポート |
| Decision | 人間等の権限主体による対象付き判断 | この成果物を受け入れる |

関係は `Project 1:N Task 1:N WorkflowRun 1:N Step 1:N Attempt` とする。Attempt は最大一つの Job を持つ。Job の配送再試行では Attempt を増やさず、実際の再実行では新しい Attempt と Job を作る。Task は同時に一つの active Workflow Run を持つ。

### 5.2 コンポーネントの責務

| コンポーネント | 読み取り・判断・操作 | 永続化 |
| --- | --- | --- |
| Control Plane | 人間向け UI、フォーム、差分、操作要求、表示同期 | 再構築可能な読み取りモデル、UI preferences、session |
| Gateway | 認証、認可、入力検証、業務状態確定、Job 登録、予算、イベント、承認 | PostgreSQL の業務状態と監査の正 |
| Agent Workflow Controller | 固定 Workflow の評価、担当候補選択、次工程の提案、引き継ぎ生成 | Gateway の内部 API 経由で Run/Step 状態を確定 |
| Agent Dispatch API | 固定された設定・配分先の検証、managed context 注入、Worker への配送 | 配送の補助台帳。業務状態の正にしない |
| Worker | 一回の仕事、制限の強制、成果物生成、状態・能力報告 | 実行 DB、成果物、callback outbox |
| Config Controller | 設定変更の検証、Git 変更作成、CI/Argo 状態の追跡 | Gateway の設定変更レコード、Git と Release artifact |

Config Controller は新しい論理コンポーネントであり、初期は Gateway リポジトリの別プロセスとして実装する。Control Plane 自体に Git/Kubernetes の変更用資格情報を持たせない。

### 5.3 通信経路

```text
人間のブラウザ → Control Plane UI/BFF ─┐
                                      ├→ Gateway API / MCP → PostgreSQL
Grok Bot ──────────────────────────────┘          ↑       ↓
                                       Agent Workflow Controller
                                                 ↓ 提案を確定
Gateway の永続 dispatch → Agent Dispatch → Worker
          ↑                                  ↓
          └───────── 実行イベント / 成果物参照 ─┘

Control Plane ← Gateway のイベント・snapshot
設定編集 → Gateway Config API → Config Controller → Git / CI / Argo CD
                                                   ↓
                                    Agent / Worker の適用報告
```

外部公開は既存 Gateway に集約する。UI は既存通り Tailnet 内を基本とする。Agent の内部進行 API、Worker 管理 API、設定適用 API を Grok に直接公開しない。

## 6. 画面構成

### 6.1 ナビゲーション

| ページ | パス案 | 主な目的 |
| --- | --- | --- |
| 概要 | `/` | 判断待ち、進行中、異常、最近の完了 |
| タスク | `/tasks` | かんばん・一覧、検索、依頼作成 |
| タスク詳細 | `/tasks/{id}` | 指示、工程、実行、成果物、判断 |
| プロジェクト | `/projects`、`/projects/{id}` | 事業単位のタスクと成果物 |
| エージェント | `/agents`、`/agents/{id}` | 役割、能力、Skill、担当状況 |
| スキル | `/skills`、`/skills/{id}` | 手順と関連ファイル、利用先、版 |
| ワークフロー | `/workflows`、`/workflows/{id}` | 工程、担当、分岐、上限 |
| Worker | `/workers`、`/workers/{id}` | 稼働、実行枠、受付状態、設定 |
| 設定変更 | `/changes`、`/changes/{id}` | Draft、差分、検証、反映状況 |
| 判断待ち | `/decisions` | 入力依頼、成果物レビュー、本番承認要求 |
| 操作履歴 | `/activity` | 人間・Bot・システムの監査 |

Harness はエージェント詳細の「実行ルール」として通常表示し、共有 Harness の編集は設定変更画面から行う。初期ナビゲーションを実装用語で埋めない。

### 6.2 概要

画面上部に「あなたの判断待ち」「実行中」「停止・失敗」「完了」を件数で表示する。次に対応が必要なものを優先し、単なるイベント数を重要度の指標にしない。

Worker がすべて受付停止している場合は「実行できる Worker がありません」と表示し、該当するタスクと Worker への導線を出す。設定適用失敗、イベント同期遅延も別の状態として表示する。

## 7. タスクかんばん

### 7.1 標準列

| 列 | 表示内容 |
| --- | --- |
| 受付 | Draft、開始待ち、担当確定待ち |
| 計画 | Product または計画工程が進行中 |
| 実装 | Developer または実行工程が進行中 |
| QA | 検証工程が進行中 |
| レビュー待ち | 成果物の受け入れを待つ |
| 完了 | 必要な成果物と判断が揃った |

「入力待ち」「実行待ち」「一時停止」「実行失敗」「状態確認中」は独立した状態として保持し、通常は本来の工程列にバッジを重ねる。「要対応」フィルターで横断して集める。中止済みは既定表示から除外し、フィルターで表示する。Growth 等は `analysis` stage を「分析」、Writer は `content_creation` を「制作」、内容レビューは `editorial` を「内容確認」として必要時だけ追加する。

列は Workflow Definition の `stage_key` と表示設定で決まる。Agent 名から列を推測しない。後続の並列工程対応ではカードを複製せず、Workflow の `display_stage` と実行中工程一覧を表示する。

### 7.2 レイアウト案

```text
タスク                          [プロジェクト ▼] [要対応] [検索] [+ 依頼]
最終同期 3 秒前                  表示: かんばん / 一覧

受付           計画           実装           QA             レビュー待ち
──────────     ──────────     ──────────     ──────────     ──────────
CSV 取込       MVP 要件       検索機能       通知設定       LP 改善
Grok から      Product        Developer      QA             あなたの確認
開始待ち       仕様整理中      テスト実行中    入力待ち       [成果物を見る]
優先度: 通常   1/3 工程完了    2 回目         理由を確認
```

### 7.3 カード項目

- タスク名、Project、作成元（人間/Grok/連携）。
- 現在の工程、担当 Agent、Worker の短い表示名。
- 主状態、補助状態、最後の進捗要約、経過時間。
- 優先度、期限、修正回数、未解決の入力依頼件数。
- 成果物またはレビューへの短い導線。

パーセント進捗は、分母が確定している工程やテストに限る。LLM の推測による「80% 完了」は表示しない。「3 工程中 1 工程完了」と「いまテスト中」を区別する。

### 7.4 カード移動の意味

`PATCH state` や `PATCH column` は公開しない。UI は Gateway が返す `available_commands` と `allowed_destinations` から操作を構成する。

| 操作 | 実際の command | 条件・効果 |
| --- | --- | --- |
| 受付 → 最初の工程 | `start` | 必須入力・能力・予算・設定を確認して Run を開始 |
| 実装完了 → QA | `advance` | 検証済み成果物がある場合だけ QA を生成 |
| QA/レビュー → 実装 | `request_changes` | 理由必須。新しい修正 Attempt を作る |
| レビュー → 完了 | `accept_deliverable` | 対象成果物の版を固定した受け入れ |
| 一時停止 | `pause` | 新しい工程の起動を止める。実行中の処理は継続 |
| 中止 | `cancel` | 実行停止を要求し、確定するまで「停止中」 |
| 失敗後の再実行 | `retry` | 再実行可否を確認し、新しい Attempt を作る |

実行中カードを「完了」に移動することはできない。自動進行する工程では通常ドラッグ不要とする。操作可能な移動先だけを強調し、不可理由を文章で表示する。

優先順位の並べ替えは列変更とは別操作である。待機中 Task の順位と priority のみ更新し、実行中 Job の横取りは行わない。マウスを使わなくてもメニューから同じ操作を実行できる。

command 受付時には「変更要求を受け付けました」と表示する。Worker の応答や Gateway の状態確定前に完了済みの見た目にしない。

### 7.5 詳細画面

| タブ | 内容 |
| --- | --- |
| 概要 | 目的、完了条件、現在の状況、次の予定、主要操作 |
| 工程 | 担当、依存関係、入出力、Attempt 履歴、修正理由 |
| 会話・指示 | 元依頼、追加指示、質問、回答、反映先 |
| 成果物 | PRD、差分、テスト、QA 判定、ダウンロード |
| 実行 | Worker、設定版、モデル、制限、使用量、ログ |
| 履歴 | 状態遷移、担当変更、設定選択、判断の監査 |

詳細の先頭に「いま必要な操作」を一つ置く。通常の利用では JSON の全文や内部 ID を読む必要がない構成にする。内部情報は「実行の詳細」から確認できる。

### 7.6 指示・担当の変更

- 開始前：目的と完了条件を編集できる。変更履歴を保持する。
- 実行中：コメントと実行指示を区別する。初期版は実行プロンプトを途中で差し替えない。
- 追加指示には `note_only`、`next_attempt`、`restart_required` の反映方式を付け、UI は日本語で表示する。
- 受け入れ条件が変わる追加指示は input revision を増やし、旧条件の QA を最新の証拠として再利用しない。
- 今すぐ反映する場合は現在の実行を停止し、停止確定後に新しい入力版で再実行する。
- 担当変更は capability を満たす Agent 候補のみ選択可能。開始済み Attempt の担当は変更せず、次回以降へ適用する。
- Worker の指定は通常「自動」。診断用の固定指定は Operator の詳細設定に限定し、適合性検証を省略しない。

## 8. エージェント・Skill・Harness の管理 UI

### 8.1 エージェント一覧と詳細

一覧には役割名、目的、有効状態、対応 action、Skill 数、稼働 Task 数、設定版を表示する。役割は論理的な担当であり、Worker やモデルと一対一に固定しない。

詳細には次の項目を持つ。

| 項目 | 編集方式 | 適用先 |
| --- | --- | --- |
| 表示名、説明、得意な仕事 | フォーム | 次の Config Release |
| 役割指示 | Markdown editor とプレビュー | profile |
| 対応 action | 管理済み capability から選択 | routing binding |
| 使用 Skill | 対応条件のある一覧から選択 | Skill ID と digest |
| 入出力形式 | schema 一覧から選択、詳細編集 | schema revision |
| モデル設定 | 検証済み runtime の候補から選択 | model policy |
| 時間・実行量上限 | 数値フォーム、上位制限も併記 | execution limits |
| 実行ルール | Harness の選択と継承内容の表示 | harness revision |
| 引き継ぎ | Workflow へのリンク | Workflow 内で編集 |

「引き継ぎ先」を Agent と Workflow の二か所で独立編集しない。Agent 画面では利用中 Workflow と担当工程を表示し、編集先は一つにする。

### 8.2 Skill 管理

Skill 詳細は `SKILL.md`、付属 scripts/references/assets、対応 action、利用 Agent、検証結果、版履歴を表示する。Skill 名を表示するだけでなく、実行に固定された本文 digest を確認できるようにする。

初期フォームでは新規 Skill の名前・説明・本文を編集できる。既存ファイルの編集は管理対象パス内に限定する。実行スクリプトの変更には構文検証と隔離テストを必要とし、依存パッケージの自由な追加は初期対象外とする。

Skill の削除は参照ゼロの Draft に限る。有効 Release が参照する Skill は廃止扱いにし、新規採用を止めても履歴と実行再現用の版を保持する。

### 8.3 Harness 管理

Harness は次の二層として表示・管理する。

1. 指示：共通ルール、報告形式、作業の進め方。Markdown とその版。
2. 実行制御：tool/capability allowlist、filesystem/network policy、timeout、終了・検証方法。型付き設定と executor 実装。

有効権限は Platform Policy、Project Policy、Role Policy、Job 制限の積集合とする。文章の変更で実行権限を拡大できない。

OS/ネットワークの分離を必要とする変更は Worker の実行クラス変更として扱い、検証済みの image/manifest へ変換する。現行の Pod 境界内で役割別の厳密な隔離が必要になった場合は、別 Worker pool または Job Pod に分ける。

### 8.4 モデル設定

`provider`、`model_id`、runtime が対応する reasoning 設定、task/attempt の上限を構造化して保持する。自由入力した CLI フラグをそのまま実行しない。

初期版は既存 Codex executor の対応範囲に限定する。候補は Config Release の runtime compatibility metadata と実機検証から作り、名称や価格を UI に固定しない。使用量や料金が取得できない場合は「不明」と表示し、ゼロとして集計しない。

## 9. Worker 管理 UI

### 9.1 一覧と詳細

Worker の論理 ID は Pod 名から独立させる。再起動ごとの `instance_id` と起動時刻を別途保持する。

一覧は Worker 名、pool、接続状態、受付状態、実行中/上限、待機数、実行クラス、設定一致状況を表示する。詳細には executor、対応 action、image/runtime/Skill bundle digest、最近の実行、heartbeat、診断結果を出す。

接続状態は `HEALTHY / DEGRADED / OFFLINE / UNKNOWN`、受付状態は `ACTIVE / DRAINING / PAUSED` として分離する。heartbeat が新しいことと、対象 action を実行できることを同一視しない。

### 9.2 更新操作

| 操作 | 反映方式 | 実行中 Job への影響 |
| --- | --- | --- |
| 新規受付を停止 | runtime command | 継続 |
| 受付を再開 | runtime command | 継続。readiness と設定一致を確認 |
| 同時実行数を変更 | bounded runtime override | 上限を下げても既存実行を強制停止しない |
| timeout 等の既定値を変更 | Config Release | 既存 Attempt は旧値 |
| 実行クラス・image・能力を変更 | Git/CI/Argo CD | drain 後に更新 |
| 接続診断 | 型付き診断 command | ヘルス・設定・必要能力を確認 |

runtime override は Gateway の単一の desired state として保存し、理由、版、適用時刻、任意の期限を持つ。Agent scheduler が参照し、Worker が observed revision を報告する。Pod 内だけに値を保存しない。

設定の通常値は Git Release、明示的な運用 override は Gateway という優先順位を固定する。UI に通常値・上書き値・実効値を併記する。期限切れで受付が意図せず再開しないよう、受付停止の既定期限は無期限とする。

Gateway が dispatch 前に実行枠を予約し、Worker も受付時に原子的に上限を検証する。画面上の同時実行数だけでは制限を実現したことにしない。

## 10. タスク・工程・実行の状態設計

### 10.1 Task の状態

Task は「業務上の状態」「工程」「操作制御状態」を分離する。

| フィールド | 値 | 意味 |
| --- | --- | --- |
| `status` | `DRAFT` | 必須情報の入力途中 |
| 同上 | `READY` | 開始できる依頼として登録済み |
| 同上 | `ACTIVE` | 工程を進めている。実行枠待ちを含む |
| 同上 | `WAITING_INPUT` | 利用者への質問や情報提供を待つ |
| 同上 | `WAITING_REVIEW` | 固定された成果物の判断を待つ |
| 同上 | `BLOCKED` | 設定不整合、予算不足、結果不明等で進行不能 |
| 同上 | `COMPLETED` | Workflow の完了条件を満たした |
| 同上 | `FAILED` | 自動復旧の上限を超えて終了した |
| 同上 | `CANCELLED` | 必要な実行停止が確定して終了した |
| `stage_key` | `intake / planning / implementation / qa / analysis / content_creation / editorial / review / done` | カードの所属列 |
| `control_state` | `ACTIVE / PAUSE_REQUESTED / PAUSED / CANCEL_REQUESTED` | 起動許可や停止要求 |

`COMPLETED / FAILED / CANCELLED` はその Workflow Run の終端である。再開操作は新しい Run を作り、旧 Run と終端の証拠を保持する。Task 自体の表示は新しい active Run に追従する。

### 10.2 主な遷移

| 現在 | イベント・操作 | 次の状態 | 条件 |
| --- | --- | --- | --- |
| DRAFT | 必須入力を保存 | READY | 入力 schema 適合 |
| READY | start | ACTIVE | Workflow、担当、設定版を固定できる |
| ACTIVE | input requested | WAITING_INPUT | 質問、必要項目、再開先を保存 |
| WAITING_INPUT | answer_input | ACTIVE | 対象質問と入力 revision を検証 |
| ACTIVE | deliverable ready | WAITING_REVIEW | 必須成果物・検証・対象 digest が揃う |
| ACTIVE | 単発 action 完了 | COMPLETED | `single-action-v1` 等、Workflow が出力検証のみを完了条件とする場合 |
| WAITING_REVIEW | accept_deliverable | COMPLETED | Decision の対象と現在の成果物が一致 |
| WAITING_REVIEW | request_changes | ACTIVE | 理由と戻し先、残予算を確認 |
| ACTIVE | QA fail | ACTIVE または FAILED | 修正上限内なら修正工程、それ以外は終了 |
| ACTIVE | QA inconclusive | WAITING_INPUT | 人間が検証条件の補足や再実行を判断 |
| 非終端 | 実行に必要な条件が失われる | BLOCKED | reason code と復旧方法を記録 |
| BLOCKED | 条件回復・resume | 復帰先 | 保存した工程から再評価し、終端 Job を再利用しない |
| 非終端 | cancel と停止確定 | CANCELLED | すべての active 実行が停止または既に終了 |
| FAILED/CANCELLED/COMPLETED | reopen | READY | 新 Run の目的と再利用する成果物を明示 |

pause は `status` と独立する。要求を受けた時点で後続起動を止め、実行中 Job の終端を待って `PAUSED` にする。pause 中に必要成果物が揃っても、後続起動や自動完了の確定は resume 後に評価する。resume は現在の工程・入力条件を再検証する。

### 10.3 Step と Attempt

Step 状態は `PENDING / READY / RUNNING / WAITING_INPUT / WAITING_REVIEW / SUCCEEDED / FAILED / CANCELLED / SKIPPED` とする。スキップは Workflow で optional と指定された工程に限る。

Attempt は入力、Agent、Worker 要件、設定 Release、開始・終了時刻、Job ID、結果、失敗分類を保持する。過去の Attempt を上書きして「再実行中」に戻さない。

Job の実行結果と品質判定を分ける。

- Job `SUCCEEDED` + QA `pass`：検証を実行し、対象が条件を満たした。
- Job `SUCCEEDED` + QA `fail`：検証を実行し、対象に不合格項目があった。
- Job `SUCCEEDED` + QA `inconclusive`：レポートはできたが判定できなかった。
- Job `FAILED_FINAL`：検証自体が正常に完了していない。

### 10.4 Job・停止の共通契約

Gateway/Agent/Worker 間で `QUEUED / DISPATCHING / ACCEPTED / RUNNING / CANCEL_REQUESTED / RECONCILING / SUCCEEDED / FAILED_FINAL / CANCELLED` を正規化する。配送の再試行待ちは `dispatch_status` で別管理する。

Worker は cancel を受けてもプロセス群の停止と成果物保存が終わるまで `CANCEL_REQUESTED` を返す。終了イベントが先に確定した場合は終了結果を維持し、cancel command は `NO_EFFECT_ALREADY_FINISHED` とする。

停止と成功が競合した場合、Gateway は整合する Worker 終了証拠により Job 終端を確定する。Task の cancel intent は独立して後続起動を止める。停止不明の Job がある限り Task は「停止中 / 状態確認中」であり、停止済みと表示しない。

### 10.5 状態導出の責任

Gateway が Task 状態、stage、available commands を返す。Control Plane は独自の状態遷移ロジックを持たない。Project の集約は `active_task_count`、`attention_count`、`latest_accepted_deliverable`、`release_candidate` として表し、単一の最新 callback で全 Project の状態を決めない。

## 11. 担当選択とワークフロー

### 11.1 利用可能な capability

公開可能集合を次の積集合とする。

```text
公開可能な action
 = Gateway が主体・Project・環境に許可する action
 ∩ 有効 Config Release の Agent binding
 ∩ 互換性を検証した Worker executor
```

実行枠の一時的な不足は capability を削除せず `availability=busy` とする。実行できない設定は `disabled / incompatible / offline` と理由を返す。Grok と UI は同じカタログを参照する。

### 11.2 ルーティングの優先順位

1. 固定された Workflow Step が要求する action と入出力 contract を解決する。
2. Project の制約、環境、設定版、実行クラスで候補 Agent を絞る。
3. 人間が指定した適合する担当があれば採用する。
4. それ以外は Workflow の既定担当を採用する。
5. 候補なしは `BLOCKED_CAPABILITY_UNAVAILABLE`。自動で広い権限の担当に置換しない。
6. Worker を互換性、成果物の所在、空き実行枠、優先順位で選ぶ。

選択理由、除外理由、候補、最終担当を `routing_decision` として保存する。モデルや担当を自動 fallback する場合は、Release に定義された候補順のみ使い、変更を履歴に残す。初期版は fallback 無効を既定とする。

Worker の選択案は Agent Workflow Controller が作り、Gateway が実行枠と revision を検証して予約・確定する。Agent Dispatch API は確定済み Worker にだけ配送し、独自に配分先を変更しない。枠の競合で予約できなければ、配送前に Controller が候補を再評価する。単発 Job も同じ予約経路を通す。

### 11.3 初期 Workflow

| Workflow | 工程 | 完了条件 |
| --- | --- | --- |
| `mvp-build-v1` | Product → Developer → QA → 人間レビュー | PRD、変更成果物、QA pass、受け入れ |
| `feature-build-v1` | 入力確認 → Developer → QA → 人間レビュー | 指定条件を満たす変更成果物と受け入れ |
| `bug-fix-v1` | 再現情報確認 → Developer → QA → 人間レビュー | 再現テストを含む修正の確認 |
| `growth-analysis-v1` | 指標入力確認 → Growth → 人間レビュー | 根拠付きの分析と提案 |
| `content-production-v1` | 入力確認 → Writer → 内容レビュー → 人間レビュー | 根拠・ブランド・形式を確認した原稿と受け入れ |
| `marketing-content-v1` | Marketing 企画・制作指示 → Writer → 内容レビュー → 人間レビュー | 企画に紐付く原稿、計測計画、受け入れ |
| `single-action-v1` | 一つの action → 結果表示 | action の出力 contract 適合 |

最初に実装する Workflow は `mvp-build-v1` と既存互換の `single-action-v1`。他は同じ定義形式で追加する。`test.run` が提供する実能力は Worker の contract で明示し、汎用テスト実行を名称だけから仮定しない。

### 11.4 定義例

以下は追加する設定形式の例であり、現在の loader がこの YAML に対応しているという意味ではない。

```yaml
id: mvp-build-v1
version: 1.0.0
entry_step: plan
completion: accepted_deliverable
limits:
  max_revision_cycles: 2
  max_job_attempts_per_step: 3
steps:
  plan:
    stage: planning
    action: product.plan
    agent: product-manager
    output_contract: prd-v1
    on_success: implement
  implement:
    stage: implementation
    action: code.build
    agent: software-engineer
    input_from: plan.prd
    output_contract: code-change-v1
    on_success: qa
  qa:
    stage: qa
    action: qa.review
    agent: qa-engineer
    input_from: latest_code_change
    acceptance_from: plan.prd
    output_contract: qa-report-v1
    on_pass: review
    on_fail: fix
    on_inconclusive: request_input
  fix:
    stage: implementation
    action: code.fix
    agent: software-engineer
    input_from: latest_code_change
    feedback_from: qa.report
    output_contract: code-change-v1
    on_success: qa
  review:
    stage: review
    kind: human_review
    decision: accept_deliverable
    on_request_changes: fix
```

循環は修正用の明示された back edge に限定し、`max_revision_cycles` で上限を持つ。CI は未知の遷移先、到達不能な工程、必要出力のない参照、無制限循環、capability 不整合を拒否する。通信の再配送回数は修正回数に含めず、実作業の予算とは別に追跡する。

### 11.5 Workflow Controller の実行方式

Agent に API プロセスとは別の Workflow Controller を置く。Gateway の内部 API から、再評価が必要な Run の lease と state revision を取得する。

Controller は LLM を使わず固定定義を評価し、`create_attempt`、`request_input`、`request_review`、`complete_run` 等の次アクションを提案する。Gateway は lease token、expected revision、遷移条件、Policy を検証し、Step/Attempt/Job/outbox を同一トランザクションで更新する。

初期値は lease 30 秒、10 秒ごとの更新とする。期限切れの提案は fencing token により拒否する。Controller が再起動して同じ工程を再評価しても、`run_id + step_instance_id + attempt_number` の一意制約で二重 Job を作らない。

HTTP リクエストを Workflow 全体の完了まで開き続けない。Task 作成・開始は受理結果を返し、進捗はイベントと照会 API で取得する。

### 11.6 引き継ぎ contract

工程間では次の内容を含む構造化された handoff を渡す。

- `task_id / run_id / step_id / attempt_id`。
- input revision と、目的・完了条件の snapshot。
- 入力 Artifact ID、SHA-256、成果物 schema version。
- 直前工程の要約、未解決事項、QA 指摘と根拠。
- 対象 repository、base commit、変更成果物 manifest。
- 許可 action、使用する Config Release、適用 limits。

全文会話ログを無制限に継承しない。重要な決定は Task の構造化入力、成果物は Artifact、補助説明は制限付き context とする。要約を作り直しても元の根拠へたどれる参照を残す。

## 12. 成果物・QA・レビュー

### 12.1 成果物 manifest

Artifact に `artifact_id`、kind、media type、size、digest、producer attempt、input revision、保存先、保持期限、アクセス区分を記録する。

コード変更は base commit SHA だけでは識別できない。現行 Worker は未コミットの変更を成果物として残すため、次を含む `code-change` manifest の digest で特定する。

- repository の論理 ID と base commit SHA。
- patch digest、追加・変更・削除ファイルの manifest digest。
- 必要な untracked file を含む workspace snapshot digest。
- ビルド・テストの evidence artifact ID。

同じ HEAD でも patch が異なれば別成果物である。QA は manifest を指定して workspace を復元し、その manifest digest を結果に返す。`code.fix` も直前成果物から開始し、元 repository の新しい clone に修正指示だけを渡して既存変更を失わないようにする。

### 12.2 保存と取得

初期版は既存 Worker PVC に成果物を保持し、Gateway の artifact metadata と取得 proxy を追加する。Worker は finalize した snapshot を以後変更しない。QA は snapshot のコピーで実行する。

同一 Worker に成果物を固定する初期方式では scheduler に artifact affinity を課す。必要な Worker が停止していれば待機し、参照できない成果物のまま別 Worker を選ばない。Writer/Marketing pool を有効化する Phase 3B では Gateway 経由の認証付き Artifact 移送を追加し、実行前に digest を確認する。共有 artifact store はその後の拡張とする。

UI は認証付き Gateway/BFF 経由で成果物を取得する。ストレージ URL や Worker 用 token をブラウザへ渡さない。HTML preview は管理 UI と分離した origin または script を制限した sandbox で表示し、ダウンロード名・パスをサーバー側で検証する。

### 12.3 出力検証

Worker は required key の存在だけでなく JSON Schema による型・値・サイズ検証を行う。Gateway は受理する結果の最小 contract と provenance を再検証する。

QA pass には受け入れ条件ごとの verdict、根拠、対象 digest が必要。要件が変わった、成果物が変わった、別 Run の成果物を指定した場合は旧 pass を再利用しない。

### 12.4 判断の種類

| 種類 | 判断対象 | 効果 |
| --- | --- | --- |
| `input_answer` | question ID、入力 revision | 必要情報を補い工程を再評価 |
| `accept_deliverable` | Task、Run、成果物集合 digest、入力 revision | 当該成果物の受け入れと Workflow 完了 |
| `request_changes` | 同上 | 修正理由付きで次 Attempt を生成 |
| `production_approval` | 既存 release candidate の immutable target | 既存 Gateway の本番承認契約に従う |
| `config_change_approval` | 設定差分・検証結果・Release digest | 管理 Policy 上必要な変更の適用許可 |

Gateway がサーバー側で対象 hash を再計算する。クライアントが送った hash だけを信用しない。対象が変わった Decision は失効する。

QA `inconclusive` を一般の「受け入れ」で pass に変換しない。初期 MVP Workflow は pass を要求する。例外的な受け入れは、別の権限と明示された Workflow Policy を実装するまでは提供しない。

## 13. Agent / Worker の Harness・Skill 編集と反映

### 13.1 管理対象と所有先

Agent と Worker の双方を編集対象とする。物理的なファイルの保存先は現行 repository 境界を維持し、UI はコンポーネントをまたいだ関連を表示する。

| 対象 | 所有 repository / 管理パス案 | UI で更新する内容 |
| --- | --- | --- |
| Agent profile | `ai-business-agent/profiles/` | 役割、判断基準、報告責任 |
| Agent Harness | `ai-business-agent/harnesses/`（新設） | 担当選択・引き継ぎ・入力処理の指示と制御設定 |
| Agent Skill | `ai-business-agent/skills/`（新設） | 計画・分類・引き継ぎ等で使う手順と付属資料 |
| Workflow | `ai-business-agent/workflows/`（新設） | 工程、担当、分岐、上限、完了条件 |
| Worker Skill | `ai-business-worker/skills/` | `SKILL.md`、scripts、references、assets |
| Worker Harness | `ai-business-worker/harnesses/`（正規化先） | 共通 AGENTS.md のソース、実行クラス別の制約設定 |
| capability/schema | Agent と Worker の contract 定義 | action と executor、入出力、適合性 |
| 配置設定 | 各 repository の `deploy/` と homelab | 許可されたフォーム項目から生成する設定変更 |

現行 Agent は固定ルーティングのサービスであり、Skill を自動実行する LLM runtime はない。Agent Skill の管理 UI は作成・版管理に対応し、`execution_target` と利用箇所を明示する。Controller の固定処理はコードで実装し、Agent Skill を実行する能力は consumer adapter と検証を追加して初めて有効化する。

Skill の `execution_target` は `agent / worker`、Harness の対象は `agent-controller / worker-executor` とする。Worker Skill は Agent の profile から参照されることがあるが、配置先と実行先を混同しない。

同じルールの本文を ConfigMap と Markdown の双方で手編集しない。Worker Harness の正規ソースから ConfigMap を生成する。既存 `codex-harness.yaml` は移行時に生成物として扱う。

### 13.2 編集体験

詳細画面の「編集」から Draft を作る。左にファイル一覧、中央に editor、右に説明・利用先・検証結果を表示する。Skill 本文と Harness 本文には Markdown プレビューを付け、設定ファイルは型付きフォームと詳細表示を併用する。

操作は「下書きを保存」「検証」「適用する」「過去の版に戻す」とする。「保存」と「稼働中環境に反映」は別の状態であり、成功メッセージも分ける。

変更の適用前に、影響する Agent、Worker pool、Workflow、次回実行、必要な再起動を示す。進行中 Attempt は使い続ける版を表示する。通常変更に毎回別の確認ダイアログは挟まず、差分画面の「適用する」が具体的な変更を確定する操作になる。

### 13.3 Draft と Release

```text
DRAFT → VALIDATING → VALIDATED → CHANGE_OPEN → BUILDING → READY
                                                        ↓
                            ACTIVATING → ACTIVE / ACTIVATION_FAILED
```

検証・CI 失敗時は該当段階を `VALIDATION_FAILED / BUILD_FAILED` とし、原因付きで編集可能にする。Draft を変更したら検証結果を無効化する。

`READY` は成果物ができた状態、`ACTIVE` は必要な Agent/Worker が digest 一致で使用可能と報告し、Gateway が新規 Run 用の active pointer を切り替えた状態である。

Release manifest は次を固定する。

- Agent profiles、Agent Harness/Skills、Workflow、schema の各 digest。
- Worker Harness/Skills bundle、executor/runtime image digest。
- 対応する API/contract version と互換性制約。
- source repository と full commit SHA。
- CI 検証結果、作成主体、作成日時、Release 全体の digest。

### 13.4 Git / CI 連携

UI Draft は Gateway DB に保存する。有効設定の正は Git commit と、その commit から作った不変な Release artifact とする。Draft DB の内容を直接 runtime へロードしない。

Config Controller は許可 repository/path のみ変更する GitHub App 等の専用資格情報を使い、変更 branch と PR を作る。UI は PR の差分と検証状況を表示する。merge 条件は repository Policy に従い、通常変更は条件を満たした時点で自動 merge 可能とする。

初期の細かな配置方式は現在の image packaging を維持する。Agent 定義と Worker Skill/Harness を image/生成 ConfigMap に組み込み、CI が digest を生成し、Argo CD が固定 digest を反映する。

設定ソースの merge/build と、稼働環境の deployment digest 更新を分ける。build 成功だけでは Argo CD の監視する本番配置参照を進めない。Config Controller が drain 条件を確認した後、管理対象の配置参照を変更して activation を開始する。外部から配置参照が変わった場合も observer が差分を検出し、互換性確認が済むまで新規実行を抑止する。

複数 repository にまたがる変更は一つの `change_set_id` で束ねる。すべての build が揃うまで active Release を切り替えない。Agent だけ新 Skill を参照し、Worker が旧 Skill のまま実行することを禁止する。

### 13.5 適用とロールバック

初期版は drain-and-switch を採用する。

1. 対象 Release を使う「新しい Run の開始」を止める。既存 Run の後続 Attempt は許可し、完了まで進められる状態を保つ。
2. 原則として旧 Release を使う active Run が終わるまで待つ。その後に Worker を drain し、未完了 Job がないことを確認する。待機中に旧版を消さない。
3. deployment の参照を更新して image/ConfigMap を反映し、各 consumer が Release digest と readiness を報告する。
4. Gateway が互換性と実効設定を確認し、active pointer を原子的に更新する。
5. 受付を再開し、UI を「反映済み」にする。

停止中・入力待ちの旧 Run が長く残る場合は、旧版を保持して適用待ちにするか、管理者が Run の移行/中止を選ぶ。Run の移行は新しい Run を作り、利用する成果物と再実行工程を明示する。旧 Run を無言で新設定に差し替えない。

初期の single-replica 構成では無停止更新を約束しない。複数版を同時に実行する必要が生じたら、Release ごとの Worker pool と Agent consumer を並行稼働する方式へ拡張する。

ロールバックも新しい activation 操作として記録する。旧 image digest、bundle、schema が取得・実行可能なことを確認し、失敗時は受付を止めたまま明示する。「Git を戻した」だけで反映済みとしない。

### 13.6 検証と競合

- Markdown/frontmatter、ID 一意性、パス traversal、サイズ、許可ファイル形式を検証する。
- capability、schema、Skill、Harness、Workflow の参照整合性を検証する。
- executable script と Harness の制御変更は隔離環境で検証する。
- 代表依頼の担当選択、出力 schema、権限制約を regression fixture で確認する。
- Prompt 本文の変更も動作変更として扱い、文字列差分だけで検証を完了しない。
- 編集元 commit を `base_revision` として保存し、外部 Git 更新と競合したら差分を表示する。新しい base に対して再検証する。
- 外部から merge された変更も取り込み、CI と activation を経た Release として UI に表示する。

### 13.7 設定の固定と緊急無効化

Run 開始時に既定 Release を固定し、Attempt には解決済みの具体的な設定 snapshot を記録する。継続実行で必要な旧 Skill/Harness の artifact を保持する。

通常の更新は次回 Run から使う。脆弱な Skill 等の緊急無効化は `revoked_release / revoked_skill_digest` として別の運用 command を用意し、旧版であっても新しい Attempt の起動を止める。実行中 Job の強制停止は別途明示した cancel 操作として扱う。

## 14. データモデル

### 14.1 Gateway PostgreSQL

既存 `projects / jobs / approvals / worker_events` を残し、段階的に拡張する。新テーブルの ID は UUID とし、UI 用の短い表示番号は別に採番する。

| テーブル | 主なフィールド | 制約・用途 |
| --- | --- | --- |
| `tasks` | id, project_id, title, objective, status, stage_key, control_state, active_run_id, priority, sort_rank, revision, created_by, source | Project 内に複数 Task。Task revision は操作競合検知に使用 |
| `task_input_revisions` | task_id, revision, objective, acceptance_criteria, context_refs, created_by | 入力の不変 snapshot |
| `workflow_runs` | id, task_id, workflow_id, workflow_version, config_release_id, input_revision, status, lease_owner, lease_until, fencing_token | Task ごとの active Run は最大一件 |
| `workflow_steps` | id, run_id, logical_key, cycle, stage_key, action, status, agent_binding, revision | `run_id + logical_key + cycle` が一意 |
| `step_dependencies` | step_id, depends_on_step_id, required_output | 工程の依存関係 |
| `step_attempts` | id, step_id, attempt_number, job_id, input_manifest, execution_snapshot, status, failure_class | `step_id + attempt_number` が一意。Job ID も一意 |
| `job_dispatches` | id, job_id, dispatch_id, request_hash, selected_worker, state, retry_at, lease_token | 同一配送の再送キーを保持。結果不明を扱う |
| `task_messages` | id, task_id, kind, body, author, applies_to, input_revision | コメント、追加指示、反映先 |
| `input_requests` | id, task_id, step_id, questions, state, answered_by, answer_revision | 質問単位の一意な回答と再開 |
| `decisions` | id, kind, task_id, run_id, target_digest, state, actor, expires_at | 成果物受け入れ等。本番承認は既存 approvals と明示的に関連付ける |
| `artifacts` | id, producer_attempt_id, project_id, kind, digest, size, storage_ref, manifest, expires_at | digest、所有 Project、出所を必須化 |
| `routing_decisions` | id, attempt_id, candidates, selected_agent, selected_worker, reason | 配分の説明可能性 |
| `commands` | id, principal_id, target_type, target_id, command_type, request_hash, idempotency_key, status, result | 操作要求と非同期の結果 |
| `platform_events` | cursor, event_id, aggregate_type, aggregate_id, aggregate_revision, type, payload, actor, occurred_at | 共通の更新 feed。immutable |
| `event_counter` | stream_id, last_cursor | commit 順に配信可能な cursor を割り当てる |
| `outbox` | id, event_id, destination, payload, state, next_attempt_at | 外部配送と Job dispatch の永続化 |
| `config_drafts` | id, kind, target_component, base_revision, content, content_digest, revision, created_by | UI 編集の下書き |
| `config_changes` | id, change_set_id, draft_digest, git_refs, validation_results, state | Git/CI との対応 |
| `config_releases` | id, digest, manifest, created_by, state | 不変な Release artifact の参照 |
| `config_activations` | id, release_id, target_pool, desired_revision, observed_revision, state | Release の反映・戻し操作 |
| `agent_catalog` | agent_id, release_id, definition, availability | Release から生成するカタログ |
| `workers` | logical_id, instance_id, capabilities, heartbeat_at, observed_release, status | 実行側の報告 |
| `worker_overrides` | worker_id, revision, accepting_jobs, max_concurrency, expires_at, actor | 一時的な運用設定の正 |
| `capacity_reservations` | id, worker_id, job_id, lease_token, expires_at, state | 受付枠の競合防止 |

既存 `jobs` に nullable な `task_id / attempt_id / config_release_id / actor_id` を追加する。既存クライアントの互換性を保ち、新規経路では必須とする。認可は actor と Project の scope に基づき、task_id を知っているだけではアクセスできない。

### 14.2 不変条件

1. 非終端の Workflow Run は Task ごとに一つ。
2. 同一 Step に非終端 Attempt は一つ。並列化は別 Step として表す。
3. 同一 Attempt に Job は一つ。
4. `principal + idempotency_key` は一意で、同一キー・異なる request hash は 409。
5. 同一 dispatch_id は同一 Job と request hash を指す。
6. 終端 Job の結果、入力 snapshot、使用設定、Artifact digest は上書きしない。
7. active Release は、必要な consumer の observed digest と一致するものだけ。
8. QA/Decision の対象は同一 Project/Run の許可された Artifact と input revision に限定する。
9. Project をまたぐ source_worker_job_id や artifact_id の参照は拒否する。
10. 旧 Attempt の遅延 callback で、新 Attempt の状態を戻さない。

DB の unique index、foreign key、transaction と API 検証を併用する。LLM の指示だけに不変条件を置かない。

### 14.3 Control Plane SQLite

`task_views / project_views / agent_views / worker_views / change_views / timeline_views / sync_checkpoint` を保持する。すべて Gateway snapshot とイベントから再構築可能にする。

Draft の正は Gateway に置くため、Control Plane の Pod 消失で保存済み設定編集を失わない。未保存 editor 内容は端末内の一時下書きとして扱い、共用端末への保存は選択可能にする。UI preference と session は業務データから分離する。

## 15. API 契約

以下は新設・拡張案である。現在の endpoint がすべて実装済みという意味ではない。

### 15.1 Gateway の UI/BFF 向け API

| Method / Path | 用途 |
| --- | --- |
| `GET /v1/tasks` | Project、状態、担当、要対応、検索、cursor による一覧 |
| `POST /v1/tasks` | 依頼と初期入力を作成。必要なら start を同時受付 |
| `GET /v1/tasks/{id}` | 集約 snapshot、revision、available commands |
| `PATCH /v1/tasks/{id}` | title、priority 等の許可 metadata。状態変更は不可 |
| `POST /v1/tasks/{id}/commands` | start/pause/resume/cancel/retry/request_changes/reopen 等 |
| `POST /v1/tasks/{id}/messages` | コメント・追加指示と反映方式 |
| `POST /v1/input-requests/{id}/answers` | 対象付き回答 |
| `GET /v1/tasks/{id}/runs` | Run と工程、Attempt の履歴 |
| `GET /v1/commands/{id}` | 受付・反映・失敗の結果 |
| `GET /v1/catalog` | 有効 Agent、capability、Skill、Harness、Workflow と適合性 |
| `GET /v1/workers` | Worker の状態と実効設定 |
| `POST /v1/workers/{id}/commands` | drain/resume/update_limits/diagnose |
| `GET /v1/artifacts/{id}` | metadata と provenance |
| `GET /v1/artifacts/{id}/content` | 認証・認可付き成果物取得 |
| `POST /v1/decisions/{id}/resolve` | 対象を固定した受け入れ・差し戻し |
| `POST /v1/config/drafts` | 対象と base revision を指定した下書き |
| `PATCH /v1/config/drafts/{id}` | 許可ファイル・型付きフィールドの編集 |
| `POST /v1/config/drafts/{id}/validate` | schema/参照/実行検証の受付 |
| `POST /v1/config/changes` | 検証済み Draft を change set として適用要求 |
| `GET /v1/config/changes/{id}` | Git、CI、Release、反映の状況 |
| `POST /v1/config/releases/{id}/activate` | 明示した pool への適用・ロールバック |
| `GET /v1/events?after=...` | cursor 付きイベント feed |
| `GET /v1/snapshot` | 一貫した全体 snapshot と再開 cursor |

Control Plane はこれらを `/api/` の同一 origin API として中継する。Gateway 内部の資格情報はサーバー側に保持する。

### 15.2 Task 登録例

```json
{
  "project_id": "sample-service",
  "title": "CSV の取り込みを追加する",
  "objective": "ユーザーが CSV を選択して明細を登録できるようにする",
  "acceptance_criteria": [
    "正常な CSV から明細を登録できる",
    "不正な行を理由付きで確認できる"
  ],
  "workflow_id": "feature-build-v1",
  "environment": "preview",
  "priority": "normal",
  "start": true
}
```

`source`、actor、適用 Release は認証主体・有効設定からサーバーが確定する。Bot が `source=human` を渡しても人間操作にはならない。初期実装にまだない Workflow を指定した場合は、利用可能な候補付きで拒否する。

### 15.3 command 例と共通応答

```json
{
  "type": "request_changes",
  "expected_revision": 12,
  "reason": "重複する明細を取り込んだ場合の挙動も検証してください",
  "parameters": {
    "target_step": "fix",
    "target_artifact_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
  }
}
```

更新要求は `Idempotency-Key` と `expected_revision` を使用する。Draft/metadata 更新では `If-Match` も利用し、競合時は最新 revision と再取得先を返す。二方式の値が両方存在して不一致なら拒否する。

非同期操作は 202 と `command_id / status=ACCEPTED / target_revision / status_url` を返す。command 状態は `ACCEPTED / APPLYING / SUCCEEDED / FAILED / NO_EFFECT` とする。Task 自体の状態と混同しない。

共通エラーは `code / message / field_errors / current_revision / retryable / correlation_id` を持つ。主な code は `REVISION_CONFLICT`、`CAPABILITY_UNAVAILABLE`、`INPUT_REQUIRED`、`ARTIFACT_MISMATCH`、`POLICY_DENIED`、`BUDGET_EXCEEDED`、`RESULT_UNKNOWN`、`CONFIG_NOT_APPLIED`。

### 15.4 MCP 公開面

| tool | 利用目的 |
| --- | --- |
| `list_capabilities` | 利用可能な仕事・入力形式・制約を知る |
| `list_workflows` | 定型の依頼経路を知る |
| `create_task` | 目的と完了条件から依頼する |
| `get_task` | 工程、担当、成果物、必要な入力を確認する |
| `list_tasks` | 認可された Project の依頼を一覧する |
| `add_task_instruction` | 追加指示と希望反映時点を登録する |
| `answer_task_input` | Agent からの質問に回答する |
| `request_task_action` | 主体に許可された pause/resume/cancel/retry 等 |

既存 `submit_job / get_job / wait_for_job / submit_business_idea` は互換経路として残す。`submit_job` 由来の新規 Job には `single-action-v1` Task を原子的に作成し、job_id と task_id の両方を返す。`submit_business_idea` の既存の意味は変更しない。

設定本文の編集、Harness の権限変更、Worker の能力変更、人間専用 Decision は初期 MCP に公開しない。Bot に許可する操作は UI の全操作からの部分集合とし、REST 経由でも同じ認可を行う。

### 15.5 内部 API

- Gateway：Run lease の取得/更新、次工程提案の確定、Worker inventory 受付、設定適用報告。
- Agent：Release と capability 詳細、dispatch の結果照会・再照合、固定設定付きの実行要求。
- Worker：状態/能力/実効設定の報告、冪等な実行受理、cancel、Artifact metadata/content、運用設定反映。

内部 payload は `contract_version` を持つ。外部リクエストに `_agent_context`、管理用 profile 本文、任意の Worker URL を渡させない。Gateway が context を組み立て、Agent/Worker が release manifest と照合する。

## 16. イベント・同期・同時操作

### 16.1 書き込みの流れ

すべての操作は Gateway で、認証 → 認可 → schema → revision → 遷移条件 → 業務更新 + command + event + outbox の順に処理する。外部 HTTP や Git 操作を DB transaction の中で待たない。

ロック順を `Project → Task → Run → Step → Job → Worker capacity → event counter` に統一する。同一種類の複数行を lock するときは ID 順とし、枠競合時は再評価する。実装時は複数経路で同じ順序を守ることをテストする。

イベントは `event_id / cursor / aggregate_revision / actor / correlation_id / causation_id / schema_version` を持つ。UI 由来と Grok 由来のイベントも同じ timeline に並ぶ。

### 16.2 cursor の欠落防止

単純な sequence 採番の大小だけを使って「cursor 以下はすべて commit 済み」と仮定しない。初期の単一 Gateway DB では、更新 transaction の最後に一行の `event_counter` を lock し、cursor 割り当てと event insert を行って commit する。

counter lock は commit まで保持するため、後続 cursor が先に可視化されない。これは低負荷の管理イベントに限定する。大量の実行ログはこの feed に全文を載せず、別の log artifact/stream に送る。

snapshot は整合した DB snapshot 上で業務データと最後の可視 cursor を読み、その cursor の次からイベントを取得する。期限切れ cursor には `SNAPSHOT_REQUIRED` を返す。

### 16.3 配信

初期値は Control Plane → Gateway のイベント取得を 3 秒間隔、開いているブラウザへの通知を同一 origin の SSE とする。SSE が使えない場合は 5 秒間隔の差分 polling に戻す。接続の再開 cursor と read model の更新を同一 SQLite transaction で保存する。

ブラウザは通知受信後に変更されたカードだけ再取得・更新する。画面全体の再読込、編集中フォームの上書き、操作中カードの不意な移動を避ける。

全表示には最終同期時刻を持たせる。取得失敗時は最終状態を残して「接続確認中」とし、新しい情報を要する操作はサーバー確認後に実行する。通信エラーだけで Job を失敗扱いにしない。

### 16.4 重複・競合

- 同じ操作の再送は同じ command_id と結果を返す。
- UI と Grok が同じ revision を変更した場合、先に確定した方を採用し、後続に 409 を返す。
- command の応答が失われた場合は idempotency key で結果を再取得する。新しいキーで無条件に再実行しない。
- Worker イベントは event_id と Job 内 sequence で重複排除する。dispatch_id、worker_job_id、認証された Worker が Job に結び付くことも検証する。
- 未知・古い callback は監査対象として隔離し、新しい Attempt の結果に流用しない。

## 17. 配送・障害復旧・実行上限

### 17.1 永続 dispatch

一般 Job にも durable outbox と scheduler を追加する。API プロセス終了で未配送 Job を失わない。Job が Gateway に登録済みかどうかと、Worker が受理したかどうかを区別する。

配送は同じ dispatch_id で再送する。Agent は Worker への応答消失時に即座に最終失敗にせず、同じ ID で照会・再送して受理結果を復元する。現在の `dispatch is in progress` が永久に残る経路をなくす。

実際に処理が始まった可能性がある場合は、結果照合が済むまで別 Worker へ同一 Job を配送しない。実行枠の lease 期限切れも「実行が止まった証拠」にはならない。結果不明の枠は隔離し、Worker の状態照会後に解放する。

### 17.2 Worker queue と callback

受理時に Job をローカル DB に commit してから 202 を返す。実行 loop は永続 queue を取得する。受理と実行開始を API request の生存期間に依存させない。

callback は状態更新と同じ transaction で outbox に保存し、成功まで上限付き backoff で再配送する。一定期間配送できなくても成果物と終端状態を保持し、Gateway の polling に応答できるようにする。

### 17.3 障害別の表示と動作

| 障害 | UI | 復旧動作 |
| --- | --- | --- |
| Control Plane 再起動 | 再接続中 | Gateway から snapshot/feed を再取得 |
| Gateway 停止 | 最終同期時刻と操作不可 | 復旧後に outbox・lease・Worker 状態を再照合 |
| Agent Controller 停止 | 進行待ち | lease 期限後に新 Controller が再評価 |
| Agent dispatch 応答消失 | 状態確認中 | 同一 dispatch_id で Worker 受理状態を照会 |
| Worker の heartbeat 消失 | Worker 接続不明 | 新規配分から除外。実行中は RECONCILING |
| Worker 再起動 | 実行中断の証拠と再試行候補 | 自動再開を仮定せず、実行終了と成果物を照合 |
| callback 消失 | 一時的に更新遅延 | outbox 再配送 + polling |
| Artifact 欠損 | 成果物が取得できない | 復元または明示した再実行。QA pass を捏造しない |
| 設定の一部反映失敗 | 対象と差分を表示 | 旧 Release 維持、受付停止、再適用または rollback |
| Git/CI 停止 | Draft 保存可、適用待ち | 接続回復後に同じ change_set を継続 |

### 17.4 retry と予算

通信再送、実行失敗の再試行、品質不合格の修正を別のカウンターにする。通信再送の初期 backoff は 1/2/4/8/16/30 秒に jitter を加え、その後は定期照合へ移行する。

既定の Workflow 修正回数は２回、工程の実実行は最大３回とし、より厳しい上位 Policy を優先する。timeout は action ごとに定義する。Project の日次/月次上限、Task の最大コスト/経過時間、Worker の同時実行数を別々に評価する。

料金情報を取得できない runtime では、料金上限を厳密に強制できると表示しない。時間・回数・同時実行数を強制し、使用量は取得可能な範囲を表示する。料金予算を必須とする Project は、必要な計測ができる runtime に限定する。

## 18. 認証・権限・監査

### 18.1 権限モデル

| 操作 | Viewer | Operator | Config Editor | Approver | Grok Bot |
| --- | --- | --- | --- | --- | --- |
| 許可 Project の参照 | 可 | 可 | 可 | 可 | scope 内 |
| Task 作成・指示・開始 | 不可 | 可 | 別途付与 | 別途付与 | scope 内 |
| Task 停止・再試行 | 不可 | 可 | 別途付与 | 別途付与 | Policy 内 |
| 成果物受け入れ | 不可 | 付与可能 | 別途付与 | 可 | 初期は不可 |
| Harness/Skill の Draft 編集 | 不可 | 別途付与 | 可 | 別途付与 | 不可 |
| 通常設定の適用 | 不可 | 別途付与 | 付与可能 | 可 | 不可 |
| 実行権限を広げる変更 | 不可 | 不可 | 変更案作成 | 適用判断 | 不可 |
| Worker drain/再開 | 不可 | 可 | 別途付与 | 別途付与 | 不可 |
| 本番承認 | 不可 | 不可 | 不可 | 独立した本番権限が必要 | 不可 |

初期の管理者一名には必要な権限をまとめて付与できるが、イベントには安定した principal ID を保存する。役割名は資格情報と紐付け、HTTP header や本文の自己申告を信用しない。

### 18.2 現行認証からの拡張

現行 Control Plane の cookie は有効期限の署名であり、個人 ID を含まない。Gateway の共通 Bearer token だけでも人間と Bot を区別できないため、設定変更を公開する前に主体の分離を行う。

初期版では、人間 Operator、Grok、Control Plane サービス、Agent Controller、Config Controller、各 Worker に別 credential と scope を割り当てる。単独管理者は固定された一つの人間 principal として記録する。

人間のログイン成功後、BFF は本人の権限を持つ短命の Gateway session/delegation credential をサーバー側に保持する。発行・交換 endpoint は新設し、Gateway が認証済みの人間 credential と登録済み BFF の両方を検証する。ブラウザには opaque session cookie のみ返す。

本番承認は既存の Human Approval 境界を維持する。必要な追加認証と対象確認を専用経路で行い、通常の BFF サービス credential を本番承認 credential として使わない。

### 18.3 UI の保護

Cookie は Secure/HttpOnly/SameSite を維持し、状態変更には CSRF token と Origin 検証を追加する。session は失効・ログアウト可能とし、TTL と対象 principal を持たせる。

既存 CSP は JavaScript と接続を許可していないため、外部 static module と同一 origin の fetch/SSE を許可する構成へ明示的に更新する。汎用 inline script や `eval` を許可しない。

Markdown、Skill ファイル、ログ、Artifact に含まれる HTML を安全にレンダリングする。設定差分やログに credential を含めず、ログ取得・Artifact 取得にも Project scope を検証する。

### 18.4 監査

状態更新と同時に、主体、対象、操作、変更前後 revision、理由、結果、correlation ID を記録する。UI の「履歴」では人が読める説明を出し、詳細から実行 ID と設定 digest にたどれるようにする。

設定変更は Draft の編集者、適用要求者、承認者、Git commit、CI 結果、適用 consumer の一致を連続して追えるようにする。通常の進捗ログと、変更・承認の監査履歴を区別して保持する。

## 19. UI 実装・操作品質

### 19.1 フロントエンド方針

既存 FastAPI を維持し、`app/views.py` の長い HTML 文字列を templates と static assets に分離する。初期は server-rendered HTML と小さな JavaScript module で実装し、かんばん、editor、SSE、フォームだけを段階的に強化する。

状態の正は Gateway、画面の派生表示はサーバー応答とし、ブラウザに独立した業務 state machine を作らない。画面規模が増えてフロントエンドの分離が必要になっても、Gateway/BFF contract を保って移行できる構成にする。

### 19.2 共通部品

- TaskCard、StageColumn、TaskStatus、AgentBadge、WorkerStatus。
- CommandButton：実行条件、反映待ち、成功・失敗を共通化。
- RevisionBanner：他の操作による更新、再読込、差分確認。
- ArtifactViewer、EvidenceList、DecisionPanel。
- ConfigEditor、FileTree、VersionDiff、ValidationResult、ActivationStatus。
- SyncIndicator、EmptyState、InlineError、ActivityTimeline。

### 19.3 表示ルール

- 通常は日本語の業務表現を使い、内部 enum は詳細に閉じる。
- 成功・失敗・待機は色だけで区別せず、文字とアイコンを併用する。
- キーボードだけでカード操作、タブ移動、editor 保存、ダイアログ操作ができる。
- ドラッグの代替として操作メニューと順位変更ボタンを用意する。
- focus を通知で奪わない。状態更新を読み上げ可能な控えめな通知にする。
- 小さい画面では列を横に圧縮せず一覧へ切り替え、詳細は全幅で開く。
- 未保存変更がある画面から移動する場合だけ保存・破棄を確認する。
- 非同期 command は連打を抑制し、再接続後も同じ command の結果を表示する。
- 取得不能、値なし、未実装、無効、権限不足を別の表示にする。
- 同じ Task を複数人・Bot が操作したときは、入力中の文字を保持して競合を説明する。

### 19.4 一覧性能

初期 page size は一覧 50 件、かんばん各列 30 件とし、続きは cursor で読む。列見出しの総数と表示中件数を分ける。完了カードは既定で最近７日を表示し、それ以前は検索できる。

フィルター・検索・選択 Task は URL に反映する。初期 sort は待機列で priority/rank、進行列で要対応/開始時刻、完了列で終了時刻とする。順位変更は optimistic concurrency を使い、同順位競合をサーバーで解決する。

## 20. 配置・運用・観測

### 20.1 初期配置

| 実行単位 | 配置方針 |
| --- | --- |
| Control Plane | 現行 Kubernetes、単一 replica、SQLite/PVC を維持 |
| Gateway API | 現行 VM と PostgreSQL を維持 |
| Gateway scheduler | 同じ repository の別プロセス。DB queue を処理 |
| Agent API | 現行 Kubernetes のサービスを拡張 |
| Agent Workflow Controller | Agent image の別プロセス/Deployment。内部 API を利用 |
| Config Controller | Gateway 管理下の別プロセス。限定した Git/CI 権限 |
| Worker | 現行 Pod/PVC。durable runner と callback outbox を追加 |
| Writer Worker | Phase 3B で専用 Deployment/Service/PVC として追加。制作と内容レビューを実行 |
| Marketing Worker | Phase 3B で専用 Deployment/Service/PVC として追加。企画・制作指示・Growth 分析を実行 |

全体を同時に高可用化することは初期要件にしない。単一プロセス停止で状態を失わず再開できることを優先する。

### 20.2 ネットワーク変更

- Control Plane → Gateway の既存通信を維持し、ブラウザから Worker/Agent の内部 API に直接接続しない。
- Agent Workflow Controller → Gateway 内部 API の認証付き通信を追加する。
- Config Controller → 管理対象 Git/CI と、適用状態を取得する管理経路を限定して許可する。
- Worker → Gateway の callback/heartbeat を許可する。Gateway からの状態照会経路も契約化する。
- Argo CD の反映状態は既存の管理用経路または権限を限定した observer から取得する。UI に cluster-admin 資格情報を持たせない。
- 現行 Cilium/Tailscale policy の差分は homelab 側で管理し、構成変更の検証対象にする。

### 20.3 観測項目

| 指標 | 用途 |
| --- | --- |
| Task/Step の状態別件数・滞留時間 | どの工程で止まっているか |
| command 受付から状態反映までの時間 | UI 操作が反映されているか |
| Gateway event と UI 表示の遅延 | 同期の健全性 |
| dispatch retry、結果不明の件数 | 配送問題と二重実行リスク |
| Worker 枠使用率、queue 時間、heartbeat age | 実行能力の不足 |
| QA pass/fail/inconclusive、修正回数 | 品質と手戻り |
| Config 適用時間、失敗、desired/observed 差 | 設定の反映状態 |
| 使用量・取得可能な費用 | 予算の確認。欠損率も併記 |

すべての Job と command に correlation ID を付ける。Task → Run → Step → Attempt → Job → Worker Execution の対応をログ・画面から検索できるようにする。

### 20.4 初期の運用目標

以下は設計目標であり、現時点の計測結果ではない。

- 1,000 Task、かんばん表示 200 枚程度の検証データで、一覧初期表示 p95 が２秒以内。
- 通常時、Gateway の確定から UI 反映まで５秒以内。
- 重い処理を伴う command も、受付応答は p95 ２秒以内。適用完了は別表示。
- heartbeat 10 秒間隔、30 秒超で degraded、60 秒超で offline 候補。初期値は実機試験で調整する。
- Gateway/Agent 再起動後は60秒以内に進行可能な Run を再評価する。実行結果不明の解決時間とは区別する。
- 同一 idempotency key の再送で、追加の Job・設定変更を作らない。

### 20.5 保持・バックアップ

既定案は通常ログ30日、イベント feed 90日、重要な設定・承認・変更監査１年とする。参照中の PRD、コード snapshot、QA 証拠、Release は期間だけで削除しない。

Artifact GC は active Run、Decision、release candidate、保持指定からの参照を確認する。削除時は metadata に tombstone を残し、UI は「期限切れ」と表示する。

Gateway DB は既存 Gateway VM のバックアップ運用に整合させ、復元試験で追加テーブルも確認する。Worker PVC の Artifact と設定 Release artifact は別の復元対象として列挙する。Control Plane の読み取りモデルだけ復元して業務状態を復元したことにしない。

初期の復元目標は既存バックアップ間隔に合わせ、実測後に RPO/RTO を設定する。未確認のバックアップで無損失復旧を約束しない。

## 21. リポジトリ別の変更計画

### 21.1 ai-business-control-plane

- `app/main.py`：認証、BFF routes、CSRF、SSE、共通エラー。
- `app/gateway.py`：Task/Command/Catalog/Config/Artifact/Event client。
- `app/store.py`：複数 Task/Run の読み取りモデルと cursor。
- `app/service.py`：同期・操作中継。独自の業務状態更新は削減。
- `app/views.py`：templates と static assets への分離。
- 新規 UI：かんばん、Task detail、Agent/Skill/Harness editor、Worker、設定変更、判断待ち。
- `docs/architecture.md`：Gateway を業務状態の正にする本設計の責務へ更新。

### 21.2 ai-business-agent

- `app/registry.py`：Release version、capability 詳細、互換性、routing の解決。
- `app/main.py`：dispatch 再照合、固定設定の要求、カタログ・適用報告。
- `app/workflow_controller.py`（新規）：Run lease、固定 Workflow の評価。
- `app/context_builder.py`（新規）：入力 snapshot、Agent Skill/Harness、Artifact 参照の組み立て。
- `profiles/`：既存役割を安定 ID と revision に分離。
- `skills/`、`harnesses/`、`workflows/`：UI 編集の正規ソース。
- Writer、Content Reviewer、Marketing の profile/capability/schema を追加し、既存 Growth と Article の互換性を維持。§27 の Workflow と pool binding を登録。

初期の Agent Skill consumer は `context_builder` とする。`kind=context_template` の Skill を役割の managed instructions と handoff に展開し、実作業は Worker に委譲する。UI には「Agent が選択・組み立て、Worker 上で使用」と表示する。これにより Agent 側の編集も実際の次回入力へ反映される。

固定 Controller の遷移条件は型付き Workflow とコードに置く。Markdown を変えて任意の状態遷移を実行する方式にはしない。Agent 自身が LLM で計画を作る consumer は、後続の Planner 導入時に別 execution target として追加する。

### 21.3 ai-business-worker

- `app/main.py`：永続 runner、枠管理、callback outbox、正確な cancel、heartbeat。
- `app/build_executor.py`：入力 Artifact からの開始、固定設定、実行制約の検証。
- `app/agent_executor.py`：QA の Artifact 指定と完全な出力 schema 検証。
- 新規 Artifact module：manifest、finalize、digest 検証、認証付き取得。
- `skills/`：既存 Skill の版管理と追加。全 Skill を無条件で公開せず、Job の許可集合だけを利用可能にする。
- `harnesses/`：共通 AGENTS.md、executor 制御設定の正規ソース。
- deploy/CI：生成 ConfigMap、image digest、drain、Release metadata。
- writing/marketing executor と専用 pool の配置、入力 Artifact の取得、原稿・企画・レビューの出力検証を追加。共通 runner を再利用し、業務ごとの repository は増やさない。

Skill の許可制御はプロンプト内の一覧だけで完了させない。使用する Codex runtime の探索先と設定を検証し、Job ごとの Skill 配置/可視性を制御する。repository 内に追加された未承認 Skill が管理ルールを代替しないことも確認する。

### 21.4 homelab / Gateway

- `gateway/app/main.py` の単一モジュールから Task/Command/Config/Event/Job modules を段階分離。
- DB migration、Task/Run/Attempt と immutable evidence、共通カタログ。
- durable scheduler、内部 Workflow API、event feed/snapshot、Artifact proxy。
- actor/scope 別の認証、操作認可、Config Controller。
- MCP の Task tools と既存 Job tool の互換処理。
- Kubernetes/Tailscale/Cilium、CI、Secret、監視の必要差分。
- 古い runtime/basic-design 記述を実装完了に合わせて更新。

## 22. 段階導入

### Phase 0：契約と実機の確認

実際の image digest、endpoint、認証経路、enabled capability、Worker Artifact の所在を取得する。ローカル未コミット変更を棚卸しし、実装の基準 commit を確定する。

Task/Job/Artifact/Config Release の schema と API contract を先に固定する。代表シナリオの fixture を作る。この段階の完了条件は、現状との差分と利用可能な action が再現可能な形で記録されていること。

### Phase 1：統一タスク台帳とかんばん参照

- Gateway の Task/Run/Step/Attempt と Project 集約を追加。
- UI/MCP/既存 submit_job の新規 Job を Task に関連付ける。
- event feed、snapshot、Control Plane read model を実装。
- かんばん・詳細・Artifact metadata を参照可能にする。

完了条件：Grok の新規依頼が手動 import なしで表示され、複数タスク・全 Attempt を追える。

### Phase 2：操作と定型 Workflow

- 主体の分離と command/revision/idempotency を実装。
- durable dispatch、Agent Workflow Controller、Worker outbox/cancel を実装。
- mvp-build と single-action の進行、QA、入力回答、受け入れを実装。
- かんばんの操作、追加指示、再試行、担当変更を実装。

完了条件：開始 → 実装 → QA → 差し戻し → 受け入れまで、再起動を挟んでも二重実行せず追跡できる。

### Phase 3A：Agent / Worker 設定の編集・適用

- Agent、Skill、Harness、Workflow、Worker の管理 UI。
- Draft、差分、検証、Config Controller、Git/CI 連携。
- Agent context builder と Worker Skill/Harness の consumer。
- Release manifest、drain-and-switch、observed digest、rollback。

完了条件：UI から Agent と Worker の両方の Harness/Skill を更新し、検証して反映し、次の Task で新しい版が使われたことを確認できる。旧版への rollback も UI から完了できる。

### Phase 3B：Writer / Marketing Worker の追加

- pool ごとの action allowlist、Deployment/Service/PVC、credential、実行枠を追加。
- Marketing → Writer の Artifact 移送と provenance 検証。
- 原稿制作、企画、制作指示、内容レビューの profile/Skill/Harness/schema。
- コンテンツ制作・マーケティング Workflow とかんばんの制作・内容確認列。
- §27 の登録 fixture と AT27–AT34 を検証して capability を有効化。

完了条件：Grok/UI からの依頼が Marketing/Writer に割り振られ、pool をまたいで企画・原稿・レビュー結果を引き継げる。専用設定の編集・反映・rollback と実行中 Task の旧版維持を確認できる。

### Phase 4：複数 Worker・運用強化

同一 pool の複数 replica、共有 Artifact store、優先順位制御、料金情報、詳細ログ、通知、複数版の共存を追加する。必要性を確認して動的 Planner を導入する。

Phase 1 は最初に触れる版であり、ユーザーが求める全機能の完成ではない。Writer/Marketing の追加を含む Phase 3B までを今回の機能範囲の完成条件とする。

## 23. 既存データと互換性の移行

### 23.1 DB と既存クライアント

追加テーブルと nullable 外部キーを先に導入し、既存 API 応答の既存フィールドを維持する。新しい client には task_id を追加して返す。schema migration は一度だけ実行する管理処理に移し、大きな backfill を API 起動ごとに行わない。

切り替え前の旧 Job は原則として一件ずつ `legacy/single-action` Task に取り込む。名前や時間の近さから工程の依存関係を推測して結合しない。既に確かな build/QA の参照がある場合のみ provenance として関連付ける。

Project の `current_job_id` は旧画面互換用とし、新かんばんは使用しない。稼働中の旧 Job には現在判明している入力と設定を記録し、不明な version は `unknown_legacy` と表示する。

### 23.2 二重の進行処理を防ぐ

Run ごとに `orchestration_mode=legacy/manual/workflow-v1` を固定する。新 Controller が旧経路の Job を自動で次工程に進めない。

Gateway の callback にある Project 状態の直接更新は、legacy Job の互換処理と Task projection に分離する。workflow-v1 では Task/Run の状態確定後に Project 集約を更新する。

既存の production approval と release candidate は維持する。別 Task の成果物を自動で現在の release candidate に昇格しない。candidate 更新は明示的な操作とし、その対象の変更・QA 更新だけで対応する approval を失効させる。

### 23.3 切り戻し

新 UI を止めても Gateway の Task/Job は保持する。新規 Workflow 起動を feature flag で止め、進行中は drain する。DB は追加カラムを残して旧 read path へ戻せる migration とし、業務データを削除する rollback を既定にしない。

Config Release の切り戻しと UI/Controller アプリの切り戻しを別操作にする。どの組み合わせが互換か Release manifest で検証する。

## 24. 受け入れ試験

### 24.1 シナリオ別

| ID | シナリオ | 合格条件 |
| --- | --- | --- |
| AT01 | Grok から Task 作成 | UI に一枚のカード。source と目的が一致 |
| AT02 | 同じ Project に複数 Task | 工程、成果物、状態を互いに上書きしない |
| AT03 | Product → Developer → QA | 各担当・入力・設定・出力の連鎖を確認できる |
| AT04 | QA fail | Job 成功と品質不合格を分け、同じ成果物に対する修正へ戻る |
| AT05 | QA inconclusive | pass 扱いせず、必要な判断や追加情報を提示 |
| AT06 | 成果物変更後の受け入れ | 古い digest を対象にした Decision を拒否 |
| AT07 | UI と Grok が同時に更新 | revision により一方が競合し、入力を失わず再確認できる |
| AT08 | command 応答消失・再送 | 同じ command と Job が返り、追加実行なし |
| AT09 | pause | 実行中を継続し、後続工程の起動を停止 |
| AT10 | cancel と正常終了の競合 | 実際の終端を保持し、停止結果を正確に表示 |
| AT11 | Worker 応答消失 | 別 Worker に二重配送せず結果照合へ移る |
| AT12 | Gateway/Agent/Worker 再起動 | 永続状態から復旧し、失われた進行と二重 Job がない |
| AT13 | callback 消失・順不同・重複 | outbox/poll で最終状態を復元し、古い状態へ戻らない |
| AT14 | UI から Agent Skill/Harness 更新 | 検証・適用後、context builder と新 Task に新 digest が反映 |
| AT15 | UI から Worker Skill/Harness 更新 | image/bundle 反映後、新 Attempt が新 digest を使用 |
| AT16 | 設定更新中の既存 Run | 旧設定を使用し続けるか、明示された移行待ちになる |
| AT17 | 複数 repository の片方だけ反映 | active Release を切り替えず、不整合の実行を拒否 |
| AT18 | 設定 rollback | 過去 Release の実際の適用報告と新実行を確認 |
| AT19 | 未承認 Skill・権限変更 | UI/API/実行側で禁止され、Bot credential で迂回できない |
| AT20 | Artifact の別 Project 参照 | ID を知っていても取得・QA 入力に利用できない |
| AT21 | Control Plane cache 消失 | snapshot/feed からタスク・設定表示を再構築 |
| AT22 | イベント transaction の競合 | cursor 取得で commit 遅延イベントを取りこぼさない |
| AT23 | 不正な Markdown/ログ/preview | 管理 UI の script 実行や credential 取得につながらない |
| AT24 | キーボード・小画面 | ドラッグなしで主要操作と設定編集を完了できる |
| AT25 | 使用量の取得欠損 | コストをゼロと表示せず、不明・推定を区別 |
| AT26 | 枠上限・受付停止 | Gateway と Worker の双方で新規実行数を制御 |
| AT27 | Marketing から Writer への引き継ぎ | 別 pool 上で同じ brief digest を参照し、原稿の出所を追跡できる |
| AT28 | 記事・LP・メール・SNS 原稿の依頼 | 対応形式を検証し、Writer に配分。送信・公開は行わない |
| AT29 | 根拠不足の原稿 | 断定の捏造をせず、未解決項目または入力待ちを返す |
| AT30 | Writer 原稿のレビューと修正 | Reviewer が同じ Artifact を検査し、新版原稿に旧レビューを流用しない |
| AT31 | Marketing と Growth の区別 | 新規施策の企画と実測データの評価が別 action/担当に配分される |
| AT32 | 未稼働 Research を必要とする依頼 | 検索したと主張せず、資料提供待ちまたは利用不可を示す |
| AT33 | 専用 Worker の action 制限 | Writer/Marketing credential・pool で未許可 executor を起動できない |
| AT34 | pool をまたぐ Release 更新 | Workflow の依存先が揃うまで切り替えず、Skill 更新を再現できる |

### 24.2 テストの層

- Domain unit test：状態遷移、不変条件、ルーティング、Workflow 分岐、設定互換性。
- DB integration test：lease/fencing、一意制約、revision、outbox、commit 順 cursor。
- Contract test：Gateway/Agent/Worker の schema、cancel、Artifact、Config Release。
- UI test：かんばん操作、競合、editor 差分、反映状態、認可、CSRF。
- E2E：模擬 executor による失敗・再試行と、実際の Codex runtime を使う少数の管理された smoke test。
- Recovery test：各コンポーネントの停止、応答消失、バックアップからの復元。

LLM 出力そのものの完全一致を一般の CI 合格条件にしない。構造・対象 digest・権限・手順の証拠を検証し、内容品質の regression fixture は別に評価する。

## 25. 要件との対応と既定値

### 25.1 ユーザー要件との対応

| 要件 | 対応する設計 | 完成を確認する試験 |
| --- | --- | --- |
| Agent の設定・役割を参照更新 | §8、§13、§21.2 | AT14、AT16、AT18 |
| Worker の設定・役割を参照更新 | §9、§13、§21.3 | AT15、AT17、AT26 |
| Agent と Worker の Harness/Skill 本文・付属ファイルを更新 | §8.2–8.3、§13.1–13.7 | AT14–AT19 |
| タスク状況をかんばんで参照更新 | §7、§10、§15 | AT01–AT10、AT24 |
| Grok からの指示を適切な担当へ配分 | §11、§15.4 | AT01、AT03、AT19 |
| 役割と責任を明確にする | §5、§8、§11.6 | AT03–AT06 |
| UI の操作を実処理と一致させる | §7.4、§10.4、§16–17 | AT07–AT13 |
| Writer/Marketing Worker を増やす | §27、Phase 3B | AT27–AT34 |

### 25.2 実装を進めるための既定値

- UI は日本語、初期は管理者一名、Tailnet 内で利用する。
- Gateway PostgreSQL を業務状態の正とする。
- 初期の Agent/Worker 実行は既存 Codex runtime を維持する。
- 最初の成果物受け入れは人間が行う。本番承認とは分離する。
- 設定は Git Release、UI は Draft と適用操作を提供する。
- 適用は drain-and-switch、進行中 Run の無言の設定更新は行わない。
- 初期 Workflow は逐次実行。QA 修正２回、各工程の実行３回を上限の初期値とする。
- Writer/Marketing pool の追加前に、現行 Worker での状態と Artifact の契約を完成させる。各専用 pool は初期１ replica、１実行枠で開始する。

### 25.3 実装着手時に確認する外部条件

設計を止める質問ではなく、Phase 0 の確認項目とする。

1. 実機が使用する Gateway/Agent/Worker/Control Plane の revision と構成。
2. Git 変更を作成する専用 App の導入可否、対象 repository と merge Policy。
3. 同一 Worker の実行枠を何件まで許可するか。CPU、メモリ、Codex 認証の共有範囲を含む。
4. バックアップから Artifact と設定 Release を復元できるか。
5. 稼働中 Codex runtime の Skill 探索、Harness 読み込み、モデル指定、終了イベントの仕様。
6. 現行の本番承認経路を UI に接続する際の、人間 credential の検証方式。

## 26. 参照した実装と文書

### 26.1 ローカル実装

以下は本設計の現状判断に使用したソース。新設案の仕様を裏付ける実装ではない。

| Repository | 参照箇所 | 確認した事項 |
| --- | --- | --- |
| homelab | `docs/ai-business-platform/agent-runtime.md` | Gateway/Agent/Worker の境界、既存 Skill/Harness 配置 |
| homelab | `docs/ai-business-platform/basic-design.md` | repository 境界、認可・Job・承認の所有 |
| homelab | `ai-business-platform/gateway/app/main.py` | MCP、Job/Project、callback、承認、一般 dispatch |
| homelab | `ai-business-platform/gateway/app/video.py` | 一般 Job と独立した動画 runner の存在 |
| ai-business-control-plane | `README.md`、`docs/architecture.md` | 現行の責務、SQLite、今後の ownership 方針 |
| ai-business-control-plane | `app/main.py`、`app/views.py` | 現行 UI、session、CSP、routes |
| ai-business-control-plane | `app/store.py`、`app/service.py`、`app/gateway.py` | current Job の追跡と同期 |
| ai-business-agent | `profiles/capabilities.yaml`、各 profile | action と役割、Skill の対応 |
| ai-business-agent | `app/registry.py`、`app/main.py` | schema 検証、managed context、dispatch |
| ai-business-agent | `schemas/`、`deploy/kubernetes/` | 入力 contract、image に含む設定 |
| ai-business-worker | `app/main.py` | Job、cancel、callback、再起動時処理 |
| ai-business-worker | `app/build_executor.py`、`app/agent_executor.py` | Codex、QA workspace、成果物検証 |
| ai-business-worker | `skills/`、`deploy/kubernetes/codex-harness.yaml` | 現行スキルと共通指示 |

### 26.2 外部仕様の参照

- [公式 Skills ドキュメント](https://learn.chatgpt.com/docs/build-skills)：`SKILL.md`、付属ファイル、Codex の `/etc/codex/skills` 等の読み込み方式。
- [公式 Codex SDK ドキュメント](https://learn.chatgpt.com/docs/codex-sdk)：将来の実行制御・会話再開の選択肢。SDK への移行は本設計の必須条件にしていない。

Config Release、Task、Workflow、command、上記 API とファイル配置の追加案は、この基盤向けの設計であり、Codex の標準機能として提供されるものではない。

## 27. Writer / Marketing Worker の追加設計

### 27.1 追加後の編成

Writer と Marketing は、役割だけでなく個別に設定・受付枠・実行環境を管理できる専用 Worker pool として追加する。実装は `ai-business-worker` の共通 API/runner を再利用する。物理サーバーを役割ごとに増やすことは必須にしない。

| Agent の役割 | 主な責任 | 配分先 pool |
| --- | --- | --- |
| Product Manager | 顧客課題、仕様、受け入れ条件 | 既存 Worker |
| Software Engineer | 実装と修正 | 既存 Worker |
| QA Engineer | ソフトウェアの検証 | 既存 Worker |
| Writer | 記事、LP、メール、SNS 等の原稿作成・改稿 | `writer` |
| Content Reviewer | 原稿の根拠、表現、ブランド、形式の検査 | `writer`。別 Attempt・別 profile で実行 |
| Marketing Strategist | 対象顧客、訴求、チャネル、施策、制作 brief、計測計画 | `marketing` |
| Growth Strategist | 観測済み指標から改善案と次の計測を提案 | `marketing` |
| Researcher | 許可された外部資料の調査と出典整理 | 専用能力の有効化後に配分。現状は無効 |

Marketing は施策の設計、Writer は表現の制作、Growth は実績の評価を担当する。Writer にキャンペーン目的を一から推測させず、Marketing の brief を入力にする。既存の Growth は維持し、Marketing に改名して履歴を混ぜない。

Content Reviewer は Writer と別の入力 context で実行し、制作物を変更せず指摘を返す。同じ pool やモデルを利用する場合もあり、別 Attempt であることだけで独立したモデルによる保証とは表示しない。最終受け入れは人間の Decision とする。

### 27.2 action と profile

| action | 担当 profile 案 | executor | 主要な出力 |
| --- | --- | --- | --- |
| `content.draft` | `content-writer-v1` | writing | 原稿、構成、出典対応、未解決事項 |
| `content.revise` | `content-writer-v1` | writing | 修正版、修正対応表、新 Artifact digest |
| `content.review` | `content-reviewer-v1` | writing-review | verdict、指摘、根拠、対象 digest |
| `marketing.plan` | `marketing-strategist-v1` | marketing | 対象、訴求、チャネル案、施策、計測計画 |
| `marketing.brief` | `marketing-strategist-v1` | marketing | Writer へ渡す制作指示 |
| `growth.plan` | 既存 `growth-strategist-v1` | growth | 指標の観察、仮説、優先改善案 |

`content.draft` は Gateway の既存 action 候補に名前があるが、Agent/Worker の対応を新設して初めて公開する。既存の無効な `content.article` は互換 adapter を追加し、`content.draft` の `format=article` に変換する。元 action と解決後 action の双方を記録し、正規化した入力で request hash を計算する。

旧 Article の `topic/audience/source_artifacts` 入力は互換 schema で受け、追加で必要な内容は Task の入力待ちにする。source_artifacts の値は Project の認可済み Artifact に解決し、任意の URL やファイルパスを実行時に取得する入口にはしない。既存 API に新規必須フィールドを無条件で追加しない。新規 Task は正規 action を利用する。

`growth.plan` は入出力 contract を維持し、新 Release の pool binding を `marketing` に変更する。旧 Release の実行中 Run は既存 Worker を使い続け、履歴の担当・配分先を書き換えない。既存 Worker 上の Growth executor を廃止するのは、参照する旧 Run がなくなった後とする。

既存 `technical-writer-v1` は技術文書用 profile として保持し、広い制作形式に対応する `content-writer-v1` を新設する。専門的な技術文書では、対応 capability を満たす既存 profile を明示選択できる。

### 27.3 入出力 contract

制作入力の基本項目は以下とする。

| 項目 | 必須条件・意味 |
| --- | --- |
| `format` | article / landing_page / email / social_post。制作形式 |
| `language` | 原稿の言語。初期既定は ja |
| `audience` | 想定読者・顧客 |
| `objective` | 読後に伝えたいこと・期待する行動 |
| `brief_artifact_id` | Marketing brief を使う場合。参照版を固定 |
| `brand_artifact_id` | tone、用語、表記、主張可能な事項の版 |
| `source_artifact_ids` | 与えられた事実や引用の根拠 |
| `constraints` | 長さ、含める内容、CTA、媒体ごとの形式制約 |

媒体に依存する制限は、取得日時を持つ入力資料または版付き constraint から読む。数値を無期限に固定した知識として扱わない。必要な制限が不明なら入力を求める。

`content.revise` は上記に `source_draft_artifact_id` と review/変更指示の参照を追加する。入力原稿を digest で特定し、その原稿から修正する。

成果物は Markdown 本文と構造化 JSON/manifest の組にする。JSON は `format / language / title / body_artifact_id / claims / source_refs / unresolved_items / brief_digest / brand_digest` を含む。主張と出典は相互に参照可能にし、引用・推論・提案を区別する。

内容レビューは `pass / fail / inconclusive`、`target_digest`、入力 revision、確認項目と evidence、修正指示を返す。本文の差し替えや旧レビュー結果の上書きは行わない。必須の根拠が不足する場合は inconclusive と入力要求にする。

Marketing plan は `audience / positioning / messages / channel_options / initiatives / measurement_plan / assumptions / evidence_refs` を持つ。提案する予算は計画上の見積りであり、支出権限や実支出の記録とは区別する。

Marketing brief は `objective / audience / format / key_message / required_claims / source_refs / brand_ref / cta / constraints / acceptance_criteria` を持つ。Writer が別の目的・ブランドへ逸脱していないかレビューできる形にする。

### 27.4 Skill と Harness

| 役割 | 初期 Skill ID 案 | 主な手順 |
| --- | --- | --- |
| Writer | `source-grounded-writing` | 指定資料を読み、根拠に沿って原稿を作る |
| Writer | `brand-voice-writing` | ブランドの用語・文体・訴求を反映する |
| Writer | `content-format-adaptation` | 同じメッセージを記事・LP・メール・SNS に整える |
| Writer | `revision-with-feedback` | 指摘と変更内容を対応付けて改稿する |
| Content Reviewer | `editorial-evidence-review` | 事実・根拠・文体・形式を確認する |
| Marketing | `audience-positioning` | 対象、課題、価値、訴求を整理する |
| Marketing | `campaign-planning` | チャネル候補、施策、期待効果、前提を整理する |
| Marketing | `content-briefing` | 制作に必要な情報と完了条件を Writer に渡す |
| Marketing/Growth | `measurement-planning` | 指標、観測方法、期間、比較条件を定める |
| Growth | `analytics-evidence-review` | 実測と仮説を区別して改善案を作る |

Agent 側の context Skill は `writer-handoff` と `marketing-handoff` を追加し、上記の入力・根拠・制約を managed context に組み立てる。実作業の Skill 本文と付属ファイルは Worker repository に保存する。

Worker Harness は `writer-executor-v1`、`marketing-executor-v1` として分ける。共通 Harness を参照し、許可 action、入力資料、生成ファイル、出力 schema、timeout と実行枠を定義する。UI では継承後の実効設定と変更元を表示する。

Writer は渡された資料の範囲で原稿を作り、不足する事実・体験談・実績を創作して埋めない。創作が目的の依頼では創作部分を識別する。Marketing は企画と制作指示を成果物にし、広告購入、メール送信、SNS 投稿、CMS 公開は初期 executor に含めない。将来それらを追加する際は別 action と対象付き承認を設計する。

### 27.5 実行環境の分離

専用 Deployment 名は `ai-business-writer-worker`、`ai-business-marketing-worker` とし、同じ `ai-business-worker` repository の pool 別 manifest から配置する。

- 初期は各１ replica、同時実行１件。resource requests/limits は実機測定で決める。
- Service、PVC、Worker credential、callback identity、Skill/Harness bundle を pool ごとに分ける。
- 同じ runner コードを使用できるが、登録済み executor と API action を pool ごとに制限する。未登録 action は実行前に拒否する。
- Job ごとの workspace、許可 Skill のみの配置、入力 Artifact、設定 snapshot を生成する。
- Writer/Marketing pool に開発用 repository、Kubernetes token、公開用 credential を配布しない。
- egress は必要なモデル接続、Gateway、明示された連携先に限定する。外部調査能力は独立した capability として有効化する。
- runtime ごとの model 認証を専用 Secret から投入し、モデルによる Artifact/ログへの書き出し対象にしない。

Marketing が生成した brief を Writer へ渡すときは、Gateway が認可済み Artifact ID と digest を handoff に含める。Worker の入力取得処理が一時的・対象限定の権限で Gateway proxy からダウンロードし、digest 確認後に read-only 入力として配置する。取得用 credential を LLM の prompt に含めない。

同一 pool の Reviewer に渡す場合も Artifact contract を通す。Project をまたいだ入力、未確定のファイル、更新中の workspace を直接参照しない。

### 27.6 Workflow とかんばん

`content-production-v1`：

```text
受付 → 入力・資料確認 → Writer の制作 → 内容レビュー → 人間レビュー → 完了
                                     ↑         │
                                     └─ 改稿 ──┘
```

`marketing-content-v1`：

```text
受付 → Marketing の企画 → 制作 brief → Writer の制作 → 内容レビュー → 人間レビュー
```

内容レビューの pass は人間レビューへ、fail は同じ原稿を入力にした改稿へ、inconclusive は資料・条件の入力待ちへ進める。修正回数と実行回数は §17.4 の上限を使用する。

かんばんは「受付 / 企画 / 制作 / 内容確認 / レビュー待ち / 完了」をこの Workflow の既定表示とする。開発系タスクも含む全体表示では stage_key ごとに列を集約し、業務種別で絞り込めるようにする。

Task 詳細には形式、対象読者、ブランド版、brief、原稿版、出典、レビュー判定、修正対応を表示する。Worker 詳細には Writer/Marketing の別、受付枠、利用 Agent、実効 Skill/Harness と版を表示する。

Marketing の企画のみ、または Writer の原稿のみを依頼する場合は不要な工程を起動しない。直接指定された action は single-action Workflow で扱えるが、品質確認付きの納品には対応する制作 Workflow を選ぶ。Grok の tool 説明にも、単発生成とレビュー付き制作の違いを明記する。

Growth は外部公開と計測後に新しい関連 Task として起動する。原稿完成を「施策が実施された」「成果が出た」と扱わず、観測対象・期間のある analytics snapshot を必要入力にする。

### 27.7 Research とデータ不足

初期の Writer/Marketing は、提供された Artifact、Project context、ブランド資料、analytics snapshot を使用する。現在無効な `browser.research` を当然に利用できるとは扱わない。

市場調査、競合の最新情報、検索需要などの取得が必要な場合は、認可された Research capability が稼働していれば別 Step として依頼する。利用できなければ、必要資料と理由を入力要求として返す。推測を取得済みデータとして報告しない。

外部から取得した情報には出所と取得日時を付け、原稿本文・企画・推論の provenance を区別する。ブランド資料や根拠資料が更新されても進行中 Attempt への入力は固定し、再評価が必要な Task を UI で示す。

### 27.8 設定変更と有効化条件

Writer/Marketing の profile、Skill、Harness、schema、Workflow、pool manifest も §13 と同じ UI 編集・Git Release・適用の経路を使う。専用の別設定 DB は作らない。

跨る pool を持つ Workflow は、参照する全 consumer を activation の依存集合に含める。Writer の Skill 更新が Marketing brief の schema と互換でなければ同時 Release を作る。実行中 Run の必要 consumer を残し、新規 Run の受付停止と既存 Run の完了待ちを分ける。

次の条件をすべて満たした時点で Gateway/MCP の capability を公開する。

1. Agent profile、Skill、Harness、入出力 schema、Workflow が検証済み。
2. 専用 pool が起動し、想定 action、設定 digest、readiness を報告している。
3. Gateway → Agent → 専用 Worker → callback の実行経路が動作する。
4. Marketing → Writer → Reviewer の Artifact 引き継ぎと digest 照合が動作する。
5. AT27–AT34 が合格し、旧 Release への rollback 手順を確認している。

この章は追加実装の設計である。既存の `content.article.enabled` の変更や、Writer/Marketing Worker の配置は、この設計書更新では実施していない。
