# Task 台帳と Workflow 進行

`docs/ai-business-platform/control-plane-agent-workflow-design.md`（homelab）の
Phase 1「統一タスク台帳とかんばん参照」と Phase 2「操作と定型 Workflow」の実装範囲、
そしてまだ保証していないことを記録する。

## 実装したもの

| 対象 | 内容 |
| --- | --- |
| Task 台帳 | `tasks / task_input_revisions / workflow_runs / workflow_steps / step_attempts / artifacts / task_messages / input_requests / decisions / commands` |
| イベント | `platform_events` と `event_counter`。更新と同一トランザクションで cursor を採番する |
| Workflow 定義 | `app/workflows.py` の型付き定義。`single-action-v1`（Gateway 駆動）と `mvp-build-v1`（Controller 駆動）を開始できる |
| 進行 | `app/runs.py` が「次に許される遷移」を定義と Run の状態から決め、範囲外の提案を拒否する |
| 内部 API | `internal` surface の `/internal/v1/runs/lease`、`/runs/{id}/proposals`、`/runs/{id}/heartbeat`、`/artifacts/{id}`（Controller 専用 credential） |
| 操作 | `start / pause / resume / cancel / retry / request_changes / accept_deliverable`、入力要求への回答、追加指示 |
| 配送 | `job_dispatches` と `worker_commands` を `app/scheduler.py` が処理する（thread と `python -m app.scheduler` の両方） |
| Worker 在庫 | `workers` と `worker_overrides`。`GET /v1/workers`、`POST /v1/workers/{id}/commands`（drain/resume） |
| REST | 上記に加えて `/v1/tasks`、`/v1/tasks/{id}/runs`、`/v1/commands/{id}`、`/v1/artifacts/{id}(/content)`、`/v1/catalog`、`/v1/events`、`/v1/snapshot` |
| MCP | `list_capabilities / list_workflows / create_task / get_task / list_tasks / request_task_action / add_task_instruction` |
| 既存互換 | `submit_job` と `POST /v1/jobs` の Job は `orchestration_mode=legacy` の single-action Task に同一トランザクションで結び付け、`task_id` を返す |

## 意図的に分けている状態

- Job の実行結果（`job_state`）と品質判定（`quality_verdict`）は別に保持する。
  判定の規則は後述の「判断と権限」にまとめる。満たさない場合は
  `reported_verdict` と `downgraded_reason` を残して `inconclusive` にする。
- legacy の Project 状態（release candidate の前提）は従来通り report の verdict を
  使うが、`acceptance_criteria` を伴わない pass では `QA_PASSED` にしない。
  台帳の厳しい判定との差は `reported_verdict` として両方記録する。
- `completed` callback が action の必要出力を含まない場合は、実行成功
  （`job_state=SUCCEEDED`）を記録したまま Attempt を
  `output_contract_unsatisfied` で失敗させる。完了にはしない。必要出力は実際の
  executor の返り値に合わせて定義する（`code.build` は `report` で包まれない、
  `operation=self_test` は capability probe が出力）。
- dispatch の結果は「何を証明したか」で分ける。Gateway 自身の拒否と、最初の送信に
  対する Worker の 4xx 応答は definitive として Attempt を失敗させる（再送の拒否や、
  既に受け付けられた実行についての拒否は definitive ではない。下の「配送の拒否が
  『確定』になるのは最初の送信のときだけ」を参照）。応答消失、5xx、429、
  読めない応答、想定外の例外は結果不明として Job を `RECONCILING`（非終端）に
  残し、Task を `BLOCKED` にする。後から本物の callback が届けば受理でき、
  進捗イベントが届いた時点で Task は `ACTIVE` に戻る。release candidate の
  登録は、終端でない build/QA が残っている限り拒否する。
- Worker が cancelled を報告した実行は Attempt を `CANCELLED`、interrupted は
  `interrupted_before_completion` として記録し、失敗と区別する。
