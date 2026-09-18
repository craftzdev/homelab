# Kyverno — イメージ署名の検証

アプリのイメージに、アプリ側 CI（GitHub Actions）の cosign キーレス署名が
付いていることを admission で検証する。

- 対象: `172.16.40.201:5000` の `ai-business/*` と `moshitoku/*`
- 判定: **Audit（記録のみ）**。署名が無くても Pod は作成される
- 署名を作っている側: 各アプリリポジトリの `container.yaml` / `container.yml`

## いまの状態を見る

```bash
kubectl get clusterpolicy verify-harbor-images
kubectl get policyreport -A
kubectl get policyreport -A -o json \
  | jq -r '.items[].results[] | select(.result=="fail") | "\(.policy)/\(.rule) \(.resources[0].namespace)/\(.resources[0].name) \(.message)"'
```

## 前提となる Secret

署名は Harbor の private プロジェクトにあるため、読み取り資格情報が要る。
既存の読み取り専用ロボットから複製する。

```bash
scripts/reconcile-kyverno-registry-credentials.sh
```

作られるのは `kyverno` namespace の次の 2 つで、いずれも既存ロボットの
資格情報をクラスタ内ホスト名（`harbor.harbor.svc`）向けに置き換えたもの。

- `harbor-pull-ai-business`（`ai-agent/harbor-pull` 由来）
- `harbor-pull-moshitoku`（`moshitoku/harbor-pull` 由来）

## なぜ `allowInsecureRegistry` を使うのか

署名はイメージと同じ Harbor（`172.16.40.201:5000`）から取る。その証明書は
自前 CA で、Kyverno のコンテナはそれを知らない。CA を渡す仕組み
（`global.caCertificates`）はコンテナの `ca-certificates.crt` を丸ごと
置き換えるため、自前 CA だけを入れると Sigstore（Fulcio / Rekor / TUF）への
TLS が検証できなくなる。

そこで `allowInsecureRegistry` をこのルールにだけ与えている。宛先は
LoadBalancer IP だが、Pod から出た通信は Cilium がノード内の
`harbor-ci-proxy` へ直接渡すため LAN には出ない（Hubble でも
`kyverno/... -> arc-runners/harbor-ci-proxy-...:5000` として見える）。
署名の検証は署名そのものに対する暗号的な検証であり、取得経路の TLS には
依存しない。

なお keyless の attestor には Rekor の URL が必須で、無いと Kyverno の
webhook がポリシーを拒否する。

## Enforce への切り替え手順

1. `policyreport` に fail が出ていないことを確認する。
2. 残っていれば、そのアプリを一度ビルドし直して署名付きイメージへ入れ替える。
   署名導入（2026-09-18）より前のダイジェストで止まっているものが対象。
3. `policies/verify-harbor-images.yaml` の `failureAction` を
   `Audit` から `Enforce` へ変える。

Enforce にすると、署名の無いイメージの Pod は作成できなくなる。CI が
止まっている間に手で Pod を作る、といった運用ができなくなる点に注意する。

## 壊れたときに困らないための設定

- `failurePolicy: Ignore`: Kyverno が落ちている間は検証せずに通す。
  Pod が作れなくなってクラスタが復旧できない、という状態を避ける。
- webhook の対象から `kube-system` と `kyverno` を除外している。
  Kyverno 自身や CNI の再起動が Kyverno に依存しないようにするため。
