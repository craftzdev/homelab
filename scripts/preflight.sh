#!/usr/bin/env bash
# ===========================================================================
# 構築前の前提チェック
#
# 「動かしてみて失敗する」より「事前に分かる問題を潰す」方が安全で速い。
# 特に Ceph の健全性は、Kubernetes を載せた後に問題が発覚すると
# 切り分けが極めて困難になるため、ここで確実に検出する。
#
# 使い方:
#   ./scripts/preflight.sh
#   ./scripts/preflight.sh --skip-ceph-health   # 承知の上で Ceph 警告を無視する
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PVE_HOST="${PVE_HOST:-172.16.10.11}"
PVE_SSH_USER="${PVE_SSH_USER:-root}"
RBD_POOL="${RBD_POOL:-cephrdb_k8s}"
ISO_DATASTORE="${ISO_DATASTORE:-cephfs01}"

SKIP_CEPH_HEALTH=false
FAILURES=0
WARNINGS=0

readonly C_RED=$'\033[0;31m' C_GREEN=$'\033[0;32m' C_YELLOW=$'\033[0;33m'
readonly C_BLUE=$'\033[0;34m' C_BOLD=$'\033[1m' C_RESET=$'\033[0m'

section() { printf '\n%s=== %s ===%s\n' "${C_BOLD}" "$*" "${C_RESET}"; }
pass()    { printf '  %s✓%s %s\n' "${C_GREEN}"  "${C_RESET}" "$*"; }
fail()    { printf '  %s✗%s %s\n' "${C_RED}"    "${C_RESET}" "$*"; FAILURES=$((FAILURES + 1)); }
warn()    { printf '  %s!%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*"; WARNINGS=$((WARNINGS + 1)); }
note()    { printf '    %s\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-ceph-health) SKIP_CEPH_HEALTH=true; shift ;;
    -h|--help)
      sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
      exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

pve() { ssh -o BatchMode=yes -o ConnectTimeout=10 "${PVE_SSH_USER}@${PVE_HOST}" "$@"; }

printf '%shomelab preflight check%s\n' "${C_BLUE}" "${C_RESET}"
printf 'Proxmox: %s@%s\n' "${PVE_SSH_USER}" "${PVE_HOST}"

# ===========================================================================
section "1. ローカルのツール"
# ===========================================================================
check_tool() {
  local cmd="$1" hint="$2" required="${3:-required}"
  if command -v "${cmd}" >/dev/null 2>&1; then
    pass "${cmd} … $(command -v "${cmd}")"
  elif [[ "${required}" == "required" ]]; then
    fail "${cmd} が見つかりません"
    note "${hint}"
  else
    warn "${cmd} が見つかりません（任意）"
    note "${hint}"
  fi
}

check_tool tofu     "brew install opentofu"
check_tool talosctl "brew install siderolabs/tap/talosctl"
check_tool kubectl  "brew install kubectl"
check_tool helm     "brew install helm"
check_tool sops     "brew install sops"
check_tool age      "brew install age"
check_tool jq       "brew install jq"
check_tool cilium   "brew install cilium-cli" optional
check_tool velero   "brew install velero" optional

# ===========================================================================
section "2. SOPS / age の設定"
# ===========================================================================
if [[ -f "${REPO_ROOT}/.sops.yaml" ]]; then
  if grep -q "REPLACE_WITH_YOUR_AGE_PUBLIC_KEY" "${REPO_ROOT}/.sops.yaml"; then
    fail ".sops.yaml に age 公開鍵が設定されていません"
    note "age-keygen -o ~/.config/sops/age/keys.txt を実行し、"
    note "出力された public key を .sops.yaml の age: に記入してください。"
  else
    pass ".sops.yaml に age 公開鍵が設定されています"
  fi
else
  fail ".sops.yaml が見つかりません"
fi

AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-${HOME}/.config/sops/age/keys.txt}"
if [[ -f "${AGE_KEY_FILE}" ]]; then
  pass "age 秘密鍵が存在します: ${AGE_KEY_FILE}"
  perm="$(stat -f '%Lp' "${AGE_KEY_FILE}" 2>/dev/null || stat -c '%a' "${AGE_KEY_FILE}" 2>/dev/null || echo "?")"
  if [[ "${perm}" != "600" && "${perm}" != "400" ]]; then
    warn "age 秘密鍵のパーミッションが ${perm} です（600 を推奨）"
    note "chmod 600 ${AGE_KEY_FILE}"
  fi
