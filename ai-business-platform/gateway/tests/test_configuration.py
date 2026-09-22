import copy
import hashlib
import io
import json
import uuid
from unittest import mock

import pytest

from app import configuration, main as gateway

BOT = {"Authorization": "Bearer test-gateway-credential"}
EDITOR = {**BOT, "X-Config-Admin-Token": "c" * 64}
FETCH_INVENTORY = configuration.fetch_inventory


def source(content="# Worker policy\nKeep evidence.\n", **overrides):
    doc = {
        "component": "worker", "kind": "harness", "path": "harness/AGENTS.md",
        "content": content, "sha256": configuration.digest(content),
        "repository": "ai-business-worker", "observed_from": "installed_file",
        **overrides,
    }
    doc["id"] = configuration.digest(f"{doc['component']}:{doc['path']}")
    return doc


@pytest.fixture(autouse=True)
def configuration_setup(client, monkeypatch):
    monkeypatch.setenv("CONFIG_ADMIN_TOKEN", "c" * 64)
    monkeypatch.setenv("CONFIG_ADMIN_ACTOR", "operator")
    monkeypatch.setattr(configuration, "fetch_inventory", lambda: {"documents": [source()], "components": [{"component": "worker", "status": "available"}]})
    with gateway.pool.connection() as db:
        db.execute("TRUNCATE config_source_names,config_draft_revisions,config_drafts CASCADE")


def create(client, *, headers=EDITOR, key=None, content="# Revised\nKeep evidence.\n"):
    return client.post("/v1/config/drafts", headers={**headers, "Idempotency-Key": key or f"config-{uuid.uuid4()}"}, json={
        "source_id": source()["id"], "base_sha256": source()["sha256"], "content": content,
    })


def test_editing_requires_separate_identity_and_never_applies(client):
    assert client.get("/v1/config/inventory").status_code == 401
    assert create(client, headers=BOT).status_code == 401
    draft = create(client).json()
    assert draft["created_by"] == "config-editor:operator"
    assert draft["applied"] is False
    assert draft["state"] == "DRAFT"
    assert "-# Worker policy" in draft["diff"]
    assert source()["content"] == draft["base_content"]


def test_same_submission_replays_but_changed_payload_conflicts(client):
    a = create(client, key="same-operation")
    b = create(client, key="same-operation")
    assert a.status_code == b.status_code == 201
    assert a.json()["id"] == b.json()["id"]
    assert create(client, key="same-operation", content="Another edit").status_code == 409
    with gateway.pool.connection() as db:
        assert db.execute("SELECT count(*) n FROM config_drafts").fetchone()["n"] == 1


def test_revision_conflict_history_and_validation_reset(client):
    draft = create(client).json()
    path = f'/v1/config/drafts/{draft["id"]}'
    checked = client.post(path + "/validate", headers=EDITOR, json={"expected_revision": 1}).json()
    assert checked["state"] == "VALIDATED"
    assert checked["validation"]["scope"] == "syntax_only"
    assert checked["validation"]["activation_ready"] is False
    assert client.patch(path, headers=EDITOR, json={"expected_revision": 1, "content": "stale edit"}).status_code == 409
    edited = client.patch(path, headers=EDITOR, json={"expected_revision": 2, "content": "updated body"}).json()
    assert edited["state"] == "DRAFT" and edited["validation"] is None
    history = client.get(path, headers=BOT).json()["history"]
    assert [item["revision"] for item in history] == [3, 2, 1]
    assert all(item["actor"] == "config-editor:operator" for item in history)


def test_changed_installed_base_invalidates_validation(client, monkeypatch):
    draft = create(client).json()
    monkeypatch.setattr(configuration, "fetch_inventory", lambda: {"documents": [source("New installed version")]})
    response = client.post(f'/v1/config/drafts/{draft["id"]}/validate', headers=EDITOR, json={"expected_revision": 1})
    assert response.json()["state"] == "VALIDATION_FAILED"
    assert response.json()["validation"]["base_current"] is False
    assert create(client).status_code == 409


