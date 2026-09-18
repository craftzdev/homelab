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
資格情報を署名取得先のホスト名（`172.16.40.201:5000`）向けに置き換えたもの。
資格情報はレジストリのホスト名で選ばれるため、ここが ClusterPolicy の
参照先と一致していないと匿名アクセスになり署名を読めない。

- `harbor-pull-ai-business`（`ai-agent/harbor-pull` 由来）
- `harbor-pull-moshitoku`（`moshitoku/harbor-pull` 由来）

## 前提となる CA 束

Kyverno は署名を Harbor から HTTPS で取る。自前 CA を知らないため、
「公開 CA ＋ Harbor の CA」を束ねた ConfigMap を渡している。

```bash
scripts/reconcile-kyverno-ca-bundle.sh
```

`caCertificates` はコンテナの `ca-certificates.crt` を丸ごと置き換えるので、
自前 CA だけを入れると Sigstore への TLS が壊れる。公開 CA は管理用 Mac の
システム束（`/etc/ssl/cert.pem`）から取っている。

`allowInsecureRegistry` は使わない。あれは TLS 検証の省略ではなく平文 HTTP への
切り替えで、Harbor は HTTPS しか受けないため 400 になる。

## 署名の形式（cosign 2 系）

Kyverno 1.19 が読めるのは `sha256-<digest>.sig` タグに付いた署名まで。
cosign 3 の既定（OCI 1.1 の referrer + sigstore bundle）は読めず、
検証が `no signatures found` になる。アプリ側の CI は cosign 2 系に固定してある。

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
