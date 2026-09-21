# Configuration deployment controller

Control Plane は表示と管理 API 呼び出しを担当し、Gateway が下書き・承認・履歴を保持する。
Controller は GitHub、隔離した実行試験、配置確認を扱う。UI に GitHub / Kubernetes の資格情報を渡さない。

## Flow

1. 設定を編集・保存し、基本検証する。
2. 保存版から配布候補を作る。**検証後に自動配布**は人間が候補ごとに選ぶ。
   既定では検証後にもう一度適用を判断する。後から同じ候補の承認方式を変更できない。
3. Controller が対象 Git ファイルと編集元を照合し、固定 head の PR を作成する。
4. `config-check` が合格してから、一時 Kubernetes Job で候補設定を実際の Codex に渡す。
   配置中の Agent の読み込み版、Git base、試験入力のハッシュを照合する。
5. 実行試験合格後、自動配布が承認されている候補は Gateway が PROMOTE_REQUESTED に進める。
   未承認の候補は VERIFIED で人間の判断を待つ。Controller 自身は承認できない。
6. Controller が head/base/CI/試験対象を再確認して merge する。
7. profiles / skills / schemas は main の CI がビルド・署名し、image digest と
   `ai-business/source-revision` を Git に記録する。Argo CD が配置する。
   共通ハーネスは ConfigMap と Pod template の release annotation を同時に変更する。
8. 対象の全 Deployment の image、承認版の annotation、observedGeneration、
   更新済み・稼働中 replica 数を確認して DEPLOYED にする。

状態: `QUEUED → REVIEW → VERIFIED → PROMOTE_REQUESTED → MERGED → DEPLOYING → DEPLOYED`。
自動配布は VERIFIED を経由して直ちに PROMOTE_REQUESTED へ進み、両イベントを残す。
検証や競合で BLOCKED、CI/配置失敗や期限超過で DEPLOYMENT_FAILED。
要確認の配布は管理 API の `/recheck` で再確認できる（CI 再実行そのものは行わない）。
戻す操作は変更前の内容を持つ新しい下書きで、同じ検証を通す。

## Runtime trial boundary

試験は operator が digest 固定した既存 Agent / Worker image を使用する。
候補 PR の Python・Dockerfile・workflow を試験用資格情報で実行しない。
対象設定をデータとして ConfigMap に固定し、Agent image の既存 registry で読み込む。
Worker image の既存 executor が、影響する profile ごとに本物の Codex を起動する。
Codex は資格情報を持たず、別 Pod の固定接続先 Responses broker を使う。
broker はこの smoke では tools を無効化し、保存を無効化する。

これは **実モデル接続と設定供給の smoke test** であり、業務成果の品質評価や
すべてのツール・付属スクリプトの動作保証ではない。チェック対象は profile、schema、
capabilities、SKILL.md、共通ハーネス。付属スクリプト等の変更は試験未対応として止める。
本番の action の入力/出力契約は config-check と main の通常 CI でも検証する。

Job は本番 PVC、Gateway token、GitHub token、ServiceAccount token を持たない。
専用 namespace、read-only rootfs、非 root、resource quota、30分 deadline を使用する。
試験 Pod は Secret を一切 mount せず、直接のインターネット・DNS 通信も許可しない。
ClusterIP の broker だけに通信する。broker は別 Pod / UID で、試験との共有 volume はなく、
資格情報を専用 Secret から読む。broker の通信先は Cilium policy でも api.openai.com / chatgpt.com の443だけに限定する。
HTTP relay は固定 Responses endpoint のみ、redirect を拒否し、上流の error 本文や認証 header を返さない。
リクエストは1MB、応答は2MB、90秒、単一同時接続、30分あたり128件まで。
ログにモデル出力・認証情報は残さず、合否とハッシュを Gateway に保存する。
合格済みの同じ候補を繰り返しモデル実行しない。読み込み元の設定や試験 runtime が
変われば再利用を拒否する。完了 Job と入力 ConfigMap は7日後に Kubernetes が回収する。

