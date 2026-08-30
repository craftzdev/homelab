#!/usr/bin/env bash
# ===========================================================================
# Ceph に Kubernetes 用の最小権限ユーザーを作成し、
# 認証情報を SOPS で暗号化して kubernetes/infra/ceph-csi/ に配置する。
#
# ---------------------------------------------------------------------------
# なぜ client.admin を使わないのか
# ---------------------------------------------------------------------------
# 多くのホームラボ向け記事は client.admin の keyring をそのまま Kubernetes の
# Secret にしている。しかしそれは、Kubernetes が侵害された瞬間に
#
#   - Proxmox VM のディスク（cephrdb_vm プール）を削除できる
#   - Ceph の設定を改竄できる
#   - 全プールを削除できる
#
# ことを意味する。本スクリプトは用途ごとに権限を絞ったユーザーを作り、
# 影響範囲を cephrdb_k8s プールと cephfs01:/volumes/csi 配下に限定する。
#
# 詳細: docs/30-storage-design.md §4
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# --- 設定（環境変数で上書き可能）-------------------------------------------
PVE_HOST="${PVE_HOST:-172.16.10.11}"
PVE_SSH_USER="${PVE_SSH_USER:-root}"
RBD_POOL="${RBD_POOL:-cephrdb_k8s}"
CEPHFS_NAME="${CEPHFS_NAME:-cephfs01}"
CEPHFS_SUBVOLUMEGROUP="${CEPHFS_SUBVOLUMEGROUP:-csi}"
RBD_USER="${RBD_USER:-k8s-rbd}"
CEPHFS_USER="${CEPHFS_USER:-k8s-cephfs}"
OUTPUT_FILE="${OUTPUT_FILE:-${REPO_ROOT}/kubernetes/infra/ceph-csi/secrets.sops.yaml}"

UPDATE_CAPS=false

# --- 色付きログ -------------------------------------------------------------
readonly C_RED=$'\033[0;31m' C_GREEN=$'\033[0;32m' C_YELLOW=$'\033[0;33m'
readonly C_BLUE=$'\033[0;34m' C_RESET=$'\033[0m'
info()  { printf '%s[INFO]%s  %s\n'  "${C_BLUE}"   "${C_RESET}" "$*"; }
ok()    { printf '%s[OK]%s    %s\n'  "${C_GREEN}"  "${C_RESET}" "$*"; }
warn()  { printf '%s[WARN]%s  %s\n'  "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die()   { printf '%s[ERROR]%s %s\n'  "${C_RED}"    "${C_RESET}" "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
使い方: ceph-create-k8s-user.sh [オプション]

  --update-caps   既存ユーザーの権限（caps）が期待値と異なる場合に上書きする。
                  既定では警告して終了する（意図しない権限変更を防ぐため）。
  -h, --help      このヘルプを表示する

環境変数:
  PVE_HOST                 Ceph コマンドを実行する Proxmox ホスト (既定: 172.16.10.11)
  PVE_SSH_USER             SSH ユーザー                            (既定: root)
  RBD_POOL                 Kubernetes 用 RBD プール                (既定: cephrdb_k8s)
  CEPHFS_NAME              CephFS 名                               (既定: cephfs01)
  CEPHFS_SUBVOLUMEGROUP    CephFS のサブボリュームグループ         (既定: csi)
  OUTPUT_FILE              出力先の SOPS 暗号化ファイル
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --update-caps) UPDATE_CAPS=true; shift ;;
    -h|--help)     usage; exit 0 ;;
    *)             die "不明な引数: $1（--help を参照）" ;;
  esac
done

# ---------------------------------------------------------------------------
# 前提チェック
# ---------------------------------------------------------------------------
command -v sops >/dev/null 2>&1 || die "sops が見つかりません。'brew install sops' でインストールしてください。"
command -v ssh  >/dev/null 2>&1 || die "ssh が見つかりません。"

[[ -f "${REPO_ROOT}/.sops.yaml" ]] || die ".sops.yaml が見つかりません: ${REPO_ROOT}/.sops.yaml"

if grep -q "REPLACE_WITH_YOUR_AGE_PUBLIC_KEY" "${REPO_ROOT}/.sops.yaml"; then
  die ".sops.yaml に age 公開鍵が設定されていません。
     1) age-keygen -o ~/.config/sops/age/keys.txt
     2) 出力された public key を .sops.yaml の age: に記入してください。"
fi

# SSH は BatchMode で実行する（パスワードプロンプトで固まらないようにする）
pve() { ssh -o BatchMode=yes -o ConnectTimeout=10 "${PVE_SSH_USER}@${PVE_HOST}" "$@"; }

info "Proxmox (${PVE_HOST}) への接続を確認しています..."
pve 'ceph -s >/dev/null' \
  || die "Proxmox への SSH または ceph コマンドの実行に失敗しました。鍵認証の設定を確認してください。"