def test_validation_does_not_execute_python_or_fetch_schema_refs(tmp_path):
    marker = tmp_path / "executed"
    doc = source(kind="skill_file", path="skills/example/scripts/task.py")
    report = configuration.validate_content(doc, f'open({str(marker)!r}, "w").write("wrong")\n')
    assert report["passed"] and not marker.exists()
    with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
        report = configuration.validate_content(source(kind="schema"), '{"$ref":"https://invalid.example/schema"}')
    assert report["passed"] and not report["activation_ready"]


def test_skill_metadata_and_multibyte_limit():
    doc = source(kind="skill", skill_id="writing")
    assert not configuration.validate_content(doc, "No frontmatter")["passed"]
    assert not configuration.validate_content(doc, "---\nname: wrong\ndescription: Write\n---\nSteps")["passed"]
    assert configuration.validate_content(doc, "---\nname: writing\ndescription: Write\n---\nSteps")["passed"]
    assert not configuration.validate_content(source(), "文" * 30_000)["passed"]


def test_yaml_aliases_are_rejected_before_expansion():
    doc = source(kind="skill_file", path="skills/example/references/config.yaml")
    assert not configuration.validate_content(doc, "items: &loop [*loop]")["passed"]
    assert not configuration.validate_content(doc, "a: &a [one, two]\nb: [*a, *a]")["passed"]


def test_edit_during_validation_cannot_attach_old_validation(client, monkeypatch):
    draft = create(client).json()
    path = f'/v1/config/drafts/{draft["id"]}'
    def inventory_with_concurrent_update():
        changed = client.patch(path, headers=EDITOR, json={"expected_revision": 1, "content": "concurrent edit"})
        assert changed.status_code == 200
        return {"documents": [source()]}
    monkeypatch.setattr(configuration, "fetch_inventory", inventory_with_concurrent_update)
    assert client.post(path + "/validate", headers=EDITOR, json={"expected_revision": 1}).status_code == 409
    current = client.get(path, headers=BOT).json()
    assert current["content"] == "concurrent edit" and current["validation"] is None


def test_remote_inventory_contract_and_digest_are_checked(monkeypatch):
    monkeypatch.setenv("WORKER_BASE_URL", "https://agent.example/agent")
    monkeypatch.setenv("WORKER_API_TOKEN", "only-for-agent")
    report = {"documents": [source()], "components": [{"component": "worker", "status": "available"}]}
    with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(report).encode())) as call:
        observed = FETCH_INVENTORY()
    assert observed["documents"][0]["sha256"] == source()["sha256"]
    assert observed["activation_supported"] is False
    assert call.call_args.args[0].full_url == "https://agent.example/agent/v1/configuration"
    report["documents"][0]["content"] = "tampered"
    with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(report).encode())):
        with pytest.raises(gateway.HTTPException) as failure:
            FETCH_INVENTORY()
    assert failure.value.status_code == 502


def test_logical_names_are_durable_revisioned_metadata(client):
    path = '/v1/config/sources/' + source()['id'] + '/name'
    body = {'logical_name': ' 共通の作業ルール ', 'expected_revision': 0}
    assert client.post(path, headers=BOT, json=body).status_code == 401
    named = client.post(path, headers=EDITOR, json=body).json()
    assert named['logical_name'] == '共通の作業ルール' and named['name_revision'] == 1
    assert client.post(path, headers=EDITOR, json=body).json() == named
    inventory = client.get('/v1/config/inventory', headers=BOT).json()['documents'][0]
    assert inventory['logical_name'] == named['logical_name']
    assert inventory['sha256'] == source()['sha256'] and inventory['path'] == source()['path']
    assert client.post(path, headers=EDITOR, json={**body, 'logical_name': '別の名前'}).status_code == 409
    for invalid in ['x' * 81, '名前\n改行', '名前\x00']:
        assert client.post(path, headers=EDITOR, json={**body, 'logical_name': invalid}).status_code == 422
    assert client.post('/v1/config/sources/' + 'f'*64 + '/name', headers=EDITOR, json=body).status_code == 404
    cleared = client.post(path, headers=EDITOR, json={'logical_name': '', 'expected_revision': 1}).json()
    assert cleared['logical_name'] is None and cleared['name_revision'] == 2
    with gateway.pool.connection() as db:
        assert db.execute('SELECT count(*) n FROM config_drafts').fetchone()['n'] == 0
