import urllib.error

import pytest

from controller import Blocked, GitOps, digest, managed_path, tick


TARGET = {"component": "agent", "repository": "craftzdev/ai-business-agent", "branch": "main", "allowed_paths": ["profiles/", "schemas/"], "required_checks": ["config-check", "config-runtime-trial"], "runtime_trial_check": "config-runtime-trial", "pool": "stable"}
SOURCE = {"component": "agent", "kind": "profile", "repository": "ai-business-agent", "path": "profiles/example.md"}


def release(state="REVIEW"):
    return {"id": "a49698e4-f885-4e9a-b5cf-ff8a753766f0", "state": state, "revision": 2, "source": SOURCE, "base_content": "old", "content": "new", "content_sha256": digest("new"), "evidence": {"head_sha": "a" * 40, "base_sha": "b" * 40, "pr_number": 1, "content_sha256": digest("new")}}


class GitHub:
    def __init__(self):
        self.calls = []
        self.pr = {"head": {"sha": "a" * 40}, "base": {"ref": "main"}, "state": "open", "merged": False}
        self.checks = [{"name": name, "head_sha": "a" * 40, "app": {"slug": "github-actions"}, "status": "completed", "conclusion": "success"} for name in TARGET["required_checks"]]

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        assert path.startswith("/repos/craftzdev/ai-business-agent/")
        if method == "PUT":
            assert body == {"sha": "a" * 40, "merge_method": "merge"}
            return {"merged": True, "sha": "c" * 40}
        if path.endswith("/pulls/1"):
            return self.pr
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "b" * 40}}
        if "check-runs" in path:
            return {"total_count": len(self.checks), "check_runs": self.checks}
        raise AssertionError(path)


def test_only_operator_owned_repositories_and_paths_are_allowed():
    for path in ("../Dockerfile", "/profiles/example.md", "profiles/../Dockerfile", ".github/workflows/test.yml", "skills/x/SKILL.md"):
        with pytest.raises(Blocked):
            managed_path({**SOURCE, "path": path}, TARGET)
    with pytest.raises(Blocked):
        managed_path({**SOURCE, "source_mapping": {"path": "Dockerfile", "key": "data.AGENTS.md"}}, TARGET)
    row = release()
    row["source"] = {**SOURCE, "repository": "attacker/repo"}
    with pytest.raises(Blocked):
        GitOps(GitHub(), {"ai-business-agent": TARGET}).reconcile(row)


def test_ci_success_never_merges_without_human_request():
    api = GitHub()
    state, proof = GitOps(api, {"ai-business-agent": TARGET}).reconcile(release())
    assert state == "VERIFIED" and proof["runtime_trial_passed"]
    assert all(method == "GET" for method, *_ in api.calls)
    row = release("PROMOTE_REQUESTED")
    row["evidence"] = proof
    state, proof = GitOps(api, {"ai-business-agent": TARGET}).reconcile(row)
    assert state == "MERGED" and proof["merge_sha"] == "c" * 40


@pytest.mark.parametrize("change", ["missing", "queued-rerun", "wrong-app", "wrong-head", "failed"])
def test_trial_or_ci_does_not_pass_from_incomplete_or_old_evidence(change):
    api = GitHub()
    if change == "missing":
        api.checks.pop()
    elif change == "queued-rerun":
        api.checks.append({**api.checks[-1], "status": "queued", "conclusion": None})
    elif change == "wrong-app":
        api.checks[-1]["app"] = {"slug": "untrusted"}
    elif change == "wrong-head":
        api.checks[-1]["head_sha"] = "old"
    else:
        api.checks[-1]["conclusion"] = "failure"
    gitops = GitOps(api, {"ai-business-agent": TARGET})
    assert gitops.reconcile(release())[0] == "REVIEW"
    with pytest.raises(Blocked):
        gitops.reconcile(release("PROMOTE_REQUESTED"))
    assert all(method == "GET" for method, *_ in api.calls)