else
  fail "age 秘密鍵が見つかりません: ${AGE_KEY_FILE}"
  note "age-keygen -o ${AGE_KEY_FILE}"
  note "⚠️ この鍵を失うと、暗号化済みの全ての秘密が復号不能になります。"
  note "   パスワードマネージャ等にオフラインでバックアップしてください。"
fi

# ===========================================================================
section "3. Proxmox への接続"
# ===========================================================================
if pve 'true' 2>/dev/null; then
  pass "SSH 接続できます（${PVE_SSH_USER}@${PVE_HOST}）"
else
  fail "SSH 接続できません（${PVE_SSH_USER}@${PVE_HOST}）"
  note "鍵認証が設定されているか確認してください。"
  printf '\n%s致命的なエラーのため以降のチェックを中止します。%s\n' "${C_RED}" "${C_RESET}"
  exit 1
fi

PVE_VERSION="$(pve 'pveversion' 2>/dev/null | head -1 || echo unknown)"
pass "Proxmox VE: ${PVE_VERSION}"

if pve 'pvecm status >/dev/null 2>&1'; then
  QUORATE="$(pve "pvecm status 2>/dev/null | awk -F: '/Quorate/{gsub(/ /,\"\",\$2); print \$2}'" || echo "")"
  NODE_COUNT="$(pve "pvecm status 2>/dev/null | awk -F: '/^Nodes/{gsub(/ /,\"\",\$2); print \$2}'" || echo "?")"
  if [[ "${QUORATE}" == "Yes" ]]; then
    pass "クラスタは Quorate です（ノード数: ${NODE_COUNT}）"
  else
    fail "クラスタが Quorate ではありません"
    note "この状態で VM を作成すると予期しない動作をします。"
  fi
else
  warn "Proxmox クラスタが構成されていません（単一ノード構成）"
fi

# ===========================================================================
section "4. Ceph の健全性"
# ===========================================================================
if pve 'ceph -s >/dev/null 2>&1'; then
  CEPH_HEALTH="$(pve "ceph health detail 2>/dev/null" || echo "UNKNOWN")"
  CEPH_STATUS="$(printf '%s' "${CEPH_HEALTH}" | head -1)"

  case "${CEPH_STATUS}" in
    HEALTH_OK*)
      pass "Ceph は HEALTH_OK です"
      ;;
    HEALTH_WARN*)
      if [[ "${SKIP_CEPH_HEALTH}" == true ]]; then
        warn "Ceph が HEALTH_WARN ですが、--skip-ceph-health により続行します"
        printf '%s\n' "${CEPH_HEALTH}" | sed 's/^/      /'
      else
        fail "Ceph が HEALTH_WARN です"
        printf '%s\n' "${CEPH_HEALTH}" | sed 's/^/      /'
        note ""
        note "Kubernetes の PV はこの Ceph の上に載ります。ストレージ層の"
        note "不安定さは、そのままアプリケーションの不安定さになります。"
        note "構築前に原因を特定することを強く推奨します。"
        note "切り分けの手順: docs/30-storage-design.md §2"
        note ""
        note "承知の上で続行する場合は --skip-ceph-health を付けてください。"
      fi
      ;;
    HEALTH_ERR*)
      fail "Ceph が HEALTH_ERR です。構築を中止してください。"
      printf '%s\n' "${CEPH_HEALTH}" | sed 's/^/      /'
      ;;
    *)
      warn "Ceph の状態を判定できませんでした: ${CEPH_STATUS}"
      ;;
  esac

  # --- OSD の数 ---
  OSD_UP="$(pve "ceph osd stat --format json 2>/dev/null" | jq -r '.num_up_osds // 0' 2>/dev/null || echo 0)"
  OSD_IN="$(pve "ceph osd stat --format json 2>/dev/null" | jq -r '.num_in_osds // 0' 2>/dev/null || echo 0)"
  if [[ "${OSD_UP}" -ge 3 ]]; then
    pass "OSD: ${OSD_UP} up / ${OSD_IN} in"
  else
    fail "OSD が ${OSD_UP} 個しか up していません（replica 3 には最低 3 個必要）"
  fi

  # --- プールの存在 ---
  if pve "ceph osd pool ls 2>/dev/null | grep -qx '${RBD_POOL}'"; then
    pass "RBD プール '${RBD_POOL}' が存在します"

    POOL_USED="$(pve "ceph df --format json 2>/dev/null" \
      | jq -r --arg p "${RBD_POOL}" '.pools[] | select(.name==$p) | .stats.bytes_used' 2>/dev/null || echo 0)"
    POOL_AVAIL="$(pve "ceph df --format json 2>/dev/null" \
      | jq -r --arg p "${RBD_POOL}" '.pools[] | select(.name==$p) | .stats.max_avail' 2>/dev/null || echo 0)"

    if [[ "${POOL_USED}" -gt 0 && "${POOL_AVAIL}" -gt 0 ]]; then
      USED_GIB=$((POOL_USED / 1024 / 1024 / 1024))
      AVAIL_GIB=$((POOL_AVAIL / 1024 / 1024 / 1024))
      note "使用中: ${USED_GIB} GiB / 空き: ${AVAIL_GIB} GiB"

      # VM 6 台分（60×3 + 120×3 = 540 GiB、thin provision）に対する余裕
      if [[ "${AVAIL_GIB}" -lt 200 ]]; then
        warn "プールの空き容量が ${AVAIL_GIB} GiB です（VM 分 + PVC 分に不足する可能性）"
      fi
    fi
  else
    fail "RBD プール '${RBD_POOL}' が存在しません"
    note "Proxmox のダッシュボードから作成してください。"
  fi

  # --- 旧クラスタの残骸 ---
  ORPHAN_IMAGES="$(pve "rbd -p ${RBD_POOL} ls 2>/dev/null" || echo "")"
  if [[ -n "${ORPHAN_IMAGES}" ]]; then
    IMAGE_COUNT="$(printf '%s\n' "${ORPHAN_IMAGES}" | grep -c . || true)"
    warn "RBD プールに ${IMAGE_COUNT} 個のイメージが存在します"
    note "旧クラスタの残骸である可能性があります。内容を確認してください:"
    note "  ssh ${PVE_SSH_USER}@${PVE_HOST} rbd -p ${RBD_POOL} ls -l"
    note "⚠️ このスクリプトは自動削除を行いません（データ損失を避けるため）。"
  fi
