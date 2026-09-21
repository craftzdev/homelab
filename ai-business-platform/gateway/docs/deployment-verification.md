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

## 初回検証時に残っていた接続

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

At this earlier validation point, the dedicated GitHub App and trial-provider credential were **not provisioned**. Neither the configuration controller nor
the trial broker has been started. No candidate has been sent to the real model or automatically promoted in this validation.
The credential-mount issue found in #67 was corrected before activation: only the separate broker Pod can mount provider auth;
candidate Pods have no provider credential and no direct Internet/DNS egress.


## Dedicated App and isolated provider activation — 2026-09-21

GitHub App `craftz-ai-config-controller` (App ID `5017561`, installation `163414060`) was installed
on exactly `craftzdev/ai-business-agent` and `craftzdev/ai-business-worker`. The installation token's
repository inventory also returns exactly those two repositories. Permissions are Contents/Pull requests
write and Actions/Checks/Metadata read. The App key is root-owned, service-group readable at mode 0640;
registration-time local secrets were removed after verifying the installed key.

The broker in `ai-config-trial` uses only the current Worker's access token and account ID.
No refresh token or ID token is copied. The bootstrap Secret had a revoked token and must not be the
source of credential reuse. The current access token expires at 2026-10-01 14:16 JST and can be invalidated
earlier by session changes; it needs an operator resync when expired/revoked. Broker failures stop candidate
promotion. This setup does not refresh the Worker's shared OAuth session.

Live validation found and fixed five integration problems: the kubelet TCP readiness probe was blocked
by the restricted ingress policy; stale bootstrap credentials were revoked; valid upstream SSE lacked a
Content-Type header; the production inventory serialized absent source mappings as null; the rollout observer
needed to select the named Deployment from a multi-document YAML containing a Service. The VM also
needed python3-venv and an outbound firewall exception limited to the Kubernetes API at 172.16.40.10:6443.
Other workload/management/Ceph VLAN denies remain in place.

- Controller tests: 30 passed; CI runs both controller contracts and repository validation.
- Real provider smoke: `broker-smoke-477d9e5a9a`, succeeded with Codex 0.153.4 and the supplied
  software profile, schema, two skills, and harness. No provider credential was mounted into the trial Pod.
- Negative network check: broker reachable; direct public Internet, Kubernetes API and DNS blocked;
  `/auth/auth.json` absent. The separate installation smoke resources were removed after verification.
- Controller is enabled as an independent systemd service on `ai-gateway-01`. Its App token is refreshed
  in memory and its Kubernetes token belongs to the scoped controller ServiceAccount. Live authorization
  checks allow trial Job creation and named Deployment reads, and deny production Deployment writes
  and Secret reads (both trial and production namespaces).
- Production UI shows the immutable candidate, automatic-promotion choice, CI/trial evidence, diff and history.

### Candidate delivery

Candidate `a7c9e089-961e-4dc8-a15e-5055789b36a9` removes only the redundant final blank line from
`profiles/software-engineer-v1.md`; it changes no role instructions. It was created and validated through
the management API with automatic promotion explicitly enabled for this candidate.

- [App-created PR #8](https://github.com/craftzdev/ai-business-agent/pull/8)
- Candidate head: `1d5a966f6bf452b2ab2f16349e8c56b050b7bd0b`
- Content SHA-256: `d0b5881572f6eaa1dc4ec4e33550be37b5a00d111303bcd303f4ba164270fe36`
- Dedicated trial Job: `config-a7c9e089-1d5a966f6bf452b2`, succeeded.
- Trial configuration SHA-256: `4fb3399c9426abb3b664acd8934b376aad143397e403f1db0b9dc7415cfcfa66`
- Runtime SHA-256: `80652ec34129d4ccea2c33b48c2cda456a13106e5998c531e6367e63301fabfd`
- [config-check](https://github.com/craftzdev/ai-business-agent/actions/runs/35569715959) passed.
- The controller recorded VERIFIED, the Gateway emitted PROMOTE_REQUESTED using the candidate's
  prior authorization, and the dedicated App merged revision `cae1fee6ed2c47e37809322509c7dbc06e42855f`.
- [Signed image workflow](https://github.com/craftzdev/ai-business-agent/actions/runs/35570028109).

The candidate's receipt is from its own execution, not the installation smoke or an older production job.
This verifies actual model connectivity and configuration supply, not arbitrary business-task quality.

Final result: **DEPLOYED** at `2026-09-21T06:56:12Z` (15:56 JST). The production UI shows 配布完了,
all target deployments observed, and matching installed/loaded profile hashes. Argo CD automatically
synced Git revision `f54417eb67634247224dc1dcf1508506a08e8c51`; no manual sync or direct deployment write was used.

| Deployment | Observed generation | Available replicas |
|---|---|---|
| ai-business-agent | 13 | 1/1 |
| ai-business-workflow-controller | 5 | 1/1 |
| ai-business-agent-edge | 10 | 1/1 |

Image: `172.16.40.201:5000/ai-business/ai-business-agent@sha256:9879e909d0b95f889aded7bba0ab41a09a6efa1f26feebeb8ad152e6a61ee281`.
