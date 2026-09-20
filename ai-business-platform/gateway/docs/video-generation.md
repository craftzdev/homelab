# ComfyUI 動画生成 REST / MCP

## 利用方法

公開入口は既存の `https://gateway.craftz.dev`。Cloudflare Access のサービス認証と
Gateway Bearer Token の両方が必要。ComfyUI 自体はインターネットへ公開しない。

### REST

`POST /v1/jobs` に以下の JSON を送る。

```json
{
  "action": "video.generate",
  "project_id": "my-product",
  "environment": "preview",
  "parameters": {
    "prompt": "A small red sailboat floating on a calm pond in warm morning light.",
    "seed": 42
  },
  "limits": {"timeout_seconds": 900}
}
```

必要ヘッダー:

```text
Authorization: Bearer <Gateway API Token>
CF-Access-Client-Id: <Access service token ID>
CF-Access-Client-Secret: <Access service token secret>
Content-Type: application/json
User-Agent: My-Business-Client/1.0
Idempotency-Key: <依頼ごとに一意、8〜200文字>
```

202 の `job_id` を保存し、同じ認証ヘッダーで `GET /v1/jobs/{job_id}` をポーリングする。
このゾーンでは汎用 `Python-urllib` の User-Agent が Cloudflare の Browser Integrity Check
で拒否される場合がある。クライアント自身の識別名を明示する。Access / JWT / Bearer 検証は無効化しない。
`SUCCEEDED` 後、`GET /v1/jobs/{job_id}/video` で MP4 を取得できる（同じ二重認証が必要）。
Range リクエストにも対応し、応答は `video/mp4` / `private, no-store`。

`result.artifact.sha256` と `bytes` で取得内容の一致を確認できる。
`result.review.url` は人間向けの ComfyUI 直接再生 URL（Tailnet 内のみ、署名 URL ではない）。
これは ComfyUI 上で動画を削除すると利用できなくなるが、Gateway の保存コピーは独立している。

### MCP

既存の `https://gateway.craftz.dev/mcp` の `submit_job` を使用する。
引数は REST の JSON に `idempotency_key` を加えたもの。

1. `submit_job(action="video.generate", ...)`
2. `get_job(job_id=...)` または `wait_for_job(job_id=..., timeout_seconds=60)`
3. `get_review_url(job_id=...)` または結果の `artifact.url` を取得

同一依頼の通信再試行では **同じ idempotency_key と入力を使う**。
REST → MCP の再送でも同じ Gateway job が返る。別の内容に同じキーを使うと拒否される。
MCP クライアントに古いスキーマがキャッシュされている場合は tools/list を再取得する。

## 制限・安全性

- 固定ワークフロー `fasth3-5s-v1`、FastH3 8-step、896×512、124フレーム、24fps（約5.17秒）。
- `prompt`: 空白のみ不可、最大2,000文字。`seed`: 0〜4,294,967,295。
- `workflow` は省略可能だが指定時は `fasth3-5s-v1` のみ。
- 任意 URL、ノードグラフ、モデル名、ファイルパス、解像度、コマンドは入力不可。
- `environment`: research / preview のみ。production は拒否。
- タイムアウト120〜1,800秒、既定900秒。待機時間・成果物取得を含む期限。
- 最大8件の未完了依頼、同時生成はGateway経由で1件。ComfyUIの既存キューが空になるまで待つ。
- 他の利用者の実行は停止しない。タイムアウトも ComfyUI のグローバル interrupt を呼ばない。
- 実行先は `https://omen45.tailb6c7d.ts.net:8443` のみ。TLS検証有効、HTTPリダイレクト禁止。
- Worker callback による動画ジョブの状態更新は拒否。

## 永続化・障害時

Gateway PostgreSQL の既存 jobs に入力・状態・ComfyUI prompt_id を保存。
公開APIプロセス内のリコンサイラーが処理し、PostgreSQL advisory lock で多重実行を防ぐ。
`RUNNING` は再起動後に同じ prompt_id を照会するため再生成しない。

ComfyUI は Gateway の idempotency key を保証するシステムではないため、
送信中クラッシュ・送信結果不明時は `NEEDS_REVIEW` とし、自動再送しない。
人間が ComfyUI の履歴・キューを確認し、必要な場合のみ新しい依頼を作る。
出力ファイルのプレフィックスは `Gateway/{Gateway job UUID}`。

MP4 は Docker volume `ai-business-gateway_video-data`（`/data/videos`）に保存。
転送上限128MiB/件、保存容量の受付上限2GiB、保存期間7日。
期限切れファイルはリコンサイラーが削除し、取得APIは410を返す。
ジョブのDB記録とSHA-256は残す。この保存は短期プレビュー用で、MinIOへの正式な成果物登録は未対応。
恒久保管が必要な成果物は別途アーカイブする。

`COMFYUI_BASE_URL` は compose の既定値で有効。
`.env` で `COMFYUI_BASE_URL=` と空指定すると受付・リコンサイラーを停止できる。
無効化した後も既にComfyUIに受理された生成は停止しない。

## 検証・運用

```sh
docker compose -p gateway-video-test -f compose.test.yaml run --rm tests
```

`scripts/video-smoke-test.py` は既存の3種類の認証情報を環境変数で受け取り、
外部URLで REST / MCP 各1件を実生成する。通常CIでは実行しない。
認証拒否、不正パラメータ、MCPディスカバリー、クロスプロトコル再送、完了状態、
MP4とSHA-256の一致、レビューURLまで検証する。

参照: [ComfyUI 公式APIルート](https://docs.comfy.org/development/comfyui-server/comms_routes)。