- 非終端の progress のうち「解決していない」ことを述べる 2 種類（`stopped: false` =
  停止を確認できない、`outcome_pending: true` = 実行は終わったが記録した結果を読めない）
  は進捗として扱わない。Attempt は開いたまま、Task は `BLOCKED` にして理由を残す
  （どちらも result_summary にそのまま載る）。
- dispatch_id は送信前に Job に記録する。callback は記録済みの dispatch と
  worker 実行に一致しない限り受理せず、`job.callback_quarantined` として
  監査に残す。
- `orchestration_mode=legacy` の Job だけが `projects` の単一の build/QA/PRD 欄と
  release candidate を更新する。Workflow Task は自分の Run と Artifact に記録する。

## まだ保証していないこと

- Project 単位のアクセス制御は未実装である。人間の操作・Grok・Control Plane は
  同じ `GATEWAY_API_TOKEN` を共有し、`/v1/artifacts/{id}` の `project_id` は
  呼び出し側が指定する整合性チェックにすぎない。主体ごとの scope 検証は
  主体分離（設計 §18.2）と同時に入る。人間の判断（受け入れ・差し戻し）と
  Controller だけは専用 credential を持つ。
- `/v1/catalog` は Gateway が許可する action しか知らない。Agent binding と
  Worker executor の適合は Release の報告経路（Phase 3A）が無いため
  `availability=unverified` を返す。
- 担当変更（設計 §7.6）、設定編集・Config Release（Phase 3A）、
  Writer/Marketing pool（Phase 3B）は未着手である。
- Worker PVC 上の生ファイル（patch、ログ、preview HTML）の取得 proxy は未実装で、
  Worker の署名付き URL に依存している。台帳に保存した JSON 成果物の本文は
  `GET /v1/artifacts/{id}/content` から取得できる。
- Gateway から Worker への drain/resume は `POST /v1/workers/{id}/commands` で
  要求でき、scheduler が適用して `intake_applied` で一致を報告する。実行クラスや
  image、能力の変更は Config Release（Phase 3A）が前提で、まだできない。

## cursor の扱い

`/v1/snapshot` は repeatable read で業務データと `cursor` を読む。消費側は
`cursor` の次からイベントを取得し、checkpoint には返却されたイベントの
`next_cursor` を使う。`latest_cursor` は別に読んだ値なので checkpoint にしない。
`has_more` が真の snapshot は `GET /v1/tasks?cursor=` で続きを読み切ってから feed に
移る。`after` が feed の保持範囲より古い、または最新 cursor より先の場合は
`SNAPSHOT_REQUIRED` を返す。消費側は snapshot から作り直す。

## 配置（Phase 2 で必要な差分）

| 対象 | 設定 |
| --- | --- |
| Gateway | `internal-api` プロセス（`API_SURFACE=internal`、`127.0.0.1:8082`）と `CONTROLLER_API_TOKEN` |
| Tailscale Serve（Gateway VM） | `/internal/` を `http://127.0.0.1:8082` に proxy する path 設定を追加する。tailnet ACL は既存の `tcp:443` のままで足りる |
| Agent | `ai-business-workflow-controller` Deployment（同じ image を `python -m app.workflow_controller` で起動）と、Tailnet 宛 `443` を許可する CiliumNetworkPolicy |
| Secret | `ai-business-workflow-controller`（`gateway-internal-url`、`controller-api-token`） |

内部 API は Controller 専用である。public surface では 404 を返し、internal surface
でも `CONTROLLER_API_TOKEN` を持たない要求は 401 になる。Grok の MCP と
ブラウザからは到達経路がない。

## 配送（durable dispatch）

Job の登録と配送要求は同一トランザクションで `job_dispatches` に入る。送信は
scheduler（公開プロセス内の thread と `python -m app.scheduler` の両方で動作し、
advisory lock で 1 つだけが有効）が行う。

