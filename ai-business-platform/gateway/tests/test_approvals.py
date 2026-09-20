"""Approval invariants against disposable PostgreSQL, not a mocked SQL engine."""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from app import main as gateway

BOT = {"Authorization": "Bearer test-gateway-credential"}
HUMAN = {**BOT, "X-Human-Approval-Token": "test-human-credential"}
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


def execute(sql, args=(), *, one=False):
    with gateway.pool.connection() as db:
        cursor = db.execute(sql, args)
        return cursor.fetchone() if one else None


def seed(project_id="test-product", verdict="pass"):
    build_id, qa_id = uuid.uuid4(), uuid.uuid4()
    now = gateway.utcnow()
    with gateway.pool.connection() as db:
        db.execute(
            "INSERT INTO projects (id,title,idea,state,repository_url,build_job_id,qa_job_id,created_at,updated_at) "
            "VALUES (%s,'test','test idea','QA_PASSED',%s,%s,%s,%s,%s)",
            (
                project_id,
                f"https://github.com/example/{project_id}",
                build_id,
                qa_id,
                now,
                now,
            ),
        )
        for job_id, action, params, result, worker_id in (
            (build_id, "code.build", {}, {"base_commit": "0" * 40}, "worker-build-1"),
            (
                qa_id,
                "qa.review",
                {"source_worker_job_id": "worker-build-1"},
                {
                    "report": {
                        "verdict": verdict,
                        "summary": "QA report",
                        # The QA executor requires these fields in its report.
                        "acceptance_criteria": [
                            {"criterion": "CSV を取り込める", "verdict": verdict}
                        ],
                        "risks": [],
                    }
                },
                "worker-qa-1",
            ),
        ):
            db.execute(
                "INSERT INTO jobs (id,idempotency_key,project_id,action,environment,state,input,result,worker_job_id,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,'preview','SUCCEEDED',%s,%s,%s,%s,%s)",
                (
                    job_id,
                    str(job_id),
                    project_id,
                    action,
                    json.dumps({"payload": {"parameters": params}}),
                    json.dumps(result),
                    worker_id,
                    now,
                    now,
                ),
            )
    return {
        "commit_sha": SHA,
        "image_digest": DIGEST,
        "environment": "production",
        "build_job_id": str(build_id),
        "qa_job_id": str(qa_id),
    }


def candidate(client, project_id="test-product", verdict="pass"):
    body = seed(project_id, verdict)
    response = client.put(
        f"/v1/projects/{project_id}/release-candidate", json=body, headers=HUMAN
    )
    assert response.status_code == 200, response.text
    return body, response.json()


def pending(client, project_id="test-product", verdict="pass"):
    body, _target = candidate(client, project_id, verdict)
    response = client.post(f"/v1/projects/{project_id}/approval", headers=BOT)
    assert response.status_code == 201, response.text
    return body, response.json()


def approve(client, approval, **overrides):
    return client.post(
        f"/v1/approvals/{approval['id']}/approve",
        headers=HUMAN,
        json={"target_sha256": approval["target_sha256"], **overrides},
    )


def release(client, approval, project_id="test-product", **overrides):
    return client.put(
        f"/v1/projects/{project_id}/production",
        headers=HUMAN,
        json={
            "approval_id": approval["id"],
            "commit_sha": SHA,
            "image_digest": DIGEST,
            "environment": "production",
            "production_url": "https://example.invalid/",
            **overrides,
        },
    )


def test_bound_approval_and_single_use_release(client):
    _, approval = pending(client)
    assert approval["approved_by"] is None
    assert approval["target"]["commit_sha"] == SHA
    result = approve(client, approval)
    assert result.status_code == 200, result.text
    assert result.json()["approved_by"] == "craftz"
    assert result.json()["resolved_at"]
    assert (
        result.json()["expires_at"] == approval["expires_at"]
    )  # approving does not extend TTL
    assert release(client, approval).status_code == 200
    assert release(client, approval).status_code == 409
    stored = client.get(f"/v1/approvals/{approval['id']}", headers=BOT).json()
    assert stored["state"] == "CONSUMED"
    assert stored["consumed_at"]
    event = execute(
        "SELECT payload FROM project_events WHERE event_type='approval.approved'",
        one=True,
    )
    assert event["payload"]["approved_by"] == "craftz"
    assert event["payload"]["target"]["image_digest"] == DIGEST


