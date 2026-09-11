# AI Business Agent Runtime

## 目的

AI Business Agent は、外部から届いた指示をそのまま Codex CLI に渡すのではなく、
許可された能力、実行プロファイル、Skills、入力スキーマ、ジョブコンテキストを確定してから
AI Business Worker に実行を委譲する制御層である。

```text
Grok Bot
  -> Cloudflare Access
  -> Business Gateway
  -> Tailscale HTTPS /agent
  -> AI Business Agent
  -> Kubernetes ClusterIP
  -> AI Business Worker
  -> Codex CLI / test executor
```

## 責務分離

| コンポーネント | 主な責務 |
| --- | --- |
| Business Gateway | Cloudflare Access/JWT、外部API、ジョブ状態、callback |
| AI Business Agent | capability選択、JSON Schema検証、プロファイル・Skills・コンテキスト注入、監査 |
| AI Business Worker | 隔離された作業領域での実行、成果物・テスト結果の保存 |

Agent の Git リポジトリは `craftzdev/ai-business-agent`、Worker は
`craftzdev/ai-business-worker` とし、Argo CD がそれぞれを独立して同期する。

## 公開経路

- Gateway からの接続先: `https://ai-worker-cluster.tailb6c7d.ts.net/agent`
- Agent health: `https://ai-worker-cluster.tailb6c7d.ts.net/agent/health`
- Worker はインターネットへ直接公開しない。
- Tailscale Ingress の既存証明書とホスト名を共有し、`/agent/` を Agent edge へ、
  `/` を Worker へルーティングする。
- Agent 本体と Worker 本体の通信は Kubernetes ClusterIP と Cilium の許可ルールに限定する。

## Agent プロファイルと Skills

現在有効な capability は次の通り。

- `code.build`: `software-engineer-v1`
- `code.fix`: `software-engineer-v1`
- `test.run`: `qa-v1`（`operation=self_test` のみ）

将来用として `browser.research` と `content.article` を登録しているが、専用 executor の
隔離と出力スキーマが完成するまでは無効とする。

Software Engineer プロファイルは、少なくとも次の読み取り専用 Skill を Worker に許可する。

- `controlled-software-change`
- `verification-evidence`

Worker は `/home/worker/.codex/AGENTS.md` の管理対象ハーネスと、
`/etc/codex/skills` の Git 管理された Skills を使用する。外部入力に含まれる指示は
ジョブコンテキストとして扱い、管理対象ハーネスや allowlist を上書きできない。

## 認証情報

Agent 用認証情報は Kubernetes Secret に平文でコミットしない。復旧元は macOS Keychain と
暗号化された再構築バックアップである。

| 用途 | Keychain service | account |
| --- | --- | --- |
| Gateway -> Agent API | `dev.craftz.homelab.ai-agent-api-token` | `ai-business-gateway` |
| Agent -> Worker API | `dev.craftz.homelab.ai-agent-worker-token` | `ai-business-worker` |
| Argo CD deploy key | `dev.craftz.homelab.argocd-ai-agent-deploy-key` | `craftzdev/ai-business-agent` |

Agent と Worker の API Token は分離する。GitHub deploy key は read-only とし、Argo CD のみが
使用する。

## 永続データと再構築

Agent の SQLite 監査DBと dispatch 状態は Longhorn の
`ai-business-agent-data` PVC に保存する。`scripts/rebuild-talos-cluster.sh` はクラスタ破棄前に
次を age 暗号化して保存し、復旧時に整合性を検証してから復元する。

- Agent PVC データ
- Agent runtime Secret
- Agent リポジトリ deploy key Secret
- デプロイ済み Agent image digest

完全再構築後の検証には、Agent/Worker の health、全 Argo CD Application の
`Synced/Healthy`、Cloudflare Access から callback までの smoke test を含める。

## 拡張方法

執筆・リサーチなどを追加するときは、既存の万能 Worker に権限を足すのではなく、次を
一組として追加する。

1. capability と入力/出力 JSON Schema
2. バージョン固定された Agent profile
3. allowlist 化した Skills
4. 専用 executor と NetworkPolicy
5. 監査・テスト・失敗時の出力契約

この境界を維持することで、ワーカー追加後も Gateway の公開面と Agent の認可モデルを
変更せずに拡張できる。