else
  fail "Ceph が構成されていないか、コマンドを実行できません"
fi

# ===========================================================================
section "5. ストレージ設定"
# ===========================================================================
STORAGE_CFG="$(pve 'cat /etc/pve/storage.cfg' 2>/dev/null || echo "")"

if printf '%s' "${STORAGE_CFG}" | grep -q "^cephfs: ${ISO_DATASTORE}\$\|^cephfs: ${ISO_DATASTORE}[[:space:]]"; then
  ISO_CONTENT="$(printf '%s\n' "${STORAGE_CFG}" \
    | awk -v ds="${ISO_DATASTORE}" '$0 ~ "^[a-z]+: "ds"$"{f=1;next} /^[a-z]+: /{f=0} f && /content/{print}' || echo "")"
  if printf '%s' "${ISO_CONTENT}" | grep -q "iso"; then
    pass "ストレージ '${ISO_DATASTORE}' で iso content が有効です"
  else
    fail "ストレージ '${ISO_DATASTORE}' で iso content が有効になっていません"
    note "Talos の ISO を配置できません。以下で有効化してください:"
    note "  ssh ${PVE_SSH_USER}@${PVE_HOST} pvesm set ${ISO_DATASTORE} --content backup,vztmpl,iso"
  fi
else
  warn "ストレージ '${ISO_DATASTORE}' が見つかりません"
fi

# ===========================================================================
section "6. ネットワーク"
# ===========================================================================
# --- VLAN aware ブリッジ ---
if pve "grep -A5 'iface vmbr1' /etc/network/interfaces | grep -q 'bridge-vlan-aware yes'"; then
  pass "vmbr1 は VLAN aware です"

  VIDS="$(pve "grep -A6 'iface vmbr1' /etc/network/interfaces | awk '/bridge-vids/{\$1=\"\"; print}'" | xargs || echo "")"
  for vid in 20 40; do
    if printf '%s' "${VIDS}" | grep -qw "${vid}"; then
      pass "  VLAN ${vid} が bridge-vids に含まれています"
    else
      fail "  VLAN ${vid} が bridge-vids に含まれていません（現在: ${VIDS}）"
    fi
  done