@pytest.mark.parametrize("approved", [False, True])
def test_expiry_and_rerequest(client, approved):
    _, approval = pending(client)
    if approved:
        assert approve(client, approval).status_code == 200
    execute(
        "UPDATE approvals SET expires_at=%s WHERE id=%s",
        (gateway.utcnow() - timedelta(seconds=1), approval["id"]),
    )
    assert approve(client, approval).status_code == 409
    assert release(client, approval).status_code == 409
    listed = client.get("/v1/projects/test-product/approvals", headers=BOT).json()
    assert listed["approvals"][0]["effective_state"] == "EXPIRED"
    assert (
        client.post("/v1/projects/test-product/approval", headers=BOT).status_code
        == 201
    )


@pytest.mark.parametrize(
    "change",
    [
        {"commit_sha": "c" * 40},
        {"image_digest": "sha256:" + "d" * 64},
        {"image_digest": None},
    ],
)
def test_release_target_mismatch_rejected(client, change):
    _, approval = pending(client)
    assert approve(client, approval).status_code == 200
    assert release(client, approval, **change).status_code == 409
    assert release(client, approval).status_code == 200


def test_bot_cannot_approve_or_register_candidate(client):
    body, approval = pending(client)
    assert (
        client.put(
            "/v1/projects/test-product/release-candidate", json=body, headers=BOT
        ).status_code
        == 401
    )
    assert (
        client.post(
            f"/v1/approvals/{approval['id']}/approve",
            headers=BOT,
            json={"target_sha256": approval["target_sha256"]},
        ).status_code
        == 401
    )
    assert approve(client, approval, approved_by="someone-else").status_code == 422


def test_actor_is_server_bound_and_required(client, monkeypatch):
    _, approval = pending(client)
    monkeypatch.setattr(gateway, "HUMAN_APPROVAL_ACTOR", "")
    assert approve(client, approval).status_code == 503
    monkeypatch.setattr(gateway, "HUMAN_APPROVAL_ACTOR", "craftz")
    result = client.post(
        f"/v1/approvals/{approval['id']}/approve",
        headers={**HUMAN, "X-Approver": "attacker"},
        json={"target_sha256": approval["target_sha256"]},
    )
    assert result.status_code == 200
    assert result.json()["approved_by"] == "craftz"


def test_acknowledged_hash_must_match(client):
    _, approval = pending(client)
    assert approve(client, approval, target_sha256="f" * 64).status_code == 409


@pytest.mark.parametrize("approved", [False, True])
def test_new_candidate_invalidates_old_approval(client, approved):
    body, approval = pending(client)
    if approved:
        assert approve(client, approval).status_code == 200
    body["commit_sha"] = "e" * 40
    assert (
        client.put(
            "/v1/projects/test-product/release-candidate", json=body, headers=HUMAN
        ).status_code
        == 200
    )
    assert approve(client, approval).status_code == 409
    assert release(client, approval).status_code == 409
    assert (
        client.get(f"/v1/approvals/{approval['id']}", headers=BOT).json()["state"]
        == "INVALIDATED"
    )


@pytest.mark.parametrize("action", ["code.build", "code.fix", "qa.review"])
def test_new_work_invalidates_grant_and_candidate(client, action):
    _, approval = pending(client)
    assert approve(client, approval).status_code == 200
    result = client.post(
        "/v1/jobs",
        headers={**BOT, "Idempotency-Key": "test-new-build-1"},
        json={
            "action": action,
            "project_id": "test-product",
            "environment": "preview",
            "parameters": {},
        },
    )
    assert result.status_code == 202, result.text
    assert release(client, approval).status_code == 409
    assert (
        client.get("/v1/projects/test-product", headers=BOT).json()["release_candidate"]
        is None
    )


def test_wrong_project_cannot_consume_approval(client):
    _, approval = pending(client)
    assert approve(client, approval).status_code == 200
    assert release(client, approval, project_id="another-product").status_code == 409


def test_qa_must_review_selected_build(client):
    body = seed()
    execute(
        "UPDATE jobs SET input=%s WHERE id=%s",
        (
            json.dumps(
                {"payload": {"parameters": {"source_worker_job_id": "different-build"}}}
            ),
            body["qa_job_id"],
        ),
    )
    assert (
        client.put(
            "/v1/projects/test-product/release-candidate", json=body, headers=HUMAN
        ).status_code
        == 409
    )


def test_inconclusive_qa_needs_explicit_acknowledgement(client):
    _, approval = pending(client, verdict="inconclusive")
    assert approve(client, approval).status_code == 409
    assert approve(client, approval, accept_inconclusive_qa=True).status_code == 200


