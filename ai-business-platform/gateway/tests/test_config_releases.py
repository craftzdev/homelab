import uuid

import pytest

from app import config_releases, configuration as config, main as gateway, tasks

BOT = {"Authorization": "Bearer test-gateway-credential"}
EDITOR = {**BOT, "X-Config-Admin-Token": "c" * 64}
CONTROLLER = {**BOT, "X-Config-Controller-Token": "d" * 64}


@pytest.fixture
def source(client, monkeypatch):
    monkeypatch.setenv("CONFIG_ADMIN_TOKEN", "c" * 64)
    monkeypatch.setenv("CONFIG_ADMIN_ACTOR", "operator")
    monkeypatch.setenv("CONFIG_CONTROLLER_TOKEN", "d" * 64)
    source = {"id": config.digest("agent:profiles/example.md"), "path": "profiles/example.md", "component": "agent", "kind": "profile", "repository": "ai-business-agent", "content": "Old instructions\n", "sha256": config.digest("Old instructions\n")}
    monkeypatch.setattr(config, "fetch_inventory", lambda: {"documents": [source]})
    with gateway.pool.connection() as db:
        db.execute("TRUNCATE config_releases,config_draft_revisions,config_drafts CASCADE")
    return source


def draft(client, source):
    response = client.post("/v1/config/drafts", headers={**EDITOR, "Idempotency-Key": str(uuid.uuid4())}, json={"source_id": source["id"], "base_sha256": source["sha256"], "content": "New instructions\n"})
    assert response.status_code == 201, response.text
    return response.json()


def release(client, source):
    item = draft(client, source)
    path = f'/v1/config/drafts/{item["id"]}'
    assert client.post(path + "/release", headers=EDITOR, json={"expected_revision": 1}).status_code == 409
    assert client.post(path + "/validate", headers=EDITOR, json={"expected_revision": 1}).status_code == 200
    created = client.post(path + "/release", headers=EDITOR, json={"expected_revision": 2})
    assert created.status_code == 201, created.text
    assert client.post(path + "/release", headers=EDITOR, json={"expected_revision": 2}).json()["id"] == created.json()["id"]
    return created.json()


def report(client, row, state, proof=None):
    return client.post(f'/v1/config/controller/releases/{row["id"]}/report', headers=CONTROLLER, json={"expected_revision": row["revision"], "state": state, "evidence": proof or {}})


def test_fixed_release_cannot_be_changed_by_later_edits(client, source):
    row = release(client, source)
    work = client.get("/v1/config/controller/work", headers=CONTROLLER)
    assert work.status_code == 200, work.text
    assert work.json()["releases"][0]["id"] == row["id"]
    assert work.json()["releases"][0]["last_polled_at"] is not None
    assert client.patch(f'/v1/config/drafts/{row["draft_id"]}', headers=EDITOR, json={"expected_revision": 2, "content": "Different instructions"}).status_code == 200
    detail = client.get(f'/v1/config/releases/{row["id"]}', headers=BOT)
    assert detail.status_code == 200, detail.text
    assert detail.json()["content"] == "New instructions\n"
    assert detail.json()["observation"]["status"] == "DIFFERENT"
    assert detail.json()["history"][0]["type"] == "config.release_queued"


def test_human_and_controller_boundaries_and_trial_required(client, source):
    row = release(client, source)
    path = f'/v1/config/releases/{row["id"]}/promote'
    for headers in (BOT, CONTROLLER):
        assert client.post(path, headers=headers, json={"expected_revision": 1}).status_code == 401
    assert client.get("/v1/config/controller/work", headers=EDITOR).status_code == 401
    assert client.post(path, headers=EDITOR, json={"expected_revision": 1}).status_code == 409
    assert report(client, row, "MERGED").status_code == 409
    reviewed = report(client, row, "REVIEW").json()
    proof = {"head_sha": "a" * 40, "checks_passed": True, "content_sha256": row["content_sha256"]}
    assert report(client, reviewed, "VERIFIED", proof).status_code == 409
    proof["runtime_trial_passed"] = True
    verified = report(client, reviewed, "VERIFIED", proof).json()
    assert verified["state"] == "VERIFIED"
    # The controller cannot promote itself, even after supplying successful CI.
    assert report(client, verified, "MERGED", proof).status_code == 409
    promoted = client.post(path, headers=EDITOR, json={"expected_revision": verified["revision"]}).json()
    merged = report(client, promoted, "MERGED", proof).json()
    assert merged["state"] == "MERGED"
    # Git success is not an observed deployment.
    detail = client.get(path.removesuffix("/promote"), headers=BOT).json()
    assert detail["observation"]["status"] == "DIFFERENT"
    source.update(content=row["content"], sha256=row["content_sha256"])
    detail = client.get(path.removesuffix("/promote"), headers=BOT).json()
    assert detail["observation"]["status"] == "MATCH" and not detail["observation"]["runtime_consumed"]
    reverted = client.post(path.removesuffix("/promote") + "/rollback", headers=EDITOR, json={"expected_revision": merged["revision"]})
    assert reverted.status_code == 201, reverted.text
    assert reverted.json()["content"] == "Old instructions\n"
    assert reverted.json()["state"] == "DRAFT"


def test_worker_intake_is_revisioned_and_requires_human_credential(client, source):
    with gateway.pool.connection() as db:
        tasks.record_worker_report(db, logical_id="worker-1", report={"accepting_jobs": True, "intake_revision": 0})
    path = "/v1/config/workers/worker-1/intake"
    body = {"expected_revision": 0, "accepting_jobs": False, "reason": "Configuration trial"}
    assert client.post(path, headers=BOT, json=body).status_code == 401
    assert client.post("/v1/workers/worker-1/commands", headers=BOT, json={"type": "resume"}).status_code == 401
    result = client.post(path, headers=EDITOR, json=body)
    assert result.status_code == 202, result.text
    assert result.json()["applied"] is False
    assert client.post(path, headers=EDITOR, json=body).status_code == 409
    body.update(expected_revision=1, accepting_jobs=True)
    assert client.post(path, headers=EDITOR, json=body).status_code == 202
    assert client.post("/v1/config/workers/absent/intake", headers=EDITOR, json=body).status_code == 404


def test_unknown_inventory_is_never_applied(client, source, monkeypatch):
    row = release(client, source)
    monkeypatch.setattr(config, "fetch_inventory", lambda: config.error("UNAVAILABLE", "offline", 502))
    detail = client.get(f'/v1/config/releases/{row["id"]}', headers=BOT).json()
    assert detail["observation"]["status"] == "UNKNOWN"