試験用資格情報を持たない状態を合格にしたり、以前の本番 smoke を候補の証跡に転用しない。

## Production installation

Gateway の `automatic_promotion` / `deployment_observation` API、Control Plane の UI、
Agent / Worker の source-revision 付き image promotion を先に配置する。
Controller は Gateway VM 上の独立した systemd service として稼働する。
試験 namespace には Cilium の FQDN / DNS policy が必要。導入時に broker の Service IP を
trial-settings.json に記録し、broker の準備完了を確認してから Controller を起動する。
`deploy/install.py` は一回の導入で RBAC / trial namespace / secret / service を設定する。
VM に Python 3 と venv、呼び出し元に kubectl / SSH とこの requirements が必要。

GitHub App は `github-app-manifest.json` で作成し、対象を
`craftzdev/ai-business-agent` / `craftzdev/ai-business-worker` だけに限定する。
Contents/Pull requests write、Checks/Actions read。Runner 管理 App と分離する。
[Installation tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
は Controller がメモリー内で更新し、発行時にも対象 repo と権限を制限する。

```sh
python -m pip install -r requirements.txt
python deploy/install.py --kubeconfig /path/to/kubeconfig \
  --host craftz@172.16.40.30 \
  --github-app-id APP_ID --github-installation-id INSTALLATION_ID \
  --github-app-key /path/to/private-key.pem \
  --trial-auth-file /path/to/trial-auth.json --check
# --check を外して導入する。
# 利用者が明示的に選んだ場合のみ --trial-auth-file の代わりに --reuse-worker-auth を使う。
```

App ID・installation ID・鍵・試験用認証が必須。
試験用 auth.json は専用 API key を推奨する。明示的に既存 Codex 認証を再利用する場合も
Worker の現在の書き込み可能な認証キャッシュを読み、access token と account ID だけを broker に配置する。
初期配置用の Secret は更新後に失効するため再利用元にしない。refresh token / ID token はコピーしない。ChatGPT token の期限切れは試験失敗となり、
資格情報を更新するまで配布を進めない。この broker は OAuth refresh token を使用しない。利用者の gh token を常駐用にコピーしない。
Secret は stdin で転送し、argv や Git に入れない。VM の `/etc/ai-config-controller` は
root 管理・service group 読み取りだけ。Kubernetes は専用 ServiceAccount の token を使用し、
管理者 kubeconfig を転送しない。この VM 用 token は長期資格情報なので、VM の廃止時は
`ai-config-trial/config-controller-api` Secret を削除して失効させる。

`targets.example.json` と `trial-settings.example.json` は operator が管理する。
本番 Deployment は読み取りのみ。Controller の書き込み権限は試験 namespace 内の
Job / ConfigMap に限定する。Git の更新と Argo CD による配置の分離を維持する。
一つの Controller service で稼働させる。Git の base が競合した候補は新しい版から作り直す。

## Validation

```sh
python -m pytest -q
```

Gateway tests は自動承認の権限・固定版・失敗時停止・配布証跡を DB まで確認する。
Controller tests は token 更新、候補との証跡の結び付け、試験失敗、再実行抑止、
Pod の隔離、複数配置先の確認、古い版・CI 失敗・タイムアウトを扱う。

Codex のカスタム provider は [公式の認証設定](https://developers.openai.com/codex/auth) に従う。
検証: Controller 27 tests passed。Codex 0.153.4 とローカルの模擬 SSE 接続で、
認証ファイル・Authorization header なしに最終応答を受け取れることを確認した。
実プロバイダーへの接続と本番の候補配布は専用認証の設定後に検証する。

Broker readiness は Pod 内の exec probe で確認する。kubelet / node からの新規 ingress を許可せず、試験 Pod からの接続だけを維持する。