def test_failed_qa_cannot_register_candidate(client):
    body = seed(verdict="fail")
    assert (
        client.put(
            "/v1/projects/test-product/release-candidate", json=body, headers=HUMAN
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"commit_sha": None, "image_digest": None},
        {"commit_sha": "main"},
        {"commit_sha": "abcd123"},
        {"image_digest": "latest"},
        {"environment": "preview"},
    ],
)
def test_mutable_or_missing_target_rejected(client, fields):
    body = seed()
    body.update(fields)
    assert (
        client.put(
            "/v1/projects/test-product/release-candidate", json=body, headers=HUMAN
        ).status_code
        == 422
    )


@pytest.mark.parametrize("ttl", [0, 86401])
def test_ttl_bounds(client, ttl):
    candidate(client)
    assert (
        client.post(
            "/v1/projects/test-product/approval", headers=BOT, json={"ttl_seconds": ttl}
        ).status_code
        == 422
    )


def test_no_candidate_or_duplicate_request(client):
    body = seed()
    assert (
        client.post("/v1/projects/test-product/approval", headers=BOT).status_code
        == 409
    )
    assert (
        client.put(
            "/v1/projects/test-product/release-candidate", headers=HUMAN, json=body
        ).status_code
        == 200
    )
    assert (
        client.post("/v1/projects/test-product/approval", headers=BOT).status_code
        == 201
    )
    assert (
        client.post("/v1/projects/test-product/approval", headers=BOT).status_code
        == 409
    )


def test_cancel_granted_approval(client):
    _, approval = pending(client)
    assert approve(client, approval).status_code == 200
    assert (
        client.post(f"/v1/approvals/{approval['id']}/cancel", headers=HUMAN).status_code
        == 200
    )
    assert release(client, approval).status_code == 409


def test_concurrent_release_consumes_once(client):
    _, approval = pending(client)
    assert approve(client, approval).status_code == 200
    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(
            executor.map(lambda _: release(client, approval).status_code, range(2))
        )
    assert sorted(statuses) == [200, 409]


def test_legacy_migration_is_idempotent_and_revokes_unbound_grants(client):
    seed()
    execute("UPDATE projects SET state='DEPLOY_APPROVED' WHERE id='test-product'")
    execute(
        "INSERT INTO approvals (id,project_id,kind,state,requested_at) VALUES (%s,'test-product',"
        "'production.deploy','APPROVED',%s)",
        (uuid.uuid4(), gateway.utcnow()),
    )
    execute(gateway.SCHEMA_SQL)
    execute(gateway.SCHEMA_SQL)
    assert execute("SELECT state FROM approvals", one=True)["state"] == "INVALIDATED"
    assert (
        execute("SELECT state FROM projects", one=True)["state"] == "QA_REVIEW_REQUIRED"
    )
    assert (
        execute(
            "SELECT count(*) AS n FROM project_events WHERE event_type='approval.invalidated'",
            one=True,
        )["n"]
        == 1
    )


def test_production_job_cannot_bypass_approval(client):
    assert (
        client.post(
            "/v1/jobs",
            headers={**BOT, "Idempotency-Key": "prod-bypass-test"},
            json={
                "project_id": "test-product",
                "environment": "production",
                "action": "code.build",
            },
        ).status_code
        == 403
    )


def test_mcp_requests_bound_approval_but_exposes_no_grant_tool(client):
    candidate(client)
    approval = gateway.mcp_request_production_approval("test-product")
    assert approval["target"]["commit_sha"] == SHA
    assert approval["expires_at"]


def test_terminal_callback_cannot_rewrite_approved_evidence(client, monkeypatch):
    body, approval = pending(client)
    assert approve(client, approval).status_code == 200
    monkeypatch.setattr(gateway, "API_SURFACE", "callback")
    response = client.post(
        "/v1/worker-events",
        headers={"Authorization": "Bearer test-callback-credential"},
        json={
            "event_id": "rewrite-terminal",
            "gateway_job_id": body["build_job_id"],
            "dispatch_id": "test-dispatch",
            "worker_job_id": "worker-build-1",
            "event_type": "completed",
            "sequence": 2,
            "occurred_at": gateway.utcnow().isoformat(),
            "data": {},
        },
    )
    assert response.status_code == 409


