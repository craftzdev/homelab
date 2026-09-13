# 承認紐付けの導入記録（2026-09-13）

## 現在地

**実装はmainへpush済み、本番API切り替えは未実施。**
対象commitは `e7e8ae6`。Gateway VMのディスク応答が不安定なため、APIを再作成せず中断した。
本番DBへのmigration、実環境での新承認API検証、Worker疎通の再検証は未実施。
Webサイトの公開・本番デプロイは行っていない。

## 完了した検証

- PostgreSQL 17を使用した統合テスト: 44件成功。
- 配布用Linux/amd64・Python 3.13イメージでも同じ44件成功。
- Gateway専用CI: [34749434439](https://github.com/craftzdev/homelab/actions/runs/34749434439) 成功。
- 既存validate CI [34749434425](https://github.com/craftzdev/homelab/actions/runs/34749434425) は確認時queued。成功扱いにしない。
- 本番DBバックアップをネットワーク非接続の一時PostgreSQLへ復元し、projects=1、jobs=57、approvals=1を確認。
- 復元コピーに新schemaを適用し、件数と既存CANCELLED承認を保持することを確認。
- 一時テストDB・復元DBコンテナは削除済み。暗号化バックアップは保持。

## バックアップ

Gatewayのroot専用ディレクトリ:

`/opt/ai-business-gateway-backups/pre-bound-approval-20260913/`

- `gateway.dump`: PostgreSQL custom形式。
- `source.tar.gz`: 更新前ソースと環境設定。機密情報を含むためroot専用。

VM外のage暗号化コピー:

`homelab/_out/gateway-bound-approval-20260913/{gateway.dump.age,source.tar.gz.age}`

復号後dumpのSHA-256:

`7b371119e00068d51f302c87530056b4851af12359515de45f2808fc88de7a05`

これはDB・ソースのバックアップであり、新しいPBS VMフルバックアップではない。

## ステージ済み内容と未反映内容

- `/opt/ai-business-gateway` のソース・composeはcommit `e7e8ae6` に更新済み。
- root専用 `.env` に `HUMAN_APPROVAL_ACTOR=craftz` を追加済み。
- **稼働コンテナは旧版のまま。上記設定はまだAPIへ反映されていない。**
- 旧publicイメージは `ai-business-gateway:pre-bound-approval-20260913` として保持。
- Mac上に `ai-business-gateway:approval-e7e8ae6` のLinux/amd64イメージを作成済み。
- Gateway上のbuildと、その後のイメージ取り込みは今回開始したプロセスだけを終了した。
  新イメージのGatewayへの読み込みは完了していない。部分的なキャッシュが残る可能性がある。

## 中断理由

Gateway VMのディスク容量・メモリ不足ではなく、I/O待ちとDocker管理処理の遅延を観測した。
Dockerヘルスチェックのexec開始タイムアウトも発生し、一時的にcallbackがunhealthyとなった。
中断時点ではpublic/callback双方のローカル `/ready` は応答し、Cloudflare Access経由の
外部 `/ready` もHTTP 200。コンテナやVMの再起動は行っていない。

同時刻の別調査では、Gatewayと同じProxmoxホストのlocal-zfs上でLonghorn replica rebuildに
伴う大きな書き込み待ちが報告されている。今回、replica・ストレージ設定・VM配置は変更していない。
基盤の負荷が解消し、管理操作とヘルスチェックが安定してから再開する。

## 再開手順

1. GatewayのI/O、Docker操作、両APIとDBのhealthが安定していることを確認する。
2. 中断後の変更を確認する。DBに新しい更新があればバックアップを再取得し、復元可能性を確認する。
3. `e7e8ae6`（またはレビュー済み後続commit）のイメージを取り込み、amd64と内容を検証する。
4. composeが使用するpublic/callback両イメージへ同じ検証済みイメージを設定する。
5. DBは停止せず、旧public/callback APIの両方を停止してから新APIを起動する。
   旧APIを並行稼働させて新しい承認検証を迂回できる状態にしない。
6. 起動時migration、API readiness、認証、操作者設定を確認する。
7. 隔離したダミー案件で対象hash不一致・digest不一致・期限切れ・二重利用・旧候補を拒否し、
   `approved_by=craftz` が記録されることを検証する。ダミー本番URLは `.invalid` とし、公開はしない。
8. Gateway→Worker→callbackの既存self-testを実行する。
9. 検証結果を記録する。Control Planeの新承認フォームは別途対応であり、この更新には含まれない。

失敗時に旧イメージだけへ戻すと承認制約を迂回するため、[承認設計](approvals.md)の
ロールバック制約に従い、承認・本番URL登録経路を閉じたうえで復旧する。
