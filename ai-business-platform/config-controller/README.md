# Configuration controller

Control Plane は人間向けの管理画面、Gateway は設定変更・監査の正本、
このプロセスは GitHub 書き込みを行う専用コントローラーです。
通常の MCP/Bot 資格情報では設定の適用を承認できません。

## Implemented path

1. `/profiles` から配置中のプロファイル・スキル・ハーネスを選び、下書きを保存して基本検証する。
2. 保存した版から配布候補を作成する。本文・編集元・ハッシュは固定され、後から下書きを編集しても変わらない。
3. Controller が Git の編集元を照合し、`codex/config-<release UUID>` ブランチと PR を作成する。
4. 固定した PR head に対する必須 CI と実行試験を確認する。未取得・失敗・再実行待ちは合格にならない。
5. 人間が `/releases/<id>` で適用を要求する。Controller は head・base・CI を再確認し、同じ head を指定して merge する。
6. 配布画面は Git 反映・配置ファイル・Agent の読み込み版を分けて表示する。
7. 戻す操作は変更前の内容を持つ新しい下書きを作る。同じ検証・適用の手順を通す。

一度に扱うのは **1ファイルの変更**。複数ファイルを一括で昇格するプロファイルバンドル、
プールの作成・自動カナリア切替、モデルや権限の汎用エディターは未実装。
`pool` は監査上の対象名であり、実行先を切り替えるスケジューラーではない。

## Run

単一プロセスで稼働させる。多重起動を前提にした分散リースは未実装。
コンテナは read-only filesystem / non-root / service-account token なしで実行できる。
必要な接続先は Gateway と `api.github.com:443` のみ。Kubernetes 資格情報は不要。

```sh
python -m pip install -r requirements.txt
python controller.py --targets /etc/config-controller/targets.json --once
```

常駐時は `--once` を外す。設定ファイルには `targets.example.json` を使用する。
書き込み可能な repository / branch / path / 必須 check はオペレーターが管理する。
UI や下書きの本文から任意のコマンド、URL、リポジトリを指定することはできない。

環境変数:

| Variable | Location / purpose |
|---|---|
| `GATEWAY_URL` | Controller → Gateway の到達可能な URL |
| `GATEWAY_API_TOKEN` | Controller の Gateway Bearer 認証 |
| `CONFIG_CONTROLLER_TOKEN` | Gateway と Controller の両方に設定する32文字以上の専用 secret |
| `GITHUB_TOKEN` | Controller のみ。対象2リポジトリの Contents / Pull requests write、Checks read |
| `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET` | Cloudflare 経由の場合の接続情報 |
| `CONFIG_ADMIN_TOKEN`, `CONFIG_ADMIN_ACTOR` | 既存の Gateway 設定編集資格情報。Control Plane には token のみを渡す |

Controller に `CONFIG_ADMIN_TOKEN` を渡さない。Control Plane / Gateway に `GITHUB_TOKEN` を渡さない。
GitHub App installation token の短期発行・更新は起動基盤側で行う。
任意のユーザーが同名 check を成功扱いにできないよう、必須試験を実行する
GitHub Actions ワークフローと対象ブランチを保護する。

## Production integration

Gateway の管理 API と Control Plane の管理資格情報は本番に設定済み。
Agent / Worker / Control Plane の container CI は、テスト・ビルド・署名後に
Deployment の immutable digest を Git に反映し、Argo CD が同期する。
ソースが先に更新された古いビルドは昇格しない。`MERGED` は配置完了を意味しない。

Worker の planned rollout は preStop で受付停止し、実行中・待機中・停止未確認・
未送信 callback が 0 になるまで待つ。既定キューに対応する 10 時間の猶予を設定。
ノード障害や猶予超過時の強制終了まで防ぐものではない。

残っている接続:

- **Controller identity**: 専用 GitHub App または repository 限定 token の選択・設定。
  ARC Runner 管理用 App は Contents 権限を持たないため流用しない。
  常駐 Controller に利用者の汎用 gh token を保存しない。
- **Candidate runtime trial**: `config-check` は契約テストであり実モデル試験ではない。
  必須 `config-runtime-trial` は専用の試験環境と資格情報を設定してから有効化する。
  候補の正確な commit を実行し、代表ジョブ・成果物・設定ハッシュを評価する。
  それまで候補を VERIFIED にしない。本番の実行確認は候補 PR の合格証跡に転用しない。
- **Observation**: 現状の inventory は1つの Agent 接続から取得する。
  全プール・全レプリカへの反映は確認できない。ファイル一致を全体の反映成功と解釈しない。

ハーネスは `harness/AGENTS.md` → ConfigMap の `data.AGENTS.md` に固定対応する。
Controller は同じ commit 内で Pod template に release ID の annotation を追加する。
これにより Argo CD 同期後に subPath mount を持つ Pod が作り直される。

## Runtime evidence

Agent は起動時の capabilities / profiles / schemas を固定する。配置ファイルが変わっても
プロセス再起動までは読み込み版が変わらず、既存 dispatch の再送も保存済みの版を使う。
Worker は起動する実行へ渡すハーネス・SKILL.md 本文を取り込み、`configuration.json`
と結果の `configuration` にハッシュを記録する。本文・認証情報は記録しない。
スキルの付属テキストファイルは「観測した版」であり、モデルが読んだ証明ではない。
ファイルやプロンプトは OS の権限境界の代わりにならない。

## Validation

```sh
python -m pytest -q test_controller.py
```

HTTP の書き込みが不確定な場合は同じ branch / PR を再確認する。Git の編集元、
head、base が変わった場合は BLOCKED として新しい候補の検証を要求する。
ネットワーク障害は secret やレスポンス本文をログに出さず再試行する。