def test_mcp_discovery_and_readback(client):
    _, approval = pending(client)
    assert (
        gateway.mcp_get_production_approval(approval["id"])["target_sha256"]
        == approval["target_sha256"]
    )
    response = client.post(
        "/mcp",
        headers={**BOT, "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert response.status_code == 200, response.text
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert names == {
        "submit_job",
        "get_job",
        "wait_for_job",
        "get_review_url",
        "submit_business_idea",
        "get_project",
        "request_production_approval",
        "get_production_approval",
        # Task surface. Configuration editing, Harness permissions and
        # human-only decisions stay off the bot surface.
        "list_capabilities",
        "list_workflows",
        "create_task",
        "get_task",
        "list_tasks",
        "request_task_action",
        "add_task_instruction",
    }


def test_idempotent_job_replay_does_not_invalidate_candidate(client):
    body, _target = candidate(client)
    request = gateway.JobCreate(
        action="code.build", project_id="test-product", environment="preview"
    )
    payload = request.model_dump(mode="json")
    import hashlib

    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    execute(
        "UPDATE jobs SET idempotency_key=%s,input=%s WHERE id=%s",
        (
            "test-existing-build",
            json.dumps({"payload": payload, "sha256": digest}),
            body["build_job_id"],
        ),
    )
    response = client.post(
        "/v1/jobs",
        headers={**BOT, "Idempotency-Key": "test-existing-build"},
        json=payload,
    )
    assert response.status_code == 202
    assert (
        client.get("/v1/projects/test-product", headers=BOT).json()["release_candidate"]
        is not None
    )


def test_current_build_change_cannot_use_old_approval(client):
    _, approval = pending(client)
    assert approve(client, approval).status_code == 200
    execute(
        "UPDATE projects SET build_job_id=%s WHERE id='test-product'", (uuid.uuid4(),)
    )
    assert release(client, approval).status_code == 409


@pytest.mark.parametrize("state", ["QUEUED", "DISPATCHED", "ACCEPTED", "RUNNING"])
def test_inflight_work_blocks_candidate_registration(client, state):
    body = seed()
    execute("UPDATE jobs SET state=%s WHERE id=%s", (state, body["build_job_id"]))
    assert (
        client.put(
            "/v1/projects/test-product/release-candidate", headers=HUMAN, json=body
        ).status_code
        == 409
    )


def test_concurrent_approval_requests_are_serialized(client):
    candidate(client)
    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(
            executor.map(
                lambda _: (
                    client.post(
                        "/v1/projects/test-product/approval", headers=BOT
                    ).status_code
                ),
                range(2),
            )
        )
    assert sorted(statuses) == [201, 409]


@pytest.mark.parametrize("omit", ["commit_sha", "image_digest"])
def test_single_immutable_reference_is_supported(client, omit):
    body = seed()
    body.pop(omit)
    response = client.put(
        "/v1/projects/test-product/release-candidate", headers=HUMAN, json=body
    )
    assert response.status_code == 200
    approval = client.post("/v1/projects/test-product/approval", headers=BOT).json()
    assert approve(client, approval).status_code == 200
    assert release(client, approval, **{omit: None}).status_code == 200


def test_legacy_release_and_approval_requests_fail_closed(client):
    _, approval = pending(client)
    assert (
        client.post(
            f"/v1/approvals/{approval['id']}/approve", headers=HUMAN
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/v1/projects/test-product/production",
            headers=HUMAN,
            json={"production_url": "https://example.invalid/"},
        ).status_code
        == 422
    )


def test_completion_invalidates_candidate_and_terminal_replay_is_idempotent(
    client, monkeypatch
):
    body, approval = pending(client)
    assert approve(client, approval).status_code == 200
    # Simulate a job queued before this release, without using the new submission hook.
    execute("UPDATE jobs SET state='RUNNING' WHERE id=%s", (body["build_job_id"],))
    monkeypatch.setattr(gateway, "API_SURFACE", "callback")
    event = {
        "event_id": "completion-invalidation",
        "gateway_job_id": body["build_job_id"],
        "dispatch_id": "test-dispatch",
        "worker_job_id": "worker-build-1",
        "event_type": "completed",
        "sequence": 1,
        "occurred_at": gateway.utcnow().isoformat(),
        "data": {},
    }
    headers = {"Authorization": "Bearer test-callback-credential"}
    assert (
        client.post("/v1/worker-events", headers=headers, json=event).status_code == 202
    )
    response = client.post("/v1/worker-events", headers=headers, json=event)
    assert response.status_code == 202
    assert response.json()["duplicate"]
    assert (
        execute("SELECT state FROM approvals WHERE id=%s", (approval["id"],), one=True)[
            "state"
        ]
        == "INVALIDATED"
    )