def test_changed_pr_head_invalidates_approval():
    api = GitHub()
    api.pr["head"]["sha"] = "new-head"
    with pytest.raises(Blocked):
        GitOps(api, {"ai-business-agent": TARGET}).reconcile(release("PROMOTE_REQUESTED"))
    assert all(method == "GET" for method, *_ in api.calls)


def test_merge_response_lost_is_recovered_but_external_merge_is_not_approved():
    api = GitHub()
    api.pr.update(merged=True, merge_commit_sha="c" * 40)
    row = release("PROMOTE_REQUESTED")
    row["evidence"].update(checks_passed=True, runtime_trial_passed=True)
    assert GitOps(api, {"ai-business-agent": TARGET}).reconcile(row)[0] == "MERGED"
    with pytest.raises(Blocked):
        GitOps(api, {"ai-business-agent": TARGET}).reconcile(release())


def test_git_base_content_conflict_does_not_write_anything():
    import base64
    calls = []
    def api(method, path, body=None):
        calls.append(method)
        if "/git/ref/heads/codex/" in path:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "b" * 40}}
        return {"type": "file", "encoding": "base64", "content": base64.b64encode(b"different base").decode()}
    with pytest.raises(Blocked):
        GitOps(api, {"ai-business-agent": TARGET}).reconcile(release("QUEUED"))
    assert calls and set(calls) == {"GET"}


def test_uncertain_network_failure_keeps_same_release_for_retry():
    class Broken:
        def reconcile(self, row):
            raise TimeoutError("unknown whether write succeeded")
    calls = []
    def gateway(method, path, body=None):
        calls.append(method)
        return {"releases": [release("QUEUED")]}
    tick(gateway, Broken())
    assert calls == ["GET"]


def test_create_then_recover_reuses_the_same_git_branch_and_pr():
    import base64
    calls = []
    exists = False
    def api(method, path, body=None):
        nonlocal exists
        calls.append((method, path, body))
        if "/git/ref/heads/codex/" in path:
            if not exists:
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            return {"object": {"sha": "a" * 40}}
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "b" * 40}}
        if "/contents/" in path:
            raw = b"new" if "ref=" + "a" * 40 in path else b"old"
            return {"type": "file", "encoding": "base64", "content": base64.b64encode(raw).decode()}
        if path.endswith("/git/commits/" + "b" * 40):
            return {"tree": {"sha": "base-tree"}}
        if path.endswith("/git/trees"):
            assert body == {"base_tree": "base-tree", "tree": [{"path": "profiles/example.md", "mode": "100644", "type": "blob", "content": "new"}]}
            return {"sha": "new-tree"}
        if path.endswith("/git/commits"):
            assert body["parents"] == ["b" * 40]
            return {"sha": "a" * 40}
        if path.endswith("/git/refs"):
            assert body["ref"] == "refs/heads/codex/config-" + release()["id"]
            exists = True
            return {}
        if "/pulls?" in path:
            return [{"head": {"sha": "a" * 40}, "base": {"ref": "main"}, "number": 1, "html_url": "https://github.com/craftzdev/ai-business-agent/pull/1"}] if exists and any(p.endswith('/pulls') for _,p,_ in calls) else []
        if path.endswith("/pulls"):
            assert body["head"] == "codex/config-" + release()["id"]
            return {"number": 1, "html_url": "https://github.com/craftzdev/ai-business-agent/pull/1"}
        if path.endswith("/git/commits/" + "a" * 40):
            return {"message": "config release " + release()["id"], "parents": [{"sha": "b" * 40}]}
        if "/compare/" in path:
            return {"total_commits": 1, "files": [{"filename": "profiles/example.md"}]}
        raise AssertionError(path)
    gitops = GitOps(api, {"ai-business-agent": TARGET})
    first = gitops.reconcile(release("QUEUED"))
    before = len([m for m,_,_ in calls if m == "POST"])
    recovered = gitops.reconcile(release("QUEUED"))
    assert first == recovered and first[0] == "REVIEW"
    assert len([m for m,_,_ in calls if m == "POST"]) == before