- 再送は常に同じ `dispatch_id` を使う。Worker は dispatch_id で重複排除するため、
  既に受理された仕事は同じ実行 ID を返す。二重実行にはならない。
- backoff は 1/2/4/8/16/30 秒、その後は 60 秒間隔の照合に移る。通信再送の回数は
  Workflow の修正回数や実行回数には加算しない。
- Worker が 4xx で拒否した場合、それが **最初の送信** なら definitive として Attempt を
  失敗させる（何も受け付けられていない）。ただし 401/403/407 は例外で、request の内容
  ではなく資格情報が拒否されただけなので definitive にしない：結果不明として再送に残し、
  token を直せば同じ request が通る。cancel の送信も同じ扱いで、`worker_commands` は
  `REFUSED` にせず `UNKNOWN` のまま再送する（Worker の接続設定が未設定の場合も同じ）。
  再送が 4xx で拒否された場合は、前の送信の結果が不明なので Attempt は失敗させない（後述）。
  5xx/408/429/応答消失/途中で切れた応答/読めない応答、および分類できない例外は
  すべて結果不明として再送し、backoff を使い切った時点で Job を `RECONCILING`、
  Task を `BLOCKED` にする（想定外の例外でも dispatch は必ず結果を記録してから
  次へ進む。claim したまま放置しない）。Attempt は非終端のままなので、後から
  本物の callback が届けば受理できる。
- cancel 要求も `worker_commands` に積んで scheduler が送る。要求が Worker に
  届いたことと停止が完了したことは別で、完了は Worker の callback で確定する。

## Worker の在庫

scheduler は配送のあいだに Worker の `GET /v1/status` を定期的に読み、`workers`
テーブルに記録する。`GET /v1/workers` は報告内容と「その報告がどれだけ古いか」を
分けて返す。heartbeat が途切れた Worker は `connection_status=UNKNOWN` とし、
受付可否は不明として扱う。受付中の Worker が 1 つも無ければ `can_execute=false` と
理由を返し、Control Plane は概要とかんばんの先頭に「実行できる Worker が
ありません」と表示する。

drain/resume は `POST /v1/workers/{id}/commands` で要求でき、scheduler が Worker の
`POST /v1/runtime/accepting` に適用して `intake_applied` で一致を報告する。Gateway は
Agent 経由でしか Worker に届かないため、在庫（`GET /v1/status`）と intake の変更は
Agent の同名の経路が中継する。

まだ実装していない: 複数 pool の在庫、実行クラスと設定 digest の照合（Config
Release = Phase 3A が前提）。

## 判断と権限

- `accept_deliverable` と `request_changes` は人間の判断である。REST でも
  `X-Human-Approval-Token` を要求し、記録される actor は
  `human:<HUMAN_APPROVAL_ACTOR>` になる。Gateway の共有 token だけでは実行できず、
  MCP にも公開していない。
- 検証（QA）の拘束は Gateway が行う。verification step の提案は次を満たさないと
  `TRANSITION_NOT_ALLOWED` で拒否する。
  1. Run が生成した**最新の** code change を入力に含む。
  2. その change を生成した実行を `source_worker_job_id` として指す。
  3. Task の受け入れ条件と、`acceptance_from` が指す成果物（PRD）の受け入れ条件を
     すべて `acceptance_criteria` に含む。
- 人間レビューの要求も、最新の change 自身に pass した検証がある場合だけ許可する。
  修正後の change は、以前の pass を引き継げない。
- QA の `pass` を認めるのは、条件ごとの verdict と evidence が揃い、すべて pass で、
  検証対象が「渡された Artifact」に一致する場合だけである。報告された総合判定が
  何であれ、条件のいずれかが fail なら `fail` とする。