ok "接続を確認しました"

# ---------------------------------------------------------------------------
# クラスタの基本情報
# ---------------------------------------------------------------------------
CEPH_FSID="$(pve 'ceph fsid' | tr -d '\r\n')"
[[ -n "${CEPH_FSID}" ]] || die "Ceph の fsid を取得できませんでした。"
info "Ceph fsid: ${CEPH_FSID}"

# StorageClass / values に埋め込んだ clusterID と一致しているか検証する。
# ここがずれていると PVC が Pending のまま止まり、原因が分かりにくい。
EXPECTED_FSID="$(grep -m1 -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' \
  "${REPO_ROOT}/kubernetes/infra/ceph-csi/storageclass.yaml" || true)"
if [[ -n "${EXPECTED_FSID}" && "${EXPECTED_FSID}" != "${CEPH_FSID}" ]]; then
  die "storageclass.yaml の clusterID (${EXPECTED_FSID}) が実際の Ceph fsid (${CEPH_FSID}) と一致しません。
     kubernetes/infra/ceph-csi/ 配下の clusterID を ${CEPH_FSID} に修正してください。"
fi

# 対象プールの存在確認
pve "ceph osd pool ls | grep -qx '${RBD_POOL}'" \
  || die "RBD プール '${RBD_POOL}' が存在しません。Proxmox 側で作成してください。"
ok "RBD プール '${RBD_POOL}' を確認しました"

# ---------------------------------------------------------------------------
# CephFS のサブボリュームグループ
#
# Proxmox が使っている cephfs01 を Kubernetes と共用するため、
# 専用のサブボリュームグループを切って権限をその配下に限定する。
# ---------------------------------------------------------------------------
if pve "ceph fs ls --format json | grep -q '\"name\":\"${CEPHFS_NAME}\"'"; then
  info "CephFS '${CEPHFS_NAME}' にサブボリュームグループ '${CEPHFS_SUBVOLUMEGROUP}' を作成します（冪等）"
  pve "ceph fs subvolumegroup create ${CEPHFS_NAME} ${CEPHFS_SUBVOLUMEGROUP}"
  ok "サブボリュームグループを確認しました"
  CEPHFS_AVAILABLE=true
else
  warn "CephFS '${CEPHFS_NAME}' が見つかりません。CephFS 用ユーザーの作成をスキップします。"
  CEPHFS_AVAILABLE=false
fi

