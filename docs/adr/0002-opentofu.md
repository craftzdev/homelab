# ADR-0002: プロビジョニングに OpenTofu + bpg/proxmox + siderolabs/talos を使う

- **状態**: 承認済み
- **日付**: 2026-08-30

## 背景

既存リポジトリでは `deploy-vm.sh`（約 200 行の Bash）が Proxmox 上に VM を作り、
cloud-init で `k8s-node-setup.sh` を実行させていた。要件は「構築は常にコードで
実装してリポジトリに管理」であり、この形も広義には「コード」だが、
以下の問題を抱えていた。

- 現在の状態を把握する手段が無い（`qm status` を叩いて分岐する手続き的コード）
- 冪等性を手作業で担保している（`if qm status ... then skip`）
- 変更を適用する前に「何が変わるか」を確認できない
- 削除の手順が存在しない（作りっぱなし）

## 検討した選択肢

| 選択肢 | 評価 |
| --- | --- |
| Bash + `qm` コマンド（現状） | 手続き的。plan/destroy が無い。冪等性が自作 |
| Ansible（`community.general.proxmox`） | 宣言的に見えるが実体は手続き。状態管理が無く、削除が苦手 |
| Terraform（HashiCorp） | 実績十分。ただし BUSL ライセンス。ホームラボでは問題ないが、将来的な選択肢を狭める |
| **OpenTofu** ★採用 | Terraform 互換で MPL-2.0。Linux Foundation 傘下。プロバイダエコシステムはそのまま使える |

## 決定

**OpenTofu** を採用し、以下のプロバイダを使う。

| プロバイダ | バージョン | 役割 |
| --- | --- | --- |
| `bpg/proxmox` | `~> 0.111` | Proxmox VE API 経由での VM 作成・ISO ダウンロード |
| `siderolabs/talos` | `~> 0.11` | machine config 生成、bootstrap、kubeconfig / talosconfig 取得、Image Factory schematic |
| `cloudflare/cloudflare` | `~> 5.24` | Tunnel / Access / DNS |

### ステートを 2 つに分ける理由

```
tofu/10-proxmox-talos/   ← Proxmox VM と Talos クラスタ
tofu/20-cloudflare/      ← Cloudflare Zero Trust
```

- **障害の影響範囲を分ける**: Cloudflare API の障害や認証情報の失効が、
  クラスタ側の `plan` を妨げない
- **権限を分ける**: Cloudflare API トークンを持たない人でもクラスタ側を扱える
- **ライフサイクルが違う**: クラスタは滅多に変えないが、公開サービスは増減する

### ステートの保存先

初期はローカルステート（`terraform.tfstate`）とし、`.gitignore` で除外する。
**ステートファイルには Talos のシークレット（CA 秘密鍵など）が平文で含まれる**ため、
これは重要な注意点である。運用者は次のいずれかを選ぶ。

1. ローカル + ディスク暗号化された端末（初期の既定）
2. リモートバックエンド（S3 互換 + SSE + バージョニング）へ移行

`docs/50-operations.md` に移行手順を記載する。

## なぜ「Image Factory も IaC に含める」のか

Talos のカスタムイメージ（qemu-guest-agent 拡張入り）は、
ブラウザで factory.talos.dev を操作して schematic ID を取得することもできる。
しかしそれでは「どの拡張が入ったイメージなのか」がコードに残らない。

`talos_image_factory_schematic` リソースを使うことで、

```hcl
resource "talos_image_factory_schematic" "this" {
  schematic = yamlencode({
    customization = {
      systemExtensions = { officialExtensions = ["siderolabs/qemu-guest-agent"] }
    }
  })
}
```

と書ける。**イメージの中身がコードとして表現され、レビュー可能になる。**