- QA 報告は「何を検証したか」を名乗らなければならない。verification target を
  結び付けた attempt でも、対象（`source_worker_job_id` / `target_digest` /
  `target_artifact_id`）をひとつも示さない報告は `inconclusive` に落とす。QA は
  QA は成果物から組み立て直した workspace で動くため、名乗らない報告はどの変更に
  ついてのものか決められない。
  Gateway が照合するのは「どの実行・どの成果物についての報告か」という同一性であり、
  patch を持たない結果（`code-change-report`）は検証の対象にできない。組み立て直せない
  ものについて `pass` の意味を決められないので、進行は検証ではなく作業工程へ戻り
  （修正回数予算を消費する）、提案されても QA とレビュー要求は拒否する。
- QA 報告の判定一覧は `acceptance_criteria` ひとつだけを使う。`criteria` や `checks` を
  併記した報告は Worker が拒否し、Gateway は（古い報告のために）すべての一覧を見て
  判定する。片方だけ見て `pass` にはしない。
- 配送の拒否が「確定」になるのは最初の送信のときだけである。再送が拒否された場合
  （資格情報の入れ替えなど）、前の送信が受け付けられていたかどうかは分からないので
  Attempt は終了させず、`BLOCKED` として理由を記録する（再送では解決しないので、
  backoff を待たずに報告する）。既に受け付けられた実行（worker_job_id がある、または
  RUNNING）についても同じで、要求済みの停止も確定させない。
- ただし Attempt が既に終了している場合、あるいは Task が既に終端状態の場合、
  あとから届いた配送の拒否で Task の状態は変えない（レビュー待ちを壊さない、終わった
  Task を開き直さない）。どちらの場合も
  `task.dispatch_refused_after_completion` として記録だけ残す（`attempt_status` と
  `task_status` を添えるので、どちらの理由で見送られたか読み取れる）。
  検証の対象は「成果物から組み立て直した変更」である。Worker は実装が残した作業
  ディレクトリを複製せず、記録された base commit のクローンに記録された patch を
  当てて workspace を作る。したがって patch が再現できないもの（gitignore された
  生成物、あとから書き換えられたディレクトリ）は検証対象に入らない。組み立て直した
  結果が実装の `change_manifest`（内容・実行権限・symlink 先）と `workspace_digest` に
  一致しなければ QA を実行しない。
  Gateway が渡した `build_evidence` の `patch_digest` と `base_commit` が手元の記録と
  違えば拒否し、報告には実際に使った両方を記録する。Gateway は artifact に記録した
  patch_digest と base_commit の両方と照合し、報告側・記録側のどちらが欠けていても、
  違っていても `pass` を認めない（同じ patch でも base が違えば別の作業である）。
  検証中は対象ファイルを読み取り専用にし、終了後に残っている差分（内容・権限・
  symlink 先）があれば失敗させる。
- 依頼が指定できる `limits` は、実行が実際に適用する `timeout_seconds` と
  `max_output_bytes` だけである（他のキーは 422 で拒否する。効かない上限を
  記録すると、無い制約があるように見えるため）。Controller の提案と依頼の指定が
  食い違う場合、Gateway は厳しいほうを採用して Job に渡す。工程数や修正回数の上限は
  Workflow 定義側の値で、依頼からは変更できない。
- Controller が工程の入力を組み立てられない場合は
  `POST /internal/v1/runs/{id}/blocked` で理由を記録し、Task は `BLOCKED` になる。
  同じ入力 revision の同じ理由は一度しか記録しない。提案が通った時点で `ACTIVE` に
  戻るので、解除のための人間の操作は要らない。
- 一時停止の要求中に Run が終了した場合、保留していた pause は解消する
  （`task.pause_settled`）。Worker の報告で終わった場合と、配送が確定的に拒否された
  場合の両方で解消する。終了した依頼に「再開」は出さず、`retry` だけを出す。