# ---------------------------------------------------------------------------
# ユーザー作成のヘルパー
#
# 既存ユーザーの caps が期待値と異なる場合、既定では**上書きしない**。
# 権限の変更は影響が大きく、意図せず実行されるべきではないため。
# ---------------------------------------------------------------------------
ensure_ceph_user() {
  local user="$1"; shift
  local -a caps=("$@")

  local caps_str=""
  local i
  for ((i = 0; i < ${#caps[@]}; i += 2)); do
    caps_str+="${caps[i]} '${caps[i+1]}' "
  done

  if pve "ceph auth get client.${user} >/dev/null 2>&1"; then
    info "client.${user} は既に存在します。権限を検証します..."

    local current
    current="$(pve "ceph auth get client.${user} --format json")"

    local mismatch=false
    for ((i = 0; i < ${#caps[@]}; i += 2)); do
      local key="${caps[i]}" want="${caps[i+1]}"
      # JSON から該当 caps を素朴に抽出して比較する
      if ! printf '%s' "${current}" | grep -qF "\"${key}\":\"${want}\""; then
        warn "  caps ${key} が期待値と異なります"
        warn "    期待: ${want}"
        mismatch=true
      fi
    done

    if [[ "${mismatch}" == true ]]; then
      if [[ "${UPDATE_CAPS}" == true ]]; then
        warn "--update-caps が指定されたため権限を更新します: client.${user}"
        pve "ceph auth caps client.${user} ${caps_str}"
        ok "client.${user} の権限を更新しました"
      else
        die "client.${user} の権限が期待値と異なります。
     現在の権限を確認してください:
       ssh ${PVE_SSH_USER}@${PVE_HOST} ceph auth get client.${user}
     意図的に上書きする場合は --update-caps を付けて再実行してください。"
      fi
    else
      ok "client.${user} の権限は期待通りです"
    fi
  else
    info "client.${user} を作成します"
    pve "ceph auth get-or-create client.${user} ${caps_str} >/dev/null"
    ok "client.${user} を作成しました"
  fi
}

get_ceph_key() {
  local user="$1"
  pve "ceph auth get-key client.${user}" | tr -d '\r\n'
}

# ---------------------------------------------------------------------------
# RBD 用ユーザー
#
# profile rbd は「RBD の操作に必要な最小限」を Ceph が定義したもの。
# pool= を付けることで cephrdb_k8s 以外には一切触れなくなる。
# ---------------------------------------------------------------------------
info "=== RBD 用ユーザー ==="
ensure_ceph_user "${RBD_USER}" \
  mon "profile rbd" \
  osd "profile rbd pool=${RBD_POOL}" \
  mgr "profile rbd pool=${RBD_POOL}"

RBD_KEY="$(get_ceph_key "${RBD_USER}")"
[[ -n "${RBD_KEY}" ]] || die "client.${RBD_USER} のキーを取得できませんでした。"

# ---------------------------------------------------------------------------
# CephFS 用ユーザー
#
# path= により /volumes/csi 配下のみに書き込みを限定する。
# Proxmox が使っている ISO / バックアップ領域には到達できない。
# ---------------------------------------------------------------------------
CEPHFS_KEY=""
if [[ "${CEPHFS_AVAILABLE}" == true ]]; then
  info "=== CephFS 用ユーザー ==="
  ensure_ceph_user "${CEPHFS_USER}" \
    mon "allow r fsname=${CEPHFS_NAME}" \
    mds "allow rw fsname=${CEPHFS_NAME} path=/volumes/${CEPHFS_SUBVOLUMEGROUP}" \
    osd "allow rw tag cephfs data=${CEPHFS_NAME}" \
    mgr "allow rw"

  CEPHFS_KEY="$(get_ceph_key "${CEPHFS_USER}")"
  [[ -n "${CEPHFS_KEY}" ]] || die "client.${CEPHFS_USER} のキーを取得できませんでした。"
fi

# ---------------------------------------------------------------------------
# Secret マニフェストの生成と暗号化
#
# 平文が一時ファイルとしてディスクに残る時間を最小化するため、
# mktemp で作成 → 即座に sops で暗号化 → 元ファイルを削除する。
# trap により、途中で失敗しても平文が残らないようにする。
# ---------------------------------------------------------------------------
info "=== Secret を生成して暗号化します ==="

TMP_FILE="$(mktemp "${TMPDIR:-/tmp}/ceph-csi-secrets.XXXXXX.yaml")"
chmod 600 "${TMP_FILE}"
cleanup() { rm -f "${TMP_FILE}"; }
trap cleanup EXIT INT TERM

{
  cat <<EOF
# このファイルは scripts/ceph-create-k8s-user.sh によって生成されました。
# 手で編集する場合は 'sops kubernetes/infra/ceph-csi/secrets.sops.yaml' を使ってください。
---
apiVersion: v1
kind: Secret
metadata:
  name: csi-rbd-secret
  namespace: ceph-csi
type: Opaque
stringData:
  userID: ${RBD_USER}
  userKey: ${RBD_KEY}
EOF

  if [[ -n "${CEPHFS_KEY}" ]]; then
    cat <<EOF
---
apiVersion: v1
kind: Secret
metadata:
  name: csi-cephfs-secret
  namespace: ceph-csi
type: Opaque
stringData:
  userID: ${CEPHFS_USER}
  userKey: ${CEPHFS_KEY}
  adminID: ${CEPHFS_USER}
  adminKey: ${CEPHFS_KEY}
EOF
  fi
} > "${TMP_FILE}"

mkdir -p "$(dirname "${OUTPUT_FILE}")"
sops --encrypt --config "${REPO_ROOT}/.sops.yaml" "${TMP_FILE}" > "${OUTPUT_FILE}"
chmod 600 "${OUTPUT_FILE}"

ok "暗号化された Secret を書き出しました: ${OUTPUT_FILE}"

# 平文が混入していないことを検証する（保険）
if grep -qF "${RBD_KEY}" "${OUTPUT_FILE}"; then
  rm -f "${OUTPUT_FILE}"
  die "暗号化に失敗しています（平文のキーが出力に含まれています）。出力ファイルを削除しました。"
fi
ok "平文のキーが含まれていないことを確認しました"

cat <<EOF

┌──────────────────────────────────────────────────────────────────┐
│ Ceph 側の準備が完了しました。                                     │
└──────────────────────────────────────────────────────────────────┘

  作成したユーザー:
    client.${RBD_USER}
      mon 'profile rbd'
      osd 'profile rbd pool=${RBD_POOL}'
      mgr 'profile rbd pool=${RBD_POOL}'
EOF

if [[ -n "${CEPHFS_KEY}" ]]; then
  cat <<EOF
    client.${CEPHFS_USER}
      mon 'allow r fsname=${CEPHFS_NAME}'
      mds 'allow rw fsname=${CEPHFS_NAME} path=/volumes/${CEPHFS_SUBVOLUMEGROUP}'
      osd 'allow rw tag cephfs data=${CEPHFS_NAME}'
      mgr 'allow rw'
EOF
fi

cat <<EOF

  次の手順:
    git add ${OUTPUT_FILE#"${REPO_ROOT}/"}
    git commit -m "feat(ceph-csi): Ceph の認証情報を追加（SOPS 暗号化済み）"

EOF
