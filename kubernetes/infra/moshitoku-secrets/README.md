# moshitoku / moshitoku-scraper の外部発行シークレット

## なぜ homelab が持つのか

アプリケーションのマニフェストは各リポジトリ（`craftzdev/moshitoku`、
`craftzdev/moshitoku-scraper`）が所有し、自分の namespace のワークロードだけを
定義する。資格情報はクラスタ側の責任であり、アプリ側リポジトリに秘密を置かない。
その分界点を維持したまま、外部発行の値だけをここで持つ。

## なぜ SOPS で、他は Keychain なのか

値の性質で分けている。理由は暗号強度ではなく**配送**である。

| 値 | 性質 | 置き場 |
| --- | --- | --- |
| `webshare-api-key` | 外部サービスが発行。生成できない | **ここ（SOPS）** |
| `discord-webhook-url` | 同上 | **ここ（SOPS）** |
| DB パスワード / Django secret / MinIO secret | 自前生成。値自体に意味は無く再構築で同一なら足る | `scripts/bootstrap-moshitoku-secrets.sh`（Keychain） |
| `moshitoku-postgres-ca` / scraper 側 db-owner | CloudNativePG が実行時に作る値の複製 | 同スクリプト（SOPS では表現できない） |

SOPS 側は Argo CD が desired state として配送し、selfHeal で復旧する。人が
スクリプトを実行する手順が1つ減る。忘れると CronJob が起動直後に落ちるため、
自動配送される意味が大きい。

自前生成の値をここへ入れない理由は、生成物を Git へコミットすることになり、
ローテーションのたびに履歴が汚れるためである。Keychain に置けば再構築で同じ値が
復元され、Git には現れない。

⚠️ SOPS にしても Keychain 依存は消えない。age 秘密鍵自体が Keychain の
`dev.craftz.homelab.sops-age-key` にある。変わるのは「守るべき秘密が N 個から
1 個になる」ことであり、Keychain からの脱却ではない。

## 編集

```bash
sops kubernetes/infra/moshitoku-secrets/scraper-runtime.sops.yaml
```

`.sops.yaml` の `encrypted_regex` により `stringData` だけが暗号化され、
`apiVersion` / `kind` / `metadata` は平文で残る。Argo CD と kustomize が
リソースとして解釈できる必要があるためである。

## namespace は所有しない

`moshitoku-scraper` namespace はアプリ側リポジトリが Pod Security ラベル付きで
作る。ここで作ると2つの Application が同じ Namespace を取り合って drift する。
Application は `CreateNamespace=false` で、namespace ができた後に同期する
（sync-wave はアプリ本体より後）。