- 追加指示に `restart_required` を付けると新しい入力版になる。確認待ちの成果物は
  レビューから外れ（`task.review_withdrawn`）、Run は作業工程（`revision_entry` の
  action step、まだ何も作っていなければ入口工程）から現在の入力版でやり直す。
  質問待ちだった場合も同じで、回答が進むはずだった先ではなく作業工程に戻る。
  `revise_input` command で目的・完了条件を直接変更しても同じで、実行中は拒否する
  （設計 §7.6 の「停止確定後に新しい入力版で再実行する」）。
- 単発 action（`single-action-v1`）は parameters が実行内容そのものなので、
  `revise_input` には新しい parameters が必要である。Run は `SUPERSEDED` で終わり、
  Task は READY に戻って新しい入力版で開始し直す。以前の実行が成功していても、
  その成功は旧版に対するものなので完了に使わない。
- 「いつ以降が有効か」は入力版の番号ではなく `requirements_revision`（目的・完了条件・
  parameters を実際に変えた最後の版。`restart_required` の指示も新しい要求なので該当する）
  で判断する。制限（limits）や参照の追加だけを変えた `revise_input` は誰の発言も取り消さ
  ないので、それ以前の作業・仕様・回答・修正依頼はそのまま有効であり、レビュー待ちの成果物
  も取り下げないし、作業をやり直させない（修正回数も消費しない）。開いている質問もその
  まま回答できる。前の版が本文（reason）で求めていたこと（`restart_required` の指示など）
  は新しい版にも引き継ぐので、後続の工程と検証にも届く。run state は
  `requirements_revision` を返す。
- Controller 駆動の workflow（`mvp-build-v1`）の `revise_input` は `parameters` を受け付け
  ない。どの工程も読まないため、指示が誰にも届かないまま前の版の指示を無効化してしまう。
  作業内容は目的・完了条件・追加指示で表す。
- 変更前の版で成功した作業は、現在の版の検証対象にできない。QA の提案は
  `TRANSITION_NOT_ALLOWED` で拒否し、進行は作業工程に戻る。旧版で作られた PRD の
  完了条件も、現在の版の要求ではないので必須条件に数えない（入れ替えられた条件を
  満たすことは不可能で、修正回数を消費するだけになる）。旧版の QA レポートも同じで、
  修正指示としては渡さない。旧版に対する回答は必須入力にはせず「旧版（第 N 版）への
  回答」として参考情報としてだけ渡す（捨てはしないが、現在の依頼を上書きさせない）。
  旧版に対する修正依頼は指示として渡さない（履歴には残る）。旧版の PRD も、記述
  （タイトル・課題・機能）は作業指示に入れず参考としてだけ渡す。
- 現在の版に対する修正依頼は、新しいものが来ても古いものを取り下げない。人が求めた
  修正はすべて（新しい順に）修正工程と検証工程へ渡す。
- 単発 action の `qa.review` も、workflow の検証と同じく「Gateway が記録した変更」に
  結び付ける（`source_worker_job_id` が作った `code-change` artifact）。結び付ける
  記録が無ければ、その pass は何に対する検証かを確定できないので inconclusive にする。
  Task の完了条件は action の入力にも渡す（`code.build`/`code.fix`/`qa.review` は
  `acceptance_criteria`、`product.plan` は `constraints`）。
- 追加指示は「未反映」のあいだ run state に載り、Controller が必ず工程へ渡す
  （切り詰めない）。attempt を作った時点でその指示は消費済みになり、以降は履歴として
  だけ残る。ただしその attempt が失敗・中止で終わった場合、その指示は再び未反映に
  戻り、次に渡された attempt（再実行）がその消費者になる。目的か完了条件を
  **実際に書き換えた** `revise_input` も「依頼そのものに取り込んだ」とみなして消費する
  （同じ目的・同じ完了条件を送り直しただけの場合は取り込みではないので消費しない。
  何も渡されないまま消費済みになると、その指示はどの工程にも届かない）。未反映の指示が 1 工程に渡せる量を超えている間は Controller が
  `BLOCKED` として報告し、`revise_input` での取り込みが解除方法である。
