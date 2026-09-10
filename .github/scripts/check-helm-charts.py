#!/usr/bin/env python3
"""
kubernetes/apps/*.yaml から Helm chart の定義を抽出し、
実際に取得して `helm template` でレンダリングできるか検証する。

これが検出するもの:
  - 存在しない Helm リポジトリ（404）
  - 存在しない chart バージョン
  - values のキー名が chart のスキーマに合わない（render 時にエラーになる場合）

⚠️ 「存在しない Helm リポジトリを参照している」という不具合が実際にあった
   （kubelet-serving-cert-approver は Helm chart を提供しておらず、
    helm repo add が 404 になっていた）。静的解析では検出できない。
"""
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]


def extract_charts():
    """ArgoCD Application から (repoURL, chart, version, valuesPath) を抽出する。

    yaml でパースすると multi-doc とコメントの扱いが煩雑になるため、
    Application の source ブロックを正規表現で拾う。
    形式が変わったら気づけるよう、1 件も見つからなければエラーにする。
    """
    pattern = re.compile(
        r"repoURL:\s*(?P<repo>(?:https?://|oci://|ghcr\.io/)[^\s]+)\s*\n"
        r"\s*chart:\s*(?P<chart>[^\s]+)\s*\n"
        r"\s*targetRevision:\s*(?P<version>[^\s]+)"
        r"(?:.*?valueFiles:\s*\n\s*-\s*\$values/(?P<values>[^\s]+))?",
        re.S,
    )
    found = []
    for path in sorted((REPO / "kubernetes" / "apps").glob("*.yaml")):
        for m in pattern.finditer(path.read_text()):
            found.append((m.group("repo"), m.group("chart"),
                          m.group("version"), m.group("values")))
    return found


def main() -> int:
    charts = extract_charts()
    if not charts:
        print("::error::Helm chart の定義を 1 件も抽出できませんでした。"
              "kubernetes/apps の形式が変わった可能性があります。")
        return 1

    print(f"{len(charts)} 件の chart を検証します\n")

    failed = False
    aliases = {}
    for repo, _, _, _ in charts:
        if repo.startswith(("oci://", "ghcr.io/")) or repo in aliases:
            continue
        alias = f"repo{len(aliases)}"
        add = subprocess.run(
            ["helm", "repo", "add", alias, repo, "--force-update"],
            capture_output=True, text=True,
        )
        if add.returncode != 0:
            print(f"::error::helm repo add に失敗: {repo}")
            print(add.stderr.strip())
            failed = True
        else:
            aliases[repo] = alias

    if failed:
        return 1

    subprocess.run(["helm", "repo", "update"], capture_output=True)

    for repo, chart, version, values in charts:
        print(f"::group::{chart} {version} ({repo})")
        if repo.startswith("oci://"):
            chart_ref = f"{repo.rstrip('/')}/{chart}"
        elif repo.startswith("ghcr.io/"):
            chart_ref = f"oci://{repo.rstrip('/')}/{chart}"
        else:
            chart_ref = f"{aliases[repo]}/{chart}"

        cmd = ["helm", "template", chart, chart_ref,
               "--version", version, "--kube-version", "1.34.3"]
        if values:
            vf = REPO / values
            if not vf.exists():
                print(f"::error::values ファイルが存在しません: {values}")
                failed = True
                print("::endgroup::")
                continue
            cmd += ["-f", str(vf)]

        render = subprocess.run(cmd, capture_output=True, text=True)
        if render.returncode != 0:
            print(f"::error::helm template に失敗: {chart} {version}")
            print(render.stderr.strip()[:2000])
            failed = True
        else:
            lines = len(render.stdout.splitlines())
            print(f"OK: {lines} 行をレンダリングしました")
        print("::endgroup::")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
