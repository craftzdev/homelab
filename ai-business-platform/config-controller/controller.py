"""Single-process GitOps reconciler. GitHub credentials never enter the Gateway/UI.

Run with --once for a reconciler tick, or without it for polling. Target policy
is a root/operator-owned JSON file; releases cannot supply repositories or checks.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import PurePosixPath

import yaml


class Blocked(RuntimeError):
    pass


class HTTP:
    def __init__(self, base, headers):
        self.base, self.headers = base.rstrip("/"), headers

    def __call__(self, method, path, body=None):
        request = urllib.request.Request(self.base + path, method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "ai-config-controller", **self.headers},
            data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(4_194_305)
        if len(raw) > 4_194_304:
            raise Blocked("upstream response exceeds limit")
        return json.loads(raw) if raw else {}


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def managed_path(source, target):
    path = source["path"]
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or "\\" in path or str(pure) != path:
        raise Blocked("unmanaged source path")
    if source["component"] != target["component"]:
        raise Blocked("component does not match target")
    if source.get("source_mapping"):
        if source["kind"] != "harness" or path != "harness/AGENTS.md" or source["source_mapping"] != {"path": "deploy/kubernetes/codex-harness.yaml", "key": "data.AGENTS.md"}:
            raise Blocked("unsupported source mapping")
        if "deploy/kubernetes/codex-harness.yaml" not in target["allowed_paths"]:
            raise Blocked("harness is not allowed by target policy")
        return "deploy/kubernetes/codex-harness.yaml", "AGENTS.md"
    if source["kind"] == "harness":
        raise Blocked("harness requires the canonical ConfigMap mapping")
    roots = ("profiles/", "schemas/") if target["component"] == "agent" else ("skills/",)
    if not path.startswith(roots) or not any(path.startswith(prefix) for prefix in target["allowed_paths"]):
        raise Blocked("path is not allowed by target policy")
    return path, None


class GitOps:
    def __init__(self, github, targets):
        self.github, self.targets = github, targets

    def reconcile(self, release):
        target = self.targets.get(release["source"]["repository"])
        if target is None:
            raise Blocked("repository target is not configured")
        repo = target["repository"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise Blocked("invalid configured repository")
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", target["branch"]):
            raise Blocked("invalid configured branch")
        api = lambda method, path, body=None: self.github(method, f"/repos/{repo}" + path, body)
        path, key = managed_path(release["source"], target)
        if digest(release["content"]) != release["content_sha256"]:
            raise Blocked("release digest mismatch")
        branch = "codex/config-" + release["id"]
        q = lambda value: urllib.parse.quote(value, safe="")

        def contents(filename, ref):
            item = api("GET", f"/contents/{filename}?ref={q(ref)}")
            if item.get("type") != "file" or item.get("encoding") != "base64" or item.get("size", 0) > 262_144:
                raise Blocked("Git source is not a bounded regular file")
            return base64.b64decode(item["content"]).decode()

        def extract(text):
            return yaml.safe_load(text)["data"][key] if key else text

        if release["state"] == "QUEUED":
            # Reuse a deterministic branch/PR after a network failure or restart.
            try:
                ref = api("GET", "/git/ref/heads/" + branch)
            except urllib.error.HTTPError as failure:
                if failure.code != 404:
                    raise
                ref = None
            if ref:
                head = ref["object"]["sha"]
                commit = api("GET", "/git/commits/" + head)
                if commit["message"] != "config release " + release["id"] or len(commit["parents"]) != 1:
                    raise Blocked("release branch was modified")
                base = commit["parents"][0]["sha"]
                if extract(contents(path, head)) != release["content"] or extract(contents(path, base)) != release["base_content"]:
                    raise Blocked("release branch content was modified")
                # A recovered branch must contain only the intended configuration
                # and the known rollout annotation, never extra code changes.
                changed = api("GET", f"/compare/{base}...{head}")
                allowed = {path} | ({"deploy/kubernetes/deployment.yaml"} if key else set())
                if changed.get("total_commits") != 1 or {item["filename"] for item in changed.get("files", [])} != allowed:
                    raise Blocked("release branch contains unexpected changes")
                if key:
                    before = yaml.safe_load(contents(path, base))
                    before["data"][key] = release["content"]
                    if yaml.safe_load(contents(path, head)) != before:
                        raise Blocked("ConfigMap contains unexpected changes")
                    deployment_path = "deploy/kubernetes/deployment.yaml"
                    expected = yaml.safe_load(contents(deployment_path, base))
                    expected["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})["ai-business/config-release"] = release["id"]
                    if yaml.safe_load(contents(deployment_path, head)) != expected:
                        raise Blocked("deployment contains unexpected changes")
            else:
                base = api("GET", "/git/ref/heads/" + target["branch"])["object"]["sha"]
                original = contents(path, base)
                if extract(original) != release["base_content"]:
                    raise Blocked("Git source changed since the draft was created")
                tree = []
                body = release["content"]
                if key:
                    document = yaml.safe_load(original)
                    document["data"][key] = body
                    body = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
                    # subPath ConfigMap mounts require a new Pod to read the change.
                    deployment_path = "deploy/kubernetes/deployment.yaml"
                    deployment = yaml.safe_load(contents(deployment_path, base))
                    metadata = deployment["spec"]["template"].setdefault("metadata", {})
                    metadata.setdefault("annotations", {})["ai-business/config-release"] = release["id"]
                    tree.append({"path": deployment_path, "mode": "100644", "type": "blob", "content": yaml.safe_dump(deployment, sort_keys=False)})
                tree.append({"path": path, "mode": "100644", "type": "blob", "content": body})
                base_tree = api("GET", "/git/commits/" + base)["tree"]["sha"]
                new_tree = api("POST", "/git/trees", {"base_tree": base_tree, "tree": tree})["sha"]
                head = api("POST", "/git/commits", {"message": "config release " + release["id"], "tree": new_tree, "parents": [base]})["sha"]
                api("POST", "/git/refs", {"ref": "refs/heads/" + branch, "sha": head})
            prs = api("GET", "/pulls?state=all&head=" + q(repo.split('/')[0] + ':' + branch))
            pr = next((p for p in prs if p["head"]["sha"] == head and p["base"]["ref"] == target["branch"]), None)
            if pr is None:
                pr = api("POST", "/pulls", {"head": branch, "base": target["branch"],
                    "title": "Configure " + path, "body": f"Immutable configuration release `{release['id']}`.\n\nContent SHA-256: `{release['content_sha256']}`\n\nCI and the configured runtime trial must pass before promotion from Control Plane."})
            return "REVIEW", {"head_sha": head, "base_sha": base, "pr_number": pr["number"], "pr_url": pr["html_url"], "content_sha256": release["content_sha256"], "target": target.get("pool", "stable")}

        proof = dict(release["evidence"])
        pr = api("GET", "/pulls/" + str(int(proof["pr_number"])))
        if pr["head"]["sha"] != proof["head_sha"] or pr["base"]["ref"] != target["branch"]:
            raise Blocked("pull request head or target changed; create a new release")
        # Also recover a merge whose successful HTTP response was lost.
        if pr.get("merged"):
            if release["state"] != "PROMOTE_REQUESTED":
                raise Blocked("pull request was merged outside the promotion request")
            return "MERGED", {**proof, "merge_sha": pr["merge_commit_sha"]}
        if pr["state"] != "open":
            raise Blocked("pull request is closed")
        current_base = api("GET", "/git/ref/heads/" + target["branch"])["object"]["sha"]
        if current_base != proof["base_sha"]:
            raise Blocked("target branch changed; revalidate a new release against its new base")
        report = api("GET", "/commits/" + proof["head_sha"] + "/check-runs?per_page=100")
        if report["total_count"] > 100:
            raise Blocked("too many CI checks; cannot establish complete evidence")
        required = target["required_checks"]
        trial = target["runtime_trial_check"]
        if not required or trial not in required:
            raise Blocked("target must require a runtime trial check")
        # All checks with a required name must pass. Queued reruns invalidate a
        # previous success; an arbitrary check from another app is not evidence.
        checks = report["check_runs"]
        passed = lambda name: any(c["name"] == name for c in checks) and all(
            c.get("head_sha") == proof["head_sha"] and c.get("app", {}).get("slug") == "github-actions"
            and c["status"] == "completed" and c["conclusion"] == "success"
            for c in checks if c["name"] == name)
        proof.update(checks_passed=all(passed(name) for name in required), runtime_trial_passed=passed(trial),
                     required_checks=required, checks=[{"name": c["name"], "status": c["status"], "conclusion": c["conclusion"]} for c in checks if c["name"] in required])
        if release["state"] == "PROMOTE_REQUESTED":
            if not proof["checks_passed"]:
                raise Blocked("CI or trial no longer passes")
            merged = api("PUT", f"/pulls/{proof['pr_number']}/merge", {"sha": proof["head_sha"], "merge_method": "merge"})
            if not merged.get("merged"):
                raise Blocked("GitHub did not merge the release")
            return "MERGED", {**proof, "merge_sha": merged["sha"]}
        return ("VERIFIED" if proof["checks_passed"] else "REVIEW"), proof


def tick(gateway, gitops):
    for release in gateway("GET", "/v1/config/controller/work")["releases"]:
        try:
            state, evidence = gitops.reconcile(release)
        except Blocked as failure:
            state, evidence = "BLOCKED", {**release["evidence"], "reason": str(failure)}
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
            # Transient/uncertain upstream writes are retried with the same branch.
            # Never log tokens or upstream response bodies.
            logging.warning("release %s: reconciliation unavailable; retrying next tick", release["id"])
            continue
        try:
            gateway("POST", f"/v1/config/controller/releases/{release['id']}/report",
                    {"expected_revision": release["revision"], "state": state, "evidence": evidence})
        except (OSError, ValueError):
            logging.warning("release %s: report unavailable; reconcile again", release["id"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    with open(args.targets) as stream:
        targets = json.load(stream)
    headers = {"Authorization": "Bearer " + os.environ["GATEWAY_API_TOKEN"], "X-Config-Controller-Token": os.environ["CONFIG_CONTROLLER_TOKEN"]}
    if os.environ.get("CF_ACCESS_CLIENT_ID") and os.environ.get("CF_ACCESS_CLIENT_SECRET"):
        headers.update({"CF-Access-Client-Id": os.environ["CF_ACCESS_CLIENT_ID"], "CF-Access-Client-Secret": os.environ["CF_ACCESS_CLIENT_SECRET"]})
    gateway = HTTP(os.environ["GATEWAY_URL"], headers)
    github = HTTP("https://api.github.com", {"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"], "X-GitHub-Api-Version": "2022-11-28"})
    while True:
        try:
            tick(gateway, GitOps(github, targets))
        except (OSError, ValueError):
            logging.warning("Gateway work feed unavailable; retrying next tick")
            if args.once:
                raise SystemExit(1)
        if args.once:
            break
        time.sleep(15)


if __name__ == "__main__":
    main()