- 回答・人間の修正依頼・`context_refs` も同じ扱いで、必ず渡すか報告する。回答は
  1 件 4,000 文字・1 質問群 12,000 文字までに制限する。訂正できるのは「その回答で
  まだ何も実行していない」あいだだけで、attempt が作られた後は拒否する（作られた仕事は
  古い回答に対する仕事なので、黙って差し替えると検証が別物になる）。
- 単発 action（`single-action-v1`）は実行内容が parameters なので、追加指示
  （`next_attempt` / `restart_required`）と `context_refs` を受け付けない。読む経路が
  無いものを記録して無視するより、拒否して `revise_input` に誘導する。コメント
  （`note_only`）は人間が読むものなので受け付ける。
- run state の回答はその Run のものだけを載せる（終わった Run の回答は履歴である）。
- 依頼内容の変更でやり直す工程にも修正回数の上限が効く。`plan` へ戻る場合も
  Run の修正回数予算を消費し、使い切ったら `revision_limit_reached` で Run を
  失敗させる（人間の変更でも、際限なく作業を作り直すことはできない）。再検証は
  その修正の一部なので別枠では数えない。
- Worker が「停止を確認できなかった実行」を報告している間、その枠は capacity として
  数えない。`GET /v1/workers` は `not_stopped`・`usable_slots` を返し、受付中でも
  使える枠が無い Worker しかなければ `can_execute=false` とその理由を返す。
- QA 報告のフィールドが期待した形でない場合（配列のはずが数値、verdict が文字列で
  ないなど）、Gateway は読めなかったものとして扱い、判定は読めた条件だけで決める。
  projection で例外を投げると callback が延々と再送されるため、そうしない。
- 要求が変わった Run（`requirements_revision` が進んだ場合）は、どの工程で止まって
  いても（成功・失敗・回答済みの質問）作業工程からやり直す。判断は「この Run の attempt
  が `requirements_revision` 以降に実行されたか」だけで、工程の種類や結果には依らない。
  制限や参照だけを変えた版では何もやり直さない。
- Gateway が提案を `TRANSITION_NOT_ALLOWED` で拒否した場合、Controller はそれを
  Run に記録する（`BLOCKED`）。競合なら次の評価で解消し、恒常的な不一致なら
  ACTIVE のまま黙って回り続けることはない。その記録自体の送信が失敗した場合はログに
  残して次の評価で再試行する（記録できるまでは Task の状態は変わらない）。
- 単発 action への `restart_required` 指示は拒否する。実行内容は parameters なので、
  指示文では新しい実行内容を表せない（`revise_input` に parameters を渡す）。REST と
  MCP は同じ実装を通るので、経路による違いはない。
- Worker の再起動時は「その実行がまだ動いていないか」を、実行が記録した
  process group と job id で process table に照会して確認する。
  残っていれば停止させ、停止を確認できた場合だけ `INTERRUPTED`（終了）として報告する。
  確認できない場合は `NOT_STOPPED` のまま開いておく。Gateway は `INTERRUPTED` を
  終了として扱い、要求済みの停止をそこで確定させるため、これは本当でなければならない。
- Worker が実行を止められなかった場合（シグナルが届かない、終了を確認できない）、
  Job は `NOT_STOPPED` として開いたままになり、Gateway には `stopped: false` を付けた
  progress だけが届く。Gateway はこれを進捗ではなく「確認が必要」として扱い、Task を
  `BLOCKED` にし、理由を Attempt の result_summary に残す。Attempt は終了扱いにせず、
  要求済みの停止も確定しない。
- 入力版に付けた `context_refs` は run state に含まれ、Controller が工程の
  context として渡す（参照の中身を Gateway が取得することはしない）。
- 人間レビューの許可条件は「最新の変更に対する pass した検証が、`requirements_revision`
  以降に記録されていること」である。要求が変わった時点で、以前の pass は現在の依頼の
  根拠にならない（制限だけを変えた版では根拠のまま残る）。