else
  fail "vmbr1 が VLAN aware ではありません"
fi

# --- 運用端末から Kubernetes ノードのセグメントへ到達できるか ---
if ping -c 1 -W 2000 172.16.40.1 >/dev/null 2>&1; then
  pass "運用端末から VLAN40 のゲートウェイ (172.16.40.1) へ到達できます"
else
  fail "運用端末から 172.16.40.1 へ到達できません"
  note "Talos API / kube-apiserver へアクセスできず、構築が完了しません。"
  note "Tailscale のサブネットルートを確認してください。"
fi

# --- VLAN40 の IP が空いているか ---
section "7. IP アドレスの重複"
CONFLICT=0
for ip in 172.16.40.10 172.16.40.11 172.16.40.12 172.16.40.13 \
          172.16.40.21 172.16.40.22 172.16.40.23 172.16.40.200; do
  if ping -c 1 -W 800 "${ip}" >/dev/null 2>&1; then
    fail "${ip} に応答があります（既に使用中の可能性）"
    CONFLICT=$((CONFLICT + 1))
  fi
done
if [[ "${CONFLICT}" -eq 0 ]]; then
  pass "予定している IP アドレスは全て未使用です"
else
  note "旧クラスタの VM が稼働している場合は停止してください。"
fi

# ===========================================================================
section "8. 既存 VM との VMID 衝突"
# ===========================================================================
EXISTING_VMIDS="$(pve "qm list 2>/dev/null | awk 'NR>1{print \$1}'" || echo "")"
# クラスタ全体を見る
for node in sv-proxmox-01 sv-proxmox-02 sv-proxmox-03; do
  ids="$(ssh -o BatchMode=yes -o ConnectTimeout=5 "${PVE_SSH_USER}@${PVE_HOST}" \
    "pvesh get /nodes/${node}/qemu --output-format json 2>/dev/null" 2>/dev/null \
    | jq -r '.[].vmid' 2>/dev/null || echo "")"
  EXISTING_VMIDS="${EXISTING_VMIDS}"$'\n'"${ids}"
done

COLLISION=0
for vmid in 1001 1002 1003 1101 1102 1103; do
  if printf '%s\n' "${EXISTING_VMIDS}" | grep -qx "${vmid}"; then
    warn "VMID ${vmid} は既に使用されています"
    COLLISION=$((COLLISION + 1))
  fi
done
if [[ "${COLLISION}" -eq 0 ]]; then
  pass "使用予定の VMID は全て空いています"
else
  note "旧クラスタの VM です。削除するか、tofu の VMID を変更してください:"
  note "  ./scripts/destroy-legacy-vms.sh   （確認プロンプトあり）"
fi

# ===========================================================================
section "9. インターネット接続（イメージ取得用）"
# ===========================================================================
if pve 'curl -sf -o /dev/null --max-time 10 https://factory.talos.dev/'; then
  pass "Proxmox から factory.talos.dev へ到達できます"
else
  fail "Proxmox から factory.talos.dev へ到達できません"
  note "Talos の ISO をダウンロードできず、VM を作成できません。"
fi

# ===========================================================================
# 結果
# ===========================================================================
printf '\n%s========================================%s\n' "${C_BOLD}" "${C_RESET}"
if [[ "${FAILURES}" -eq 0 && "${WARNINGS}" -eq 0 ]]; then
  printf '%s全てのチェックに合格しました。%s\n' "${C_GREEN}" "${C_RESET}"
  printf '\n次の手順:\n'
  printf '  cd tofu/10-proxmox-talos\n'
  printf '  cp terraform.tfvars.example terraform.tfvars   # 編集する\n'
  printf '  tofu init && tofu apply\n\n'
  exit 0
elif [[ "${FAILURES}" -eq 0 ]]; then
  printf '%s警告 %d 件（致命的なエラーはありません）%s\n' "${C_YELLOW}" "${WARNINGS}" "${C_RESET}"
  printf '内容を確認した上で続行してください。\n\n'
  exit 0
else
  printf '%sエラー %d 件 / 警告 %d 件%s\n' "${C_RED}" "${FAILURES}" "${WARNINGS}" "${C_RESET}"
  printf '上記のエラーを解決してから構築を開始してください。\n\n'
  exit 1
fi
