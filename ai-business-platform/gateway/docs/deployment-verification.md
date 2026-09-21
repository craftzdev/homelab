# 本番接続検証 — 2026-09-21

## 配置

Gateway を VM ai-gateway-01 に配置し、public/callback/internal API の health と DB 接続を確認。
更新前のソース・.env・DB dump は VM の `/opt/ai-business-gateway-backups/control-plane-production-20260921` に保存。
管理資格情報は Gateway と Control Plane の server-side secret に設定。Git には保存していない。

GitHub Actions → テスト → build → Harbor → Sigstore 署名 → digest commit → Argo CD を実走した。
3 Application はすべて Synced / Healthy。Agent の本体・edge・workflow controller を同一 digest に更新。

| Service | Image digest | Successful CI |
|---|---|---|
| Agent | `sha256:b124508074d33345be75586b2c8a1ef7f820e57c009e5ba2fc9ba7d3ad7ad5cf` | [run](https://github.com/craftzdev/ai-business-agent/actions/runs/35563574478) |
| Worker | `sha256:ce5d391fc97ab0818cf382b3a8c6e72f1304bb331cb8eee8958ff15b2aea9285` | [run](https://github.com/craftzdev/ai-business-worker/actions/runs/35563593811) |
| Control Plane | `sha256:d3e95552c6e35d33d5fc7ebb5da592505b2ba508ebf85c0b31bcdddaeb0032a2` | [run](https://github.com/craftzdev/ai-business-control-plane/actions/runs/35563576213) |

## API / UI

- 認証付き `/v1/config/contract` が version 1.0 を返す。
- 管理 OpenAPI に 14 paths。release promotion の schema を取得できた。
- 一般 Gateway token だけの管理操作・Controller work 取得は 401。
- 古い intake revision は 409。
- 管理 API で受付停止（revision 1）→実際の停止を観測。
- 本番 UI で受付再開（revision 2）→反映確認済みを観測。
- 本番ブラウザでログイン、プロファイル一覧、ワーカー、タスク、成果物を確認。
- 更新中に inventory が一時 502 となったが、Worker 起動後に正常取得へ復帰。

## 実モデル試験

本番 Gateway / Agent / Worker / Codex を経由し、隔離された既存 worker-demo workspace に
`code.build` を実行した。job environment は **preview**。外部公開・デプロイを伴う
業務ジョブや、未検証の候補 PR に対する試験ではない。

- Gateway Job: `9f4949d2-fb23-4407-9e1a-52daa94d67e8` — SUCCEEDED
- Worker Job: `wjob_36f19fbf9e074a359b9116018b6e5a6e`
- [Control Plane task](https://ai-business-control-plane.tailb6c7d.ts.net/tasks/d7a0f42a-3e21-4210-ac44-88358a663135)
- calculator の multiply と正数・負数・ゼロのテストを追加。既存を含む5件が成功。
- Codex exit 0、unittest exit 0、変更2ファイル、Gateway callback と成果物保存に成功。
- configuration.scope = supplied_to_runtime、launched = true。
- 実行設定 digest: `7e9d35067ae8237e9c049089ebc420c55353b8a1f9e20f41693e79e5fcd17ff4`
- profile、schema、2つの SKILL.md、共通ハーネスのハッシュを結果と configuration.json に記録。
- 終了後は受付中、running / queued / not_stopped / callback_backlog はすべて0。

## 残っている接続

専用 GitHub App または repository 限定 token の選択・設定が未完了のため、
設定配布 Controller は常駐させていない。candidate PR の config-runtime-trial も
専用試験環境・資格情報が必要。今回の本番 smoke を候補 PR の合格証跡として扱わない。
これらが揃うまでは UI の設定候補を自動的に VERIFIED / MERGED にしない。

UI と API の分離、および main から署名済みイメージを本番配置する経路は接続済み。
設定を編集してから候補試験・人間承認を通す全経路は、上記2点の接続待ち。


## Configuration automatic delivery integration — 2026-09-21

Merged homelab #67, Agent #7, Worker #7, and Control Plane #10. Gateway API source was backed up with the database to
`/opt/ai-business-gateway-backups/config-autodeploy-20260921` before updating only the two changed API files.
The public/internal/callback API containers are healthy and the dispatch scheduler is running.
`/v1/config/contract` exposes `automatic_promotion` and `deployment_observation` on API version 1.0.

The signed-image pipelines completed, all three Argo CD applications are Synced/Healthy, and the live Deployment
source annotations and observed generations match. Worker intake is enabled with one usable slot and no unconfirmed execution.

| Deployment | Source revision | Observed generation | Available replicas |
|---|---|---|---|
| ai-business-agent | `175e4fa1fcf3dc2c0e52909e2ad65ebabf50b51a` | 12 | 1/1 |
| ai-business-workflow-controller | `175e4fa1fcf3dc2c0e52909e2ad65ebabf50b51a` | 4 | 1/1 |
| ai-business-agent-edge | `175e4fa1fcf3dc2c0e52909e2ad65ebabf50b51a` | 9 | 1/1 |
| ai-business-worker | `3a0590c2442f112df9c0c542708b8af81b402b4c` | 16 | 1/1 |
| ai-business-control-plane | `55192cc2f26f9724ee15608ee0c599eab23cc12e` | 9 | 1/1 |

Images:

- `172.16.40.201:5000/ai-business/ai-business-agent@sha256:bd00f71206bdbb7496bf18c50f3e23761643100b5f584aca26c3d3b1b10e4c7a`
- `172.16.40.201:5000/ai-business/ai-business-worker@sha256:c0f2796e0e89602421bed25ba67b9040b93d9cb75c356cbb9b5e45aeb3c08316`
- `172.16.40.201:5000/ai-business/ai-business-control-plane@sha256:5224b605b0985161ef8186deca1bf1e28eef4d786bb7be802a4b74a7f485d950`

The production settings/profile UI loads both Agent and Worker inventory and shows the automatic-delivery workflow.
The release checkbox and CSRF/revision forwarding are covered by the 68 UI tests. Gateway: 268 passed / 1 skipped.
Controller: 25 passed, including candidate recovery, immutable evidence, rollout observation, broker credential separation,
restricted network policy, fixed upstream destinations, and sanitized errors.
Codex 0.153.4 also completed a local mock SSE response through the custom provider with no auth file or Authorization header.

The dedicated GitHub App and trial-provider credential are **not provisioned**. Neither the configuration controller nor
the trial broker has been started. No candidate has been sent to the real model or automatically promoted in this validation.
The credential-mount issue found in #67 was corrected before activation: only the separate broker Pod can mount provider auth;
candidate Pods have no provider credential and no direct Internet/DNS egress.
