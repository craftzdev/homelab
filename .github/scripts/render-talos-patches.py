#!/usr/bin/env python3
"""
Talos の machine config パッチ（*.tftpl）をダミー値でレンダリングする。

CI で `talosctl gen config` → `validate --strict` を実行するために使う。
OpenTofu を動かさずにテンプレートを展開できるようにするのが目的。

⚠️ ここで使う値は「構文が検証できればよい」ためのダミーである。
   実際の値は tofu/10-proxmox-talos/variables.tf で定義される。
   両者がずれても CI は気づけないため、変数を増やしたら
   このスクリプトにも追加すること（不足すると未置換で検出される）。
"""
import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]

# パッチが参照する変数のダミー値
COMMON = {
    "installer_image": "factory.talos.dev/nocloud-installer/0000:v1.13.9",
    "nameservers": json.dumps(["172.16.40.1", "1.1.1.1"]),
    "ntp_servers": json.dumps(["ntp.nict.jp", "time.cloudflare.com"]),
    "cert_sans": json.dumps(["172.16.40.10", "127.0.0.1",
                             "172.16.40.11", "172.16.40.12", "172.16.40.13"]),
    "gateway": "172.16.40.1",
    "vip": "172.16.40.10",
    "k8s_subnet": "172.16.40.0/24",
    "pod_cidr": "10.244.0.0/16",
    "service_cidr": "10.96.0.0/12",
}
CP = {**COMMON, "hostname": "k8s-1", "mac_k8s": "bc:24:11:40:00:11", "ip": "172.16.40.11"}
WK = {**COMMON, "hostname": "k8s-w1", "mac_k8s": "bc:24:11:40:00:21", "ip": "172.16.40.21"}

MGMT_CIDRS = ["172.16.40.0/24", "172.16.10.0/24", "100.64.0.0/10"]
FW = {
    **COMMON,
    "management_ingress": "\n".join(f"  - subnet: {c}" for c in MGMT_CIDRS),
    "management_cidrs_desc": ", ".join(MGMT_CIDRS),
}


def render(template: pathlib.Path, values: dict) -> str:
    text = template.read_text()
    for key, value in values.items():
        text = text.replace("${" + key + "}", str(value))
    return text


def main() -> int:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/talos")
    out.mkdir(parents=True, exist_ok=True)

    targets = [
        ("cp.yaml", "talos/patches/controlplane.yaml.tftpl", CP),
        ("wk.yaml", "talos/patches/worker.yaml.tftpl", WK),
        ("fw.yaml", "talos/patches/ingress-firewall.yaml.tftpl", FW),
    ]

    failed = False
    for name, path, values in targets:
        rendered = render(REPO / path, values)
        (out / name).write_text(rendered)

        # 置換漏れがあれば、変数名の変更にスクリプトが追随できていない証拠。
        # 黙って通すと talosctl が意味不明なエラーを返すので、ここで落とす。
        leftover = sorted(set(re.findall(r"\$\{[a-z_]+\}", rendered)))
        if leftover:
            print(f"::error file={path}::未置換の変数があります: {leftover}")
            print("        .github/scripts/render-talos-patches.py に値を追加してください")
            failed = True
        else:
            print(f"rendered: {path} -> {out / name}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