- 修正（fix）と再質問（request_input）は同じ Run の修正回数予算
  （`max_revision_cycles`、既定 2）を共有する。片方から回り込めない。

## Phase 2 で動くようになったこと

- `mvp-build-v1` の進行：計画 → 実装 → 検証 → 差し戻し（修正）→ 再検証 →
  人間レビュー → 受け入れ。遷移の可否は Gateway が判断し、Controller は
  「次に許された遷移」を提案するだけである。
- 操作 command：`start / pause / resume / cancel / retry / request_changes /
  accept_deliverable`。`Idempotency-Key` と `expected_revision` を使い、
  再送は同じ結果を返す。
- 入力要求と回答：`request_input` で質問を保存し、回答は対象の質問 ID と
  入力 revision を検証してから工程を再開する。
- 判断：成果物の digest を Gateway が再計算し、対象が変わった判断は
  `ARTIFACT_MISMATCH` で失効させる。
- cancel は「要求」と「完了」を分ける。実行中は `CANCEL_REQUESTED` のままで、
  Worker が終了を報告して初めて `CANCELLED` になる。

## Phase 2 で未実装のもの

- 主体の分離は部分的である。人間の判断は `HUMAN_APPROVAL_TOKEN`、Controller は
  `CONTROLLER_API_TOKEN` を持つが、人間の操作・Grok・Control Plane サービスは
  まだ同じ `GATEWAY_API_TOKEN` を共有している。

## ローカルでの通し確認

四つのサービスを実プロセスで動かし、依頼から受け入れまでを確認する手順。

```sh
# 1. Gateway 一式（db / public / callback / internal / scheduler）
docker compose --env-file <env> -p gateway-smoke up -d --build \
  db public-api callback-api internal-api dispatch-scheduler

# 2. Worker の代役（Codex は動かさない test double）
WORKER_CALLBACK_TOKEN=... WORKER_API_TOKEN=... python3 scripts/stub-worker.py

# 3. 本物の Workflow Controller（ai-business-agent）
GATEWAY_INTERNAL_URL=http://127.0.0.1:8082 CONTROLLER_API_TOKEN=... \
  python -m app.workflow_controller

# 4. 依頼を作る（mvp-build-v1）
curl -X POST -H "Authorization: Bearer $GATEWAY_API_TOKEN" \
  -H "Idempotency-Key: smoke-1" -H 'Content-Type: application/json' \
  -d '{"project_id":"smoke-product","title":"...","objective":"...",
       "acceptance_criteria":["..."],"workflow_id":"mvp-build-v1","start":true}' \
  http://127.0.0.1:8080/v1/tasks
```

`WORKER_BASE_URL` は stub worker を直接指す（compose からは
`http://host.docker.internal:9099`）。クラスタでは同じ経路が Agent
（`.../agent`）を通り、dispatch も在庫も intake の変更も Agent が中継する。2026-09-20 の確認では、計画 → 実装 → 検証 →
レビュー要求まで進み、`X-Human-Approval-Token` の無い受け入れは 401、古い digest の
受け入れは 409、人間の token での受け入れで `COMPLETED` になり、判断の actor が
`human:<HUMAN_APPROVAL_ACTOR>` として記録されることを確認した。`drain` は
`desired_accepting_jobs=false` → Worker が適用 → `intake_applied=true` と
`can_execute=false` に反映された。Control Plane も同じ Gateway に対して
かんばん・概要・依頼作成が動作した。

この確認は Codex を動かしていない。Worker 側のテストも Codex の代役プログラムを
使うため、**本物の Codex CLI を通した実行はどのテストでも確認していない**。
Worker のテストが確認しているのは、実行の前後（workspace の同一性、変更の記録と復元、
プロセスグループの終了、callback の永続化）である。Gateway・Controller・Agent の
取り合いは `compose.e2e.yaml` のクロスリポジトリテストで確認している。
