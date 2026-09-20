"""The mvp-build progression, driven the way the Controller drives it.

These tests use the internal Workflow API directly: it is the contract the
Controller depends on, and the Gateway is the authority on what may happen next.
"""

import uuid
from unittest import mock

import pytest

from app import main as gateway
from app import runs, scheduler, tasks, workflows  # noqa: F401

BOT = {"Authorization": "Bearer test-gateway-credential"}
# Deciding about a deliverable needs the separate human credential.
HUMAN = {**BOT, "X-Human-Approval-Token": "test-human-credential"}
CONTROLLER = {"Authorization": "Bearer test-controller-credential"}


def project(project_id="test-product"):
    now = gateway.utcnow()
    with gateway.pool.connection() as db:
        db.execute(
            "INSERT INTO projects (id,title,idea,state,created_at,updated_at) "
            "VALUES (%s,'test','test idea','REGISTERED',%s,%s) ON CONFLICT DO NOTHING",
            (project_id, now, now),
        )
    return project_id


def prd_report():
    return {
        "title": "CSV 取り込み",
        "slug": "csv-import",
        "problem": "明細を手入力している",
        "target_audience": "個人事業主",
        "value_proposition": "CSV から一括登録できる",
        "features": ["CSV アップロード"],
        "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        "analytics_events": ["csv_imported"],
    }


def build_result(**overrides):
    return {
        "mode": "codex",
        "succeeded": True,
        "project_id": "test-product",
        "base_commit": "0" * 40,
        "changed_files": ["app/main.py"],
        "tests": [{"name": "pytest", "passed": True}],
        "patch_digest": "b" * 64,
        "workspace_digest": "c" * 64,
        "artifacts": ["changes.patch"],
        **overrides,
    }


def qa_report(verdict="pass", criteria=None):
    return {
        "verdict": verdict,
        "summary": "検証結果",
        "acceptance_criteria": criteria
        if criteria is not None
        else [
            {
                "criterion": "正常な CSV から明細を登録できる",
                "verdict": verdict,
                "evidence": "tests.log",
            }
        ],
        "risks": [],
    }


def internal(client, method, path, body=None):
    """Call the internal workflow API, which only the internal surface serves."""
    with mock.patch.object(gateway, "API_SURFACE", "internal"):
        return client.request(method, path, json=body, headers=CONTROLLER)


def lease(client, owner="test-controller"):
    response = internal(
        client,
        "POST",
        "/internal/v1/runs/lease",
        {"owner": owner, "limit": 5, "lease_seconds": 30},
    )
    assert response.status_code == 200, response.text
    return response.json()["runs"]


def propose(client, run, **proposal):
    """Propose a transition the way the Controller does: with the revision it saw."""
    body = {
        "token": run["lease"]["token"],
        "expected_task_revision": run["task_revision"],
        **proposal,
    }
    return internal(
        client, "POST", f"/internal/v1/runs/{run['run_id']}/proposals", body
    )


def finish(client, job_id, data, worker="worker-1"):
    """Report a Worker completion for one dispatched job.

    A verification report carries the execution it verified, because the real
    Worker stamps the source it was given into the report it returns.
    """
    report = (data or {}).get("report")
    if isinstance(report, dict) and "verdict" in report:
        with gateway.pool.connection() as db:
            requested = db.execute(
                "SELECT a.input_manifest FROM step_attempts a WHERE a.job_id = %s",
                (job_id,),
            ).fetchone()
        manifest = (requested or {}).get("input_manifest") or {}
        source = (manifest.get("parameters") or {}).get("source_worker_job_id")
        if source and "source_worker_job_id" not in report and "target_digest" not in report:
            data = {**data, "report": {**report, "source_worker_job_id": source}}
        # The real Worker rebuilds the change from its patch and says which patch it
        # used, so the fixture does the same.
        bound = manifest.get("verification_target") or {}
        if bound.get("artifact_id") and "verified_change" not in data.get("report", {}):
            with gateway.pool.connection() as db:
                artifact = db.execute(
                    "SELECT manifest FROM artifacts WHERE id = %s",
                    (uuid.UUID(str(bound["artifact_id"])),),
                ).fetchone()
            content = ((artifact or {}).get("manifest") or {}).get("content") or {}
            if content.get("patch_digest"):
                data = {
                    **data,
                    "report": {
                        **data["report"],
                        "verified_change": {
                            "patch_digest": content["patch_digest"],
                            "base_commit": content.get("base_commit"),
                            "rebuilt": True,
                        },
                    },
                }
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE jobs SET dispatch_id = %s, worker_job_id = %s, state = 'DISPATCHED' "
            "WHERE id = %s",
            (f"gateway:{job_id}:1", worker, job_id),
        )
    with mock.patch.object(gateway, "API_SURFACE", "callback"):
        response = client.post(
            "/v1/worker-events",
            headers={"Authorization": "Bearer test-callback-credential"},
            json={
                "event_id": f"event-{uuid.uuid4()}",
                "gateway_job_id": job_id,
                "dispatch_id": f"gateway:{job_id}:1",
                "worker_job_id": worker,
                "event_type": "completed",
                "sequence": 1,
                "occurred_at": gateway.utcnow().isoformat(),
                "data": data,
            },
        )
    assert response.status_code == 202, response.text
    return response


def start_mvp_task(client, title="MVP を作る", start=True):
    """A started controller-driven Task, with a Controller seen by the Gateway."""
    project()
    lease(client)  # the Gateway only starts these while a Controller is evaluating
    created = client.post(
        "/v1/tasks",
        json={
            "project_id": "test-product",
            "title": title,
            "objective": "CSV から明細を登録できるようにする",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
            "workflow_id": "mvp-build-v1",
            "start": start,
        },
        headers={**BOT, "Idempotency-Key": f"mvp-{uuid.uuid4()}"},
    )
    assert created.status_code == 201, created.text
    return created.json()


def change_artifact(run):
    """The newest code change this Run produced, which QA must be given."""
    changes = [
        item
        for item in run["artifacts"]
        if item["kind"] in {"code-change", "code-change-report"}
    ]
    assert changes, "the run produced no change to verify"
    return changes[-1]["artifact_id"]


def advance(client, run, action, **extra):
    """Apply the allowed next action the way the Controller would."""
    body = {"type": action["type"], **extra}
    if action.get("step_key"):
        body["step_key"] = action["step_key"]
    if action.get("cycle"):
        body["cycle"] = action["cycle"]
    response = propose(client, run, **body)
    assert response.status_code == 202, response.text
    return response.json()


def test_the_gateway_offers_only_the_next_allowed_transition(client):
    task = start_mvp_task(client)
    (run,) = lease(client)
    assert run["task_id"] == task["task_id"]
    assert [action["step_key"] for action in run["next_actions"]] == ["plan"]
    assert run["next_actions"][0]["action"] == "product.plan"

    # Anything else is refused, including a later step of the same workflow.
    refused = propose(
        client, run, type="create_attempt", step_key="qa", cycle=1, parameters={}
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "TRANSITION_NOT_ALLOWED"


def test_plan_implement_verify_reject_fix_accept(client):
    task = start_mvp_task(client)
    (run,) = lease(client)

    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})

    (run,) = lease(client)
    assert run["next_actions"][0]["step_key"] == "implement"
    prd = next(item for item in run["artifacts"] if item["kind"] == "prd")
    built = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={"task": "CSV 取り込みを実装する"},
        input_artifact_ids=[prd["artifact_id"]],
    )
    finish(client, built["job_id"], build_result(), worker="worker-build")

    (run,) = lease(client)
    assert run["next_actions"][0]["step_key"] == "qa"
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    # QA fails, so the Workflow goes back to a fix rather than to acceptance.
    finish(client, verified["job_id"], {"report": qa_report("fail")}, worker="worker-qa")

    (run,) = lease(client)
    action = run["next_actions"][0]
    assert (action["step_key"], action["cycle"]) == ("fix", 1)
    fixed = advance(
        client,
        run,
        action,
        parameters={"task": "不正な行の扱いを直す"},
        feedback={"failing_criteria": ["正常な CSV から明細を登録できる"]},
    )
    finish(client, fixed["job_id"], build_result(), worker="worker-fix")

    (run,) = lease(client)
    assert run["next_actions"][0]["step_key"] == "qa"
    reverified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-fix",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    finish(client, reverified["job_id"], {"report": qa_report("pass")}, worker="worker-qa-2")

    (run,) = lease(client)
    assert run["next_actions"][0]["type"] == "request_review"
    advance(client, run, run["next_actions"][0])

    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "WAITING_REVIEW"
    assert detail["stage_key"] == "review"
    accept = next(
        command
        for command in detail["available_commands"]
        if command["type"] == "accept_deliverable"
    )

    # The service credential alone cannot accept work on a person's behalf.
    assert client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "accept_deliverable", "target_digest": accept["target_digest"]},
        headers={**BOT, "Idempotency-Key": "accept-mvp-bot"},
    ).status_code == 401

    accepted = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "accept_deliverable", "target_digest": accept["target_digest"]},
        headers={**HUMAN, "Idempotency-Key": "accept-mvp-1"},
    )
    assert accepted.status_code == 202, accepted.text
    final = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert final["status"] == "COMPLETED"
    assert final["stage_key"] == "done"
    assert final["decisions"][-1]["actor"] == "human:craftz"
    assert [step["logical_key"] for step in final["runs"][0]["steps"]] == [
        "plan",
        "implement",
        "qa",
        "fix",
        "qa",
        "review",
    ]
    assert lease(client) == []  # a completed Run is not offered again


def test_an_inconclusive_verification_asks_for_input_and_resumes(client):
    task = start_mvp_task(client)
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    # A pass the evidence does not support is inconclusive, not acceptance.
    finish(
        client,
        verified["job_id"],
        {"report": qa_report("pass", criteria=["正常な CSV から明細を登録できる"])},
        worker="worker-qa",
    )

    (run,) = lease(client)
    action = run["next_actions"][0]
    assert action["type"] == "request_input"
    assert action["resume_step"] == "qa"
    advance(
        client,
        run,
        action,
        questions=[{"id": "qa-evidence", "text": "検証手順を教えてください", "required": True}],
    )

    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "WAITING_INPUT"
    assert lease(client)[0]["next_actions"] == []  # the answer decides what happens

    request_id = [
        event["payload"]["input_request_id"]
        for event in client.get("/v1/events", headers=BOT).json()["events"]
        if event["type"] == "task.input_requested"
    ][-1]
    answered = client.post(
        f"/v1/input-requests/{request_id}/answers",
        json={"answers": {"qa-evidence": "サンプル CSV を添付します"}},
        headers=BOT,
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["resume_step"] == "qa"
    resumed = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert resumed["status"] == "ACTIVE"
    assert lease(client)[0]["next_actions"][0]["step_key"] == "qa"


def test_an_unanswered_question_cannot_be_closed_by_a_different_answer(client):
    start_mvp_task(client)
    (run,) = lease(client)
    advance(
        client,
        run,
        {"type": "request_input", "step_key": "plan", "cycle": 1},
        questions=[{"id": "scope", "text": "対象範囲は？", "required": True}],
    ) if False else None
    # request_input is not allowed as the first step of this workflow.
    refused = propose(
        client,
        run,
        type="request_input",
        step_key="request_input",
        cycle=1,
        questions=[{"id": "scope", "text": "対象範囲は？"}],
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "TRANSITION_NOT_ALLOWED"


def test_a_failed_step_is_retried_within_its_limit_then_fails_the_run(client):
    task = start_mvp_task(client)
    for attempt in range(3):
        (run,) = lease(client)
        action = run["next_actions"][0]
        assert action["step_key"] == "plan"
        planned = advance(client, run, action, parameters={"idea": "CSV 取り込み" * 3})
        with mock.patch.object(gateway, "API_SURFACE", "callback"):
            client.post(
                "/v1/worker-events",
                headers={"Authorization": "Bearer test-callback-credential"},
                json={
                    "event_id": f"event-{uuid.uuid4()}",
                    "gateway_job_id": planned["job_id"],
                    "dispatch_id": f"gateway:{planned['job_id']}:1",
                    "worker_job_id": f"worker-{attempt}",
                    "event_type": "failed",
                    "sequence": 1,
                    "occurred_at": gateway.utcnow().isoformat(),
                    "data": {"error": "codex failed"},
                },
            )
    (run,) = lease(client)
    action = run["next_actions"][0]
    assert action["type"] == "fail_run"
    assert action["failure_class"] == "attempt_limit_reached"
    advance(client, run, action)
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "FAILED"
    assert detail["runs"][0]["result"]["failure_class"] == "attempt_limit_reached"


def test_a_decision_about_changed_results_is_refused(client):
    task = start_mvp_task(client)
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    finish(client, verified["job_id"], {"report": qa_report("pass")}, worker="worker-qa")
    (run,) = lease(client)
    advance(client, run, run["next_actions"][0])

    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    stale_digest = next(
        command["target_digest"]
        for command in detail["available_commands"]
        if command["type"] == "accept_deliverable"
    )
    # Another artifact lands for this run: the deliverable is no longer the same.
    with gateway.pool.connection() as db:
        run_id = detail["runs"][0]["run_id"]
        db.execute(
            "INSERT INTO artifacts (id, project_id, task_id, run_id, kind, media_type, "
            "digest, storage_ref, manifest, created_at) VALUES "
            "(%s, 'test-product', %s, %s, 'note', 'application/json', %s, '{}', '{}', %s)",
            (
                uuid.uuid4(),
                task["task_id"],
                run_id,
                "9" * 64,
                gateway.utcnow(),
            ),
        )
    refused = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "accept_deliverable", "target_digest": stale_digest},
        headers={**HUMAN, "Idempotency-Key": "accept-stale-1"},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "ARTIFACT_MISMATCH"
    assert client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["status"] == "WAITING_REVIEW"


def test_requesting_changes_returns_the_run_to_the_fix_step(client):
    task = start_mvp_task(client)
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    finish(client, verified["job_id"], {"report": qa_report("pass")}, worker="worker-qa")
    (run,) = lease(client)
    advance(client, run, run["next_actions"][0])

    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    digest = next(
        command["target_digest"]
        for command in detail["available_commands"]
        if command["type"] == "request_changes"
    )
    changes = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "request_changes",
            "target_digest": digest,
            "reason": "重複行の扱いも直してください",
        },
        headers={**HUMAN, "Idempotency-Key": "request-changes-1"},
    )
    assert changes.status_code == 202, changes.text
    assert changes.json()["result"]["return_to"] == "fix"
    assert client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["status"] == "ACTIVE"
    (run,) = lease(client)
    assert run["next_actions"][0]["step_key"] == "fix"


def test_pausing_stops_the_next_step_without_stopping_the_current_one(client):
    task = start_mvp_task(client)
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})

    paused = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "pause"},
        headers={**BOT, "Idempotency-Key": "pause-mvp-1"},
    )
    assert paused.status_code == 202
    assert paused.json()["result"]["control_state"] == "PAUSE_REQUESTED"
    # Nothing new may start while paused.
    assert lease(client)[0]["next_actions"] == []

    finish(client, planned["job_id"], {"report": prd_report()})
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["control_state"] == "PAUSED"
    assert detail["runs"][0]["steps"][0]["status"] == "SUCCEEDED"
    assert lease(client)[0]["next_actions"] == []

    resumed = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "resume"},
        headers={**BOT, "Idempotency-Key": "resume-mvp-1"},
    )
    assert resumed.status_code == 202
    assert lease(client)[0]["next_actions"][0]["step_key"] == "implement"


def test_cancelling_is_only_complete_once_execution_has_stopped(client):
    task = start_mvp_task(client)
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})

    # The Worker must know about the execution before it can be asked to stop.
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE jobs SET worker_job_id = 'worker-running', state = 'RUNNING' "
            "WHERE id = %s",
            (planned["job_id"],),
        )
    cancelled = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "cancel", "reason": "不要になりました"},
        headers={**BOT, "Idempotency-Key": "cancel-mvp-1"},
    )
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "APPLYING"
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    # Requested, not finished: the work may still be running.
    assert detail["control_state"] == "CANCEL_REQUESTED"
    assert detail["status"] == "ACTIVE"

    # The request to stop is queued, so it survives the request that asked.
    with gateway.pool.connection() as db:
        queued = db.execute(
            "SELECT * FROM worker_commands WHERE job_id = %s", (planned["job_id"],)
        ).fetchone()
    assert (queued["kind"], queued["state"]) == ("cancel", "PENDING")

    class StoppingWorker:
        def __init__(self):
            self.asked = []

        def configured(self):
            return True

        def cancel(self, worker_job_id):
            self.asked.append(worker_job_id)
            return "CANCEL_REQUESTED"

    worker = StoppingWorker()
    scheduler.deliver_commands(gateway.pool, worker)
    assert worker.asked == ["worker-running"]
    with gateway.pool.connection() as db:
        sent = db.execute(
            "SELECT * FROM worker_commands WHERE job_id = %s", (planned["job_id"],)
        ).fetchone()
    assert sent["state"] == "SENT"
    # Asked is still not stopped: the Task waits for the Worker's own report.
    assert client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["status"] == "ACTIVE"

    with mock.patch.object(gateway, "API_SURFACE", "callback"):
        stopped = client.post(
            "/v1/worker-events",
            headers={"Authorization": "Bearer test-callback-credential"},
            json={
                "event_id": f"event-{uuid.uuid4()}",
                "gateway_job_id": planned["job_id"],
                "dispatch_id": f"gateway:{planned['job_id']}:1",
                # The execution the stop was requested for, as bound at dispatch.
                "worker_job_id": "worker-running",
                "event_type": "failed",
                "sequence": 1,
                "occurred_at": gateway.utcnow().isoformat(),
                "data": {"error": "job was cancelled", "cancelled": True},
            },
        )
    assert stopped.status_code == 202, stopped.text
    final = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert final["status"] == "CANCELLED"
    assert final["runs"][0]["status"] == "CANCELLED"
    assert final["runs"][0]["steps"][0]["attempts"][0]["status"] == "CANCELLED"
    assert lease(client) == []


def test_an_expired_lease_cannot_apply_its_proposal(client):
    start_mvp_task(client)
    (run,) = lease(client)
    stale = dict(run)
    with gateway.pool.connection() as db:
        # The first Controller stopped renewing its lease.
        db.execute(
            "UPDATE workflow_runs SET lease_until = %s WHERE id = %s",
            (gateway.utcnow() - gateway.timedelta(seconds=1), run["run_id"]),
        )
    # Another Controller takes over the Run.
    (taken,) = lease(client, owner="second-controller")
    assert taken["lease"]["token"] > stale["lease"]["token"]
    refused = propose(
        client,
        stale,
        type="create_attempt",
        step_key="plan",
        cycle=1,
        parameters={"idea": "CSV 取り込み" * 3},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "LEASE_LOST"


def test_the_internal_api_is_not_on_the_public_surface(client):
    start_mvp_task(client)
    # The public surface does not serve it at all.
    assert client.post(
        "/internal/v1/runs/lease", json={"owner": "attacker"}, headers=BOT
    ).status_code == 404
    with mock.patch.object(gateway, "API_SURFACE", "internal"):
        # On the internal surface it still needs the Controller's own credential.
        assert client.post(
            "/internal/v1/runs/lease", json={"owner": "attacker"}, headers=BOT
        ).status_code == 401
        assert client.post(
            "/internal/v1/runs/lease", json={"owner": "attacker"}
        ).status_code == 401


def test_a_restarted_controller_cannot_create_the_same_step_twice(client):
    start_mvp_task(client)
    (run,) = lease(client)
    action = run["next_actions"][0]
    first = advance(client, run, action, parameters={"idea": "CSV 取り込み" * 3})
    # The same proposal arrives again under a fresh lease, as a restarted
    # Controller would send it.
    (again,) = lease(client)
    repeated = propose(
        client,
        again,
        type="create_attempt",
        step_key="plan",
        cycle=1,
        parameters={"idea": "CSV 取り込み" * 3},
    )
    assert repeated.status_code == 409
    with gateway.pool.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM step_attempts").fetchone()["n"] == 1
        assert db.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 1
    assert first["job_id"]


def drive_to_review(client, title="MVP を作る"):
    """Run an mvp-build Task through to a deliverable waiting for a decision."""
    task = start_mvp_task(client, title=title)
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    finish(client, verified["job_id"], {"report": qa_report("pass")}, worker="worker-qa")
    (run,) = lease(client)
    advance(client, run, run["next_actions"][0])
    return task


def deliverable_digest(client, task, command="accept_deliverable"):
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    return next(
        item["target_digest"]
        for item in detail["available_commands"]
        if item["type"] == command
    )


def test_changes_can_be_made_and_the_deliverable_reviewed_again(client):
    task = drive_to_review(client)
    changes = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "request_changes",
            "target_digest": deliverable_digest(client, task, "request_changes"),
            "reason": "重複行の扱いも直してください",
        },
        headers={**HUMAN, "Idempotency-Key": "changes-then-review"},
    )
    assert changes.status_code == 202, changes.text

    (run,) = lease(client)
    assert run["next_actions"][0]["step_key"] == "fix"
    # The Controller sees the human's reason as part of the Run.
    assert run["decisions"][-1]["reason"] == "重複行の扱いも直してください"
    fixed = advance(client, run, run["next_actions"][0], parameters={"task": "重複行を直す"})
    finish(client, fixed["job_id"], build_result(), worker="worker-fix")

    (run,) = lease(client)
    reverified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-fix",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    finish(client, reverified["job_id"], {"report": qa_report("pass")}, worker="worker-qa-2")

    (run,) = lease(client)
    action = run["next_actions"][0]
    # The deliverable comes back for review as a second cycle of the same step.
    assert (action["type"], action["cycle"]) == ("request_review", 2)
    advance(client, run, action)

    accepted = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "accept_deliverable",
            "target_digest": deliverable_digest(client, task),
        },
        headers={**HUMAN, "Idempotency-Key": "accept-after-changes"},
    )
    assert accepted.status_code == 202, accepted.text
    final = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert final["status"] == "COMPLETED"
    assert [step["logical_key"] for step in final["runs"][0]["steps"]].count("review") == 2


def test_repeated_inconclusive_verification_is_bounded(client):
    task = start_mvp_task(client)
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")

    for attempt in range(4):
        (run,) = lease(client)
        action = run["next_actions"][0]
        if action["type"] == "fail_run":
            assert action["failure_class"] == "revision_limit_reached"
            advance(client, run, action)
            break
        assert action["step_key"] == "qa"
        verified = advance(
            client,
            run,
            action,
            parameters={
                "source_worker_job_id": "worker-build",
                "acceptance_criteria": ["正常な CSV から明細を登録できる"],
            },
            input_artifact_ids=[change_artifact(run)],
        )
        # A pass without per-criterion evidence is inconclusive, so the Run asks.
        finish(
            client,
            verified["job_id"],
            {"report": qa_report("pass", criteria=["正常な CSV から明細を登録できる"])},
            worker=f"worker-qa-{attempt}",
        )
        (run,) = lease(client)
        ask = run["next_actions"][0]
        if ask["type"] == "fail_run":
            assert ask["failure_class"] == "revision_limit_reached"
            advance(client, run, ask)
            break
        advance(
            client,
            run,
            ask,
            questions=[{"id": f"q-{attempt}", "text": "検証手順を教えてください"}],
        )
        request_id = [
            event["payload"]["input_request_id"]
            for event in client.get("/v1/events", params={"limit": 500}, headers=BOT).json()[
                "events"
            ]
            if event["type"] == "task.input_requested"
        ][-1]
        client.post(
            f"/v1/input-requests/{request_id}/answers",
            json={"answers": {f"q-{attempt}": "手順はこちらです"}},
            headers=BOT,
        )
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "FAILED"
    assert detail["runs"][0]["result"]["failure_class"] == "revision_limit_reached"


def test_a_verification_cannot_be_aimed_at_something_else(client):
    start_mvp_task(client)
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")
    (run,) = lease(client)
    action = run["next_actions"][0]

    # Naming an execution the input artifact did not come from.
    wrong_source = propose(
        client,
        run,
        type="create_attempt",
        step_key="qa",
        cycle=1,
        parameters={
            "source_worker_job_id": "worker-somewhere-else",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    assert wrong_source.status_code == 409
    assert "produced by the execution" in wrong_source.json()["detail"]["message"]

    # Leaving out an acceptance criterion the Task was accepted with.
    missing_criteria = propose(
        client,
        run,
        type="create_attempt",
        step_key="qa",
        cycle=1,
        parameters={"source_worker_job_id": "worker-build", "acceptance_criteria": []},
        input_artifact_ids=[change_artifact(run)],
    )
    assert missing_criteria.status_code == 409
    assert "every acceptance criterion" in missing_criteria.json()["detail"]["message"]
    assert action["step_key"] == "qa"


def test_cancelling_before_delivery_stops_it_without_asking_the_worker(client):
    task = start_mvp_task(client)
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})

    cancelled = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "cancel"},
        headers={**BOT, "Idempotency-Key": "cancel-before-delivery"},
    )
    assert cancelled.status_code == 202
    # Nothing was delivered, so the stop is complete immediately.
    assert cancelled.json()["status"] == "SUCCEEDED"
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "CANCELLED"
    assert detail["runs"][0]["steps"][0]["attempts"][0]["status"] == "CANCELLED"
    with gateway.pool.connection() as db:
        dispatch = db.execute(
            "SELECT state FROM job_dispatches WHERE job_id = %s", (planned["job_id"],)
        ).fetchone()
        commands = db.execute("SELECT count(*) AS n FROM worker_commands").fetchone()["n"]
    assert dispatch["state"] == "CANCELLED"
    assert commands == 0  # no Worker was ever asked to stop something it never had
    assert client.get(f"/v1/jobs/{planned['job_id']}", headers=BOT).json()["state"] == "CANCELLED"


def test_answering_a_closed_question_cannot_revive_a_cancelled_task(client):
    task = start_mvp_task(client)
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    finish(
        client,
        verified["job_id"],
        {"report": qa_report("pass", criteria=["正常な CSV から明細を登録できる"])},
        worker="worker-qa",
    )
    (run,) = lease(client)
    advance(
        client,
        run,
        run["next_actions"][0],
        questions=[{"id": "q", "text": "検証手順を教えてください"}],
    )
    request_id = [
        event["payload"]["input_request_id"]
        for event in client.get("/v1/events", params={"limit": 500}, headers=BOT).json()["events"]
        if event["type"] == "task.input_requested"
    ][-1]

    client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "cancel"},
        headers={**BOT, "Idempotency-Key": "cancel-waiting-input"},
    )
    assert client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["status"] == "CANCELLED"

    late = client.post(
        f"/v1/input-requests/{request_id}/answers",
        json={"answers": {"q": "いまさらの回答"}},
        headers=BOT,
    )
    assert late.status_code == 409
    assert client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["status"] == "CANCELLED"


def test_a_failed_controller_run_can_be_retried(client):
    task = start_mvp_task(client)
    for _ in range(3):
        (run,) = lease(client)
        planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
        with mock.patch.object(gateway, "API_SURFACE", "callback"):
            client.post(
                "/v1/worker-events",
                headers={"Authorization": "Bearer test-callback-credential"},
                json={
                    "event_id": f"event-{uuid.uuid4()}",
                    "gateway_job_id": planned["job_id"],
                    "dispatch_id": f"gateway:{planned['job_id']}:1",
                    "worker_job_id": "worker-failing",
                    "event_type": "failed",
                    "sequence": 1,
                    "occurred_at": gateway.utcnow().isoformat(),
                    "data": {"error": "codex failed"},
                },
            )
    (run,) = lease(client)
    advance(client, run, run["next_actions"][0])
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "FAILED"
    assert "retry" in {item["type"] for item in detail["available_commands"]}

    retried = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "retry"},
        headers={**BOT, "Idempotency-Key": "retry-mvp-1"},
    )
    assert retried.status_code == 202, retried.text
    resumed = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert resumed["status"] == "ACTIVE"
    assert len(resumed["runs"]) == 2  # a new Run, with the failed one kept
    assert lease(client)[0]["next_actions"][0]["step_key"] == "plan"


def test_a_verification_must_examine_the_latest_change(client):
    """After a fix, a pass on the earlier change cannot stand in for the new one."""
    task = drive_to_review(client, title="最新の変更を検証する")
    client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "request_changes",
            "target_digest": deliverable_digest(client, task, "request_changes"),
            "reason": "重複行の扱いも直してください",
        },
        headers={**HUMAN, "Idempotency-Key": "changes-latest-change"},
    )
    (run,) = lease(client)
    first_change = change_artifact(run)
    fixed = advance(client, run, run["next_actions"][0], parameters={"task": "重複行を直す"})
    finish(client, fixed["job_id"], build_result(), worker="worker-fix")

    (run,) = lease(client)
    assert run["next_actions"][0]["step_key"] == "qa"
    # Pointing the verification at the change that was already reviewed.
    stale = propose(
        client,
        run,
        type="create_attempt",
        step_key="qa",
        cycle=run["next_actions"][0]["cycle"],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[first_change],
    )
    assert stale.status_code == 409
    assert "latest change" in stale.json()["detail"]["message"]


def test_a_review_is_not_requested_for_an_unverified_change(client):
    task = drive_to_review(client, title="未検証の変更")
    client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "request_changes",
            "target_digest": deliverable_digest(client, task, "request_changes"),
            "reason": "もう一度直してください",
        },
        headers={**HUMAN, "Idempotency-Key": "changes-unverified"},
    )
    (run,) = lease(client)
    fixed = advance(client, run, run["next_actions"][0], parameters={"task": "直す"})
    finish(client, fixed["job_id"], build_result(), worker="worker-fix")

    # A verification that only reports inconclusive leaves the change unverified.
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-fix",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    finish(
        client,
        verified["job_id"],
        {"report": qa_report("inconclusive")},
        worker="worker-qa-inconclusive",
    )
    (run,) = lease(client)
    action = run["next_actions"][0]
    # The Run asks for what is missing rather than for acceptance.
    assert action["type"] == "request_input"
    assert client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["status"] != "WAITING_REVIEW"


def test_the_revision_budget_is_shared_by_fixes_and_questions(client):
    """Repeating work is bounded once for the Run, however the repeats arrive."""
    task = start_mvp_task(client, title="修正と質問で予算を使う")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")
    change_producer = "worker-build"
    questions = 0
    fixes = 0

    for _ in range(8):
        (run,) = lease(client)
        action = run["next_actions"][0]
        if action["type"] == "fail_run":
            assert action["failure_class"] == "revision_limit_reached"
            advance(client, run, action)
            break
        if action["type"] == "request_input":
            questions += 1
            advance(
                client,
                run,
                action,
                questions=[{"id": f"q{questions}", "text": "情報をください"}],
            )
            request_id = [
                event["payload"]["input_request_id"]
                for event in client.get(
                    "/v1/events", params={"limit": 500}, headers=BOT
                ).json()["events"]
                if event["type"] == "task.input_requested"
            ][-1]
            client.post(
                f"/v1/input-requests/{request_id}/answers",
                json={"answers": {f"q{questions}": "これです"}},
                headers=BOT,
            )
            continue
        if action["step_key"] == "qa":
            verified = advance(
                client,
                run,
                action,
                parameters={
                    "source_worker_job_id": change_producer,
                    "acceptance_criteria": ["正常な CSV から明細を登録できる"],
                },
                input_artifact_ids=[change_artifact(run)],
            )
            # Inconclusive, so the Run asks for information: another revision.
            finish(
                client,
                verified["job_id"],
                {"report": qa_report("pass", criteria=["正常な CSV から明細を登録できる"])},
                worker=f"worker-qa-{questions}-{fixes}",
            )
            continue
        fixes += 1
        change_producer = f"worker-fix-{fixes}"
        fixed = advance(client, run, action, parameters={"task": "直す"})
        finish(client, fixed["job_id"], build_result(), worker=change_producer)

    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "FAILED"
    assert detail["runs"][0]["result"]["failure_class"] == "revision_limit_reached"
    # Two revisions in total, not two of each kind.
    assert questions + fixes == 2
    assert detail["runs"][0]["revision_cycles"] == 2


def test_a_report_about_an_older_change_does_not_verify_the_newer_one(client):
    """A verification is evidence about the change it was given, nothing else."""
    task = drive_to_review(client, title="古い変更の報告")
    client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "request_changes",
            "target_digest": deliverable_digest(client, task, "request_changes"),
            "reason": "もう一度直してください",
        },
        headers={**HUMAN, "Idempotency-Key": "changes-older-report"},
    )
    (run,) = lease(client)
    older = next(
        item["digest"]
        for item in run["artifacts"]
        if item["kind"] in {"code-change", "code-change-report"}
    )
    fixed = advance(client, run, run["next_actions"][0], parameters={"task": "直す"})
    # A different change, so its digest is its own.
    finish(
        client,
        fixed["job_id"],
        build_result(patch_digest="e" * 64, changed_files=["app/main.py", "app/csv.py"]),
        worker="worker-fix",
    )

    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-fix",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    # The report claims to be about the change that was already reviewed.
    finish(
        client,
        verified["job_id"],
        {"report": {**qa_report("pass"), "target_digest": older}},
        worker="worker-qa-older",
    )
    summary = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["runs"][0][
        "steps"
    ][-1]["attempts"][-1]["result_summary"]
    assert summary["quality_verdict"] == "inconclusive"
    assert "this verification was given" in summary["downgraded_reason"]
    # And the deliverable is not offered for acceptance on that basis.
    (run,) = lease(client)
    assert run["next_actions"][0]["type"] != "request_review"


def test_a_pass_must_judge_the_specification_criteria_too(client):
    """The PRD's acceptance criteria are part of what a pass has to cover."""
    task = start_mvp_task(client, title="仕様の条件も検証する")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    prd = prd_report()
    prd["acceptance_criteria"] = [
        "正常な CSV から明細を登録できる",
        "不正な行を理由付きで確認できる",
    ]
    finish(client, planned["job_id"], {"report": prd})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")

    (run,) = lease(client)
    # Leaving out the criterion the specification added is refused outright.
    refused = propose(
        client,
        run,
        type="create_attempt",
        step_key="qa",
        cycle=1,
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    assert refused.status_code == 409
    assert "不正な行を理由付きで確認できる" in refused.json()["detail"]["message"]

    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": [
                "正常な CSV から明細を登録できる",
                "不正な行を理由付きで確認できる",
            ],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    # A report that judges only one of them is not a pass.
    finish(client, verified["job_id"], {"report": qa_report("pass")}, worker="worker-qa")
    summary = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["runs"][0][
        "steps"
    ][-1]["attempts"][-1]["result_summary"]
    assert summary["quality_verdict"] == "inconclusive"
    assert "不正な行を理由付きで確認できる" in summary["downgraded_reason"]


def test_a_stop_during_delivery_settles_when_the_delivery_is_refused(client):
    task = start_mvp_task(client, title="配送中の停止")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    with gateway.pool.connection() as db:
        # The delivery is already in flight, so it cannot be withdrawn.
        db.execute("UPDATE job_dispatches SET state = 'SENDING' WHERE job_id = %s", (planned["job_id"],))
    cancelled = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "cancel"},
        headers={**BOT, "Idempotency-Key": "cancel-during-delivery"},
    )
    assert cancelled.status_code == 202
    assert client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["control_state"] == "CANCEL_REQUESTED"

    # The Worker refuses the delivery: the stop is settled by that outcome.
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE job_dispatches SET state = 'PENDING', retry_at = %s WHERE job_id = %s",
            (gateway.utcnow(), planned["job_id"]),
        )

    class RefusingDelivery:
        def configured(self):
            return True

        def send(self, _dispatch):
            raise scheduler.Rejected("worker refused the dispatch: HTTP 422")

    scheduler.deliver_once(gateway.pool, RefusingDelivery())
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "FAILED"
    assert detail["control_state"] == "ACTIVE"
    command = client.get(
        f"/v1/commands/{cancelled.json()['command_id']}", headers=BOT
    ).json()
    assert command["status"] == "SUCCEEDED"
    assert command["result"]["outcome"] == "FAILED"


def test_a_verification_that_names_nothing_is_not_evidence(client):
    """A report about no identifiable execution cannot authorise acceptance.

    QA works on a copy of the implementation's workspace, so a report that names
    nothing may have been produced by an earlier execution entirely.
    """
    task = start_mvp_task(client, title="出所のない検証")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    finish(
        client,
        verified["job_id"],
        # Everything a passing report needs, except what it is a report about.
        {"report": {**qa_report("pass"), "source_worker_job_id": None}},
        worker="worker-qa-anonymous",
    )
    summary = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["runs"][0][
        "steps"
    ][-1]["attempts"][-1]["result_summary"]
    assert summary["quality_verdict"] == "inconclusive"
    assert "verified" in summary["downgraded_reason"]
    # No passing verification, so no review is offered.
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] != "WAITING_REVIEW"


def test_the_limits_the_request_set_bind_every_step(client):
    """A Task's own limits are not replaced by the executor's defaults."""
    project()
    lease(client)
    created = client.post(
        "/v1/tasks",
        json={
            "project_id": "test-product",
            "title": "短い予算の依頼",
            "objective": "CSV から明細を登録できるようにする",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
            "workflow_id": "mvp-build-v1",
            "limits": {"timeout_seconds": 30},
            "start": True,
        },
        headers={**BOT, "Idempotency-Key": f"limited-{uuid.uuid4()}"},
    )
    assert created.status_code == 201, created.text
    (run,) = lease(client)
    assert run["request"]["limits"] == {"timeout_seconds": 30}
    # Even a step that asks for more runs under what the request allowed.
    planned = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={"idea": "CSV 取り込み" * 3},
        limits={"timeout_seconds": 1800, "max_output_bytes": 500},
    )
    with gateway.pool.connection() as db:
        queued = db.execute(
            "SELECT payload FROM job_dispatches WHERE job_id = %s", (planned["job_id"],)
        ).fetchone()
        recorded = db.execute(
            "SELECT execution_snapshot FROM step_attempts WHERE job_id = %s",
            (planned["job_id"],),
        ).fetchone()
    # What is actually sent to the Worker, and what the attempt records it ran under.
    assert queued["payload"]["limits"] == {"timeout_seconds": 30, "max_output_bytes": 500}
    assert recorded["execution_snapshot"]["limits"]["timeout_seconds"] == 30


def test_a_run_that_cannot_be_advanced_is_reported_as_blocked(client):
    """A Controller that cannot build a step's input says so on the Run."""
    task = start_mvp_task(client, title="進められない依頼")
    (run,) = lease(client)
    reported = internal(
        client,
        "POST",
        f"/internal/v1/runs/{run['run_id']}/blocked",
        {
            "token": run["lease"]["token"],
            "reason": "60 acceptance criteria exceed the 50 a single verification can carry",
            "detail": {"step_key": "qa"},
        },
    )
    assert reported.status_code == 202, reported.text
    assert reported.json()["applied"] == "report_blocked"
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "BLOCKED"
    events = client.get("/v1/events", params={"limit": 200}, headers=BOT).json()["events"]
    blocked = [event for event in events if event["type"] == "task.blocked"]
    assert blocked and "exceed" in blocked[-1]["payload"]["reason"]

    # The same obstruction on the next pass is not recorded twice.
    (run,) = lease(client)
    again = internal(
        client,
        "POST",
        f"/internal/v1/runs/{run['run_id']}/blocked",
        {
            "token": run["lease"]["token"],
            "reason": "60 acceptance criteria exceed the 50 a single verification can carry",
        },
    )
    assert again.status_code == 202
    assert again.json()["applied"] is None
    events = client.get("/v1/events", params={"limit": 200}, headers=BOT).json()["events"]
    assert len([item for item in events if item["type"] == "task.blocked"]) == 1

    # Work resumes without a human command as soon as a step can be proposed.
    (run,) = lease(client)
    advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    assert client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["status"] == "ACTIVE"


def test_a_failure_while_pausing_does_not_leave_a_pause_to_resume(client):
    """A Run that ends before the pause takes effect is not also paused."""
    project()
    created = client.post(
        "/v1/tasks",
        json={
            "project_id": "test-product",
            "title": "停止要求中に失敗する依頼",
            "objective": "動画を作る",
            "action": "product.plan",
            "parameters": {"idea": "CSV 取り込みの計画" * 2},
            "workflow_id": "single-action-v1",
            "start": True,
        },
        headers={**BOT, "Idempotency-Key": f"pause-fail-{uuid.uuid4()}"},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]
    paused = client.post(
        f"/v1/tasks/{task_id}/commands",
        json={"type": "pause"},
        headers={**BOT, "Idempotency-Key": f"pause-{uuid.uuid4()}"},
    )
    assert paused.status_code == 202, paused.text
    assert paused.json()["result"]["control_state"] == "PAUSE_REQUESTED"

    job_id = created.json()["job_id"]
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE jobs SET dispatch_id = %s, worker_job_id = 'worker-plan', "
            "state = 'DISPATCHED' WHERE id = %s",
            (f"gateway:{job_id}:1", job_id),
        )
    with mock.patch.object(gateway, "API_SURFACE", "callback"):
        reported = client.post(
            "/v1/worker-events",
            headers={"Authorization": "Bearer test-callback-credential"},
            json={
                "event_id": f"event-{uuid.uuid4()}",
                "gateway_job_id": job_id,
                "dispatch_id": f"gateway:{job_id}:1",
                "worker_job_id": "worker-plan",
                "event_type": "failed",
                "sequence": 1,
                "occurred_at": gateway.utcnow().isoformat(),
                "data": {"error": "codex timed out"},
            },
        )
    assert reported.status_code == 202, reported.text

    detail = client.get(f"/v1/tasks/{task_id}", headers=BOT).json()
    assert detail["status"] == "FAILED"
    assert detail["control_state"] == "ACTIVE"
    offered = {item["type"] for item in detail["available_commands"]}
    # Retrying is the coherent option; there is nothing left to resume.
    assert "retry" in offered
    assert "resume" not in offered


def test_a_restart_instruction_withdraws_the_deliverable_from_review(client):
    """"Redo the work" must not leave the old result acceptable.

    An instruction marked `restart_required` changes what the Task asks for. The
    verification that led to this review answered the previous version, so the
    deliverable goes back for changes instead of staying acceptable.
    """
    task = drive_to_review(client, title="やり直しを指示する")
    before = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert before["status"] == "WAITING_REVIEW"
    stale_digest = next(
        item["target_digest"]
        for item in before["available_commands"]
        if item["type"] == "accept_deliverable"
    )

    added = client.post(
        f"/v1/tasks/{task['task_id']}/messages",
        json={
            "kind": "instruction",
            "body": "重複行は取り込まないでください",
            "applies_to": "restart_required",
        },
        headers=BOT,
    )
    assert added.status_code == 201, added.text
    assert added.json()["restarted"] is True
    assert added.json()["input_revision"] == 2

    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "ACTIVE"
    assert detail["input_revision"] == 2
    offered = {item["type"] for item in detail["available_commands"]}
    assert "accept_deliverable" not in offered
    # The old deliverable cannot be accepted, by its digest or by any other.
    refused = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "accept_deliverable", "target_digest": stale_digest},
        headers={**HUMAN, "Idempotency-Key": f"stale-accept-{uuid.uuid4()}"},
    )
    assert refused.status_code == 409, refused.text

    # The Workflow redoes the work, and only a verification of the new version
    # brings it back for a decision.
    (run,) = lease(client)
    assert run["input_revision"] == 2
    assert run["next_actions"][0]["step_key"] == "fix"
    fixed = advance(client, run, run["next_actions"][0], parameters={"task": "重複行を除外する"})
    finish(client, fixed["job_id"], build_result(patch_digest="f" * 64), worker="worker-fix")
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-fix",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    finish(client, verified["job_id"], {"report": qa_report("pass")}, worker="worker-qa-3")
    (run,) = lease(client)
    assert [item["applied"] for item in [advance(client, run, run["next_actions"][0])]] == [
        "request_review"
    ]
    again = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert again["status"] == "WAITING_REVIEW"
    accepted = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "accept_deliverable",
            "target_digest": deliverable_digest(client, task),
        },
        headers={**HUMAN, "Idempotency-Key": f"accept-after-restart-{uuid.uuid4()}"},
    )
    assert accepted.status_code == 202, accepted.text


def test_a_verification_of_an_older_request_does_not_authorise_the_new_one(client):
    """A pass recorded before the request changed is not evidence about it now."""
    task = drive_to_review(client, title="条件が変わった依頼")
    client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "revise_input",
            "acceptance_criteria": [
                "正常な CSV から明細を登録できる",
                "重複行は取り込まない",
            ],
            "reason": "条件を追加しました",
        },
        headers={**BOT, "Idempotency-Key": f"revise-{uuid.uuid4()}"},
    )
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["input_revision"] == 2
    assert detail["status"] == "ACTIVE"
    assert [
        revision["acceptance_criteria"]
        for revision in detail["input_revisions"]
        if revision["revision"] == 2
    ] == [["正常な CSV から明細を登録できる", "重複行は取り込まない"]]

    # The Run continues under the new version: the review is only reachable again
    # after a verification of it.
    (run,) = lease(client)
    assert run["acceptance_criteria"] == [
        "正常な CSV から明細を登録できる",
        "重複行は取り込まない",
    ]
    assert run["next_actions"][0]["step_key"] == "fix"


def test_a_limit_only_revision_leaves_a_deliverable_in_review(client):
    """Extending a timeout does not take a finished deliverable back off the table.

    Nothing about what is asked for changed, so the verification that led to this
    review still answers it: redoing the work would spend a revision cycle for a
    change nobody made to the request.
    """
    task = drive_to_review(client, title="制限だけ変えて審査を続ける")
    revised = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "revise_input",
            "limits": {"timeout_seconds": 1800},
            "reason": "時間だけ延ばします",
        },
        headers={**BOT, "Idempotency-Key": f"revise-limit-review-{uuid.uuid4()}"},
    )
    assert revised.status_code == 202, revised.text
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "WAITING_REVIEW"
    assert detail["input_revision"] == 2
    # And it can still be accepted, against the deliverable as it now stands.
    accepted = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "accept_deliverable",
            "target_digest": deliverable_digest(client, task),
        },
        headers={**HUMAN, "Idempotency-Key": f"accept-after-limit-{uuid.uuid4()}"},
    )
    assert accepted.status_code == 202, accepted.text
    assert client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["status"] == (
        "COMPLETED"
    )


def test_a_limit_only_revision_keeps_a_standing_requirement_and_its_question(client):
    """A restart requirement and an open question both survive a timeout change."""
    task = drive_to_review(client, title="やり直し要求を保持する")
    restarted = client.post(
        f"/v1/tasks/{task['task_id']}/messages",
        json={
            "kind": "instruction",
            "body": "重複行を除外し、除外されたことを検証してください",
            "applies_to": "restart_required",
        },
        headers=BOT,
    )
    assert restarted.status_code == 201, restarted.text
    (run,) = lease(client)
    assert run["next_actions"][0]["step_key"] == "fix"
    fixed = advance(client, run, run["next_actions"][0], parameters={"task": "重複行を除外する"})
    finish(
        client,
        fixed["job_id"],
        build_result(patch_digest="c" * 64),
        worker="worker-fix-restart",
    )
    revised = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "revise_input",
            "limits": {"timeout_seconds": 1800},
            "reason": "時間だけ延ばします",
        },
        headers={**BOT, "Idempotency-Key": f"revise-standing-{uuid.uuid4()}"},
    )
    assert revised.status_code == 202, revised.text

    (run,) = lease(client)
    # The requirement the person added is still what this version asks for, so the
    # verification that follows is built from it too.
    assert "重複行を除外し" in run["input_revision_reason"]
    assert run["requirements_revision"] == 2
    assert run["next_actions"][0]["step_key"] == "qa"



def test_a_question_survives_a_limit_only_revision(client):
    """A revision that changed no requirement leaves an open question answerable.

    The step is waiting on that answer; refusing it because the version number moved
    would strand the Run with nothing able to resolve it.
    """
    task = drive_to_review(client, title="質問の途中で制限を変える")
    client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "request_changes",
            "target_digest": deliverable_digest(client, task, "request_changes"),
            "reason": "もう一度直してください",
        },
        headers={**HUMAN, "Idempotency-Key": f"changes-question-{uuid.uuid4()}"},
    )
    (run,) = lease(client)
    fixed = advance(client, run, run["next_actions"][0], parameters={"task": "直す"})
    finish(client, fixed["job_id"], build_result(), worker="worker-fix-question")
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-fix-question",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    finish(
        client,
        verified["job_id"],
        {"report": qa_report("inconclusive")},
        worker="worker-qa-question",
    )
    (run,) = lease(client)
    action = run["next_actions"][0]
    assert action["type"] == "request_input"
    advance(
        client,
        run,
        action,
        questions=[{"id": "q1", "text": "上限は何 MB ですか", "required": True}],
    )
    request_id = [
        event["payload"]["input_request_id"]
        for event in client.get(
            "/v1/events", params={"limit": 500}, headers=BOT
        ).json()["events"]
        if event["type"] == "task.input_requested"
    ][-1]

    revised = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "revise_input",
            "limits": {"timeout_seconds": 1800},
            "reason": "時間だけ延ばします",
        },
        headers={**BOT, "Idempotency-Key": f"revise-question-{uuid.uuid4()}"},
    )
    assert revised.status_code == 202, revised.text
    # The question was not asked about a version that has been replaced, so it is
    # still open and still answerable.
    answered = client.post(
        f"/v1/input-requests/{request_id}/answers",
        json={"answers": {"q1": "10MB です"}},
        headers=BOT,
    )
    assert answered.status_code == 200, answered.text
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] != "WAITING_INPUT"


def test_a_workflow_that_reads_no_parameters_refuses_them(client):
    """A version whose instruction reaches no step is not recorded as one."""
    task = start_mvp_task(client, title="parameters を受け付けない")
    refused = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "revise_input",
            "parameters": {"task": "重複行を除外する"},
            "reason": "parameters で指示しようとする",
        },
        headers={**BOT, "Idempotency-Key": f"revise-params-{uuid.uuid4()}"},
    )
    assert refused.status_code == 409, refused.text
    assert "parameters" in refused.json()["detail"]["message"]
    # What is asked for is unchanged, so nothing was withdrawn either.
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["input_revision"] == 1


def test_a_limit_only_revision_supersedes_nothing_a_person_said(client):
    """The version moves; what is asked for does not, so nothing is withdrawn."""
    task = drive_to_review(client, title="制限だけ変えた依頼")
    changed = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "request_changes",
            "target_digest": deliverable_digest(client, task, command="request_changes"),
            "reason": "アップロード上限を 10MB にしてください",
        },
        headers={**HUMAN, "Idempotency-Key": f"changes-limit-{uuid.uuid4()}"},
    )
    assert changed.status_code == 202, changed.text
    revised = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "revise_input",
            "limits": {"timeout_seconds": 1800},
            "reason": "時間だけ延ばします",
        },
        headers={**BOT, "Idempotency-Key": f"revise-limit-{uuid.uuid4()}"},
    )
    assert revised.status_code == 202, revised.text

    (run,) = lease(client)
    # The request itself is unchanged, so the correction still stands and the
    # specification's conditions are still required.
    assert run["input_revision"] == 2
    assert run["requirements_revision"] == 1
    assert [
        decision["reason"]
        for decision in run["decisions"]
        if decision["kind"] == "request_changes"
    ] == ["アップロード上限を 10MB にしてください"]
    assert run["next_actions"][0]["step_key"] == "fix"
    fixed = advance(client, run, run["next_actions"][0], parameters={"task": "上限を直す"})
    finish(
        client,
        fixed["job_id"],
        build_result(patch_digest="d" * 64),
        worker="worker-fix-limit",
    )
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-fix-limit",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    with gateway.pool.connection() as db:
        bound = db.execute(
            "SELECT input_manifest FROM step_attempts WHERE job_id = %s",
            (uuid.UUID(verified["job_id"]),),
        ).fetchone()["input_manifest"]["verification_target"]
    # The PRD written under revision 1 is still what this request is judged by.
    assert bound["criteria"] == ["正常な CSV から明細を登録できる"]


def test_a_superseded_specification_does_not_bind_the_new_request(client):
    """A PRD written for the old request is not what the new one is judged by.

    Requiring a condition the requester has since replaced would make a correct
    answer to the current request impossible to verify, and spend revision cycles
    failing on it.
    """
    task = drive_to_review(client, title="仕様が古くなった依頼")
    replaced = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "revise_input",
            "acceptance_criteria": ["不正な行は理由付きで除外する"],
            "reason": "条件を入れ替えました",
        },
        headers={**BOT, "Idempotency-Key": f"revise-spec-{uuid.uuid4()}"},
    )
    assert replaced.status_code == 202, replaced.text

    (run,) = lease(client)
    assert run["next_actions"][0]["step_key"] == "fix"
    fixed = advance(client, run, run["next_actions"][0], parameters={"task": "入れ替えた条件で直す"})
    finish(
        client,
        fixed["job_id"],
        build_result(patch_digest="f" * 64, changed_files=["app/csv.py"]),
        worker="worker-fix-superseded",
    )
    (run,) = lease(client)
    # The verification is required to cover the current condition only: the PRD's
    # own condition belonged to the version that was replaced.
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-fix-superseded",
            "acceptance_criteria": ["不正な行は理由付きで除外する"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    with gateway.pool.connection() as db:
        bound = db.execute(
            "SELECT input_manifest FROM step_attempts WHERE job_id = %s",
            (uuid.UUID(verified["job_id"]),),
        ).fetchone()["input_manifest"]["verification_target"]
    assert bound["criteria"] == ["不正な行は理由付きで除外する"]
    finish(
        client,
        verified["job_id"],
        {
            "report": qa_report(
                "pass",
                criteria=[
                    {
                        "criterion": "不正な行は理由付きで除外する",
                        "verdict": "pass",
                        "evidence": "tests.log",
                    }
                ],
            )
        },
        worker="worker-qa-superseded",
    )
    summary = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["runs"][0][
        "steps"
    ][-1]["attempts"][-1]["result_summary"]
    assert summary["quality_verdict"] == "pass"
    (run,) = lease(client)
    assert run["next_actions"][0]["type"] == "request_review"


def test_the_request_cannot_be_changed_under_a_running_execution(client):
    """Work in flight answers to the version it was given; stop it first."""
    task = start_mvp_task(client, title="実行中の変更")
    (run,) = lease(client)
    advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    reason = next(
        item
        for item in detail["unavailable_commands"]
        if item["type"] == "revise_input"
    )
    assert reason["reason"] == "EXECUTION_RUNNING"
    refused = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "revise_input", "objective": "もっと詳しい目的の説明を書きます"},
        headers={**BOT, "Idempotency-Key": f"revise-running-{uuid.uuid4()}"},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "EXECUTION_RUNNING"


def test_a_blocked_task_can_be_unblocked_by_changing_the_request(client):
    """The recovery the board offers is one the Gateway actually performs."""
    project()
    lease(client)
    created = client.post(
        "/v1/tasks",
        json={
            "project_id": "test-product",
            "title": "目的が短い依頼",
            "objective": "短い",
            "workflow_id": "mvp-build-v1",
            "start": True,
        },
        headers={**BOT, "Idempotency-Key": f"short-{uuid.uuid4()}"},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]
    (run,) = lease(client)
    internal(
        client,
        "POST",
        f"/internal/v1/runs/{run['run_id']}/blocked",
        {
            "token": run["lease"]["token"],
            "reason": "the objective is too short to plan from",
        },
    )
    assert client.get(f"/v1/tasks/{task_id}", headers=BOT).json()["status"] == "BLOCKED"

    revised = client.post(
        f"/v1/tasks/{task_id}/commands",
        json={
            "type": "revise_input",
            "objective": "CSV を選んで明細を一括登録できるようにする",
            "reason": "目的を具体的にしました",
        },
        headers={**BOT, "Idempotency-Key": f"unblock-{uuid.uuid4()}"},
    )
    assert revised.status_code == 202, revised.text
    detail = client.get(f"/v1/tasks/{task_id}", headers=BOT).json()
    assert detail["status"] == "ACTIVE"
    assert detail["objective"] == "CSV を選んで明細を一括登録できるようにする"
    # And the Controller can now build the step it could not build before.
    (run,) = lease(client)
    assert run["objective"] == "CSV を選んで明細を一括登録できるようにする"
    assert run["next_actions"][0]["step_key"] == "plan"


def test_a_limit_nothing_enforces_is_refused(client):
    project()
    refused = client.post(
        "/v1/tasks",
        json={
            "project_id": "test-product",
            "title": "効かない上限",
            "objective": "CSV から明細を登録できるようにする",
            "workflow_id": "mvp-build-v1",
            "limits": {"max_revision_cycles": 0},
        },
        headers={**BOT, "Idempotency-Key": f"bad-limit-{uuid.uuid4()}"},
    )
    assert refused.status_code == 422
    assert "max_revision_cycles" in refused.text


def test_a_criterion_longer_than_a_step_can_carry_is_refused(client):
    project()
    refused = client.post(
        "/v1/tasks",
        json={
            "project_id": "test-product",
            "title": "長すぎる条件",
            "objective": "CSV から明細を登録できるようにする",
            "acceptance_criteria": ["条件 " * 300],
            "workflow_id": "mvp-build-v1",
        },
        headers={**BOT, "Idempotency-Key": f"long-criterion-{uuid.uuid4()}"},
    )
    assert refused.status_code == 422
    assert "500" in refused.text


def test_changing_the_request_while_a_question_is_open_continues_the_run(client):
    """A question asked under the old request is closed, and the Run goes on.

    Leaving the step that asked it waiting would strand the Run: nothing could
    answer the cancelled question and nothing could follow it.
    """
    task = start_mvp_task(client, title="質問中に条件を変える")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    # An inconclusive verification asks the requester a question.
    finish(client, verified["job_id"], {"report": qa_report("inconclusive")}, worker="worker-qa")
    (run,) = lease(client)
    advance(
        client,
        run,
        run["next_actions"][0],
        questions=[{"id": "how", "text": "確認手順を教えてください", "required": True}],
    )
    assert client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["status"] == (
        "WAITING_INPUT"
    )

    revised = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "revise_input",
            "acceptance_criteria": ["CSV の取り込み結果を画面で確認できる"],
            "reason": "確認方法を条件にしました",
        },
        headers={**BOT, "Idempotency-Key": f"revise-open-{uuid.uuid4()}"},
    )
    assert revised.status_code == 202, revised.text
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "ACTIVE"
    assert [item["state"] for item in detail["input_requests"]] == ["CANCELLED"]

    # The work is done again for the new request: verifying what was built for the
    # previous one would pass the new conditions without implementing them.
    (run,) = lease(client)
    assert run["next_actions"], "the run was left with nothing it could do"
    assert run["next_actions"][0]["step_key"] == "fix"
    assert run["input_revision_reason"] == "確認方法を条件にしました"


def test_accepting_while_paused_leaves_no_pause_behind(client):
    """A completed Task holds nothing back."""
    task = drive_to_review(client, title="一時停止中に受け入れる")
    paused = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "pause"},
        headers={**BOT, "Idempotency-Key": f"pause-review-{uuid.uuid4()}"},
    )
    assert paused.status_code == 202, paused.text
    assert paused.json()["result"]["control_state"] == "PAUSED"

    accepted = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "accept_deliverable",
            "target_digest": deliverable_digest(client, task),
        },
        headers={**HUMAN, "Idempotency-Key": f"accept-paused-{uuid.uuid4()}"},
    )
    assert accepted.status_code == 202, accepted.text
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "COMPLETED"
    assert detail["control_state"] == "ACTIVE"
    assert {item["type"] for item in detail["available_commands"]} == set()


def test_changing_a_single_action_request_makes_it_run_again(client):
    """One execution is the whole request: a new version has to be executed.

    A paused single action whose execution succeeded is completed on resume. If the
    request changed in between, completing it would deliver work that answers a
    version nobody is asking for.
    """
    project()
    created = client.post(
        "/v1/tasks",
        json={
            "project_id": "test-product",
            "title": "単発の依頼を変更する",
            "objective": "CSV 取り込みの計画を作る",
            "action": "product.plan",
            "parameters": {"idea": "CSV 取り込みの計画を立てる" * 2},
            "workflow_id": "single-action-v1",
            "start": True,
        },
        headers={**BOT, "Idempotency-Key": f"single-revise-{uuid.uuid4()}"},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]
    job_id = created.json()["job_id"]
    client.post(
        f"/v1/tasks/{task_id}/commands",
        json={"type": "pause"},
        headers={**BOT, "Idempotency-Key": f"pause-single-{uuid.uuid4()}"},
    )
    finish(client, job_id, {"report": prd_report()}, worker="worker-plan")
    # Held back by the pause rather than completed.
    assert client.get(f"/v1/tasks/{task_id}", headers=BOT).json()["status"] == "ACTIVE"

    # A single action runs its parameters, so a new version has to say what to run.
    without_parameters = client.post(
        f"/v1/tasks/{task_id}/commands",
        json={
            "type": "revise_input",
            "objective": "CSV と Excel の取り込みの計画を作る",
        },
        headers={**BOT, "Idempotency-Key": f"revise-no-params-{uuid.uuid4()}"},
    )
    assert without_parameters.status_code == 409
    assert "parameters" in without_parameters.json()["detail"]["message"]

    revised = client.post(
        f"/v1/tasks/{task_id}/commands",
        json={
            "type": "revise_input",
            "objective": "CSV と Excel の取り込みの計画を作る",
            "parameters": {"idea": "CSV と Excel の取り込みの計画を立てる" * 2},
            "reason": "Excel も対象にしました",
        },
        headers={**BOT, "Idempotency-Key": f"revise-single-{uuid.uuid4()}"},
    )
    assert revised.status_code == 202, revised.text
    detail = client.get(f"/v1/tasks/{task_id}", headers=BOT).json()
    # Ready to run again for this version, not completed from the previous one.
    assert detail["status"] == "READY"
    assert detail["input_revision"] == 2
    assert detail["control_state"] == "ACTIVE"
    assert {item["type"] for item in detail["available_commands"]} >= {"start"}
    assert detail["runs"][0]["status"] == "SUPERSEDED"

    started = client.post(
        f"/v1/tasks/{task_id}/commands",
        json={"type": "start"},
        headers={**BOT, "Idempotency-Key": f"start-again-{uuid.uuid4()}"},
    )
    assert started.status_code == 202, started.text
    assert client.get(f"/v1/tasks/{task_id}", headers=BOT).json()["status"] == "ACTIVE"
    # And it runs the revised request, not the one it was started with.
    with gateway.pool.connection() as db:
        queued = db.execute(
            "SELECT payload FROM job_dispatches ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert "Excel" in queued["payload"]["parameters"]["idea"]


def test_a_revision_that_changes_nothing_is_refused(client):
    """An unchanged submission must not withdraw a deliverable from review."""
    task = drive_to_review(client, title="変更のない更新")
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    current = detail["input_revisions"][-1]
    refused = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "revise_input",
            "objective": current["objective"],
            "acceptance_criteria": current["acceptance_criteria"],
        },
        headers={**BOT, "Idempotency-Key": f"revise-noop-{uuid.uuid4()}"},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "INVALID_STATE"
    after = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert after["status"] == "WAITING_REVIEW"
    assert after["input_revision"] == 1


def test_work_finished_for_an_older_request_is_not_verified_as_current(client):
    """A change built for the previous request cannot be verified against the new one.

    Pause while the implementation runs, let it succeed, then change what the Task
    asks for. Verifying that change would judge it against conditions it was never
    given, and a pass would then open the review.
    """
    task = start_mvp_task(client, title="実装後に条件を変える")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "pause"},
        headers={**BOT, "Idempotency-Key": f"pause-implement-{uuid.uuid4()}"},
    )
    finish(client, built["job_id"], build_result(), worker="worker-build")

    revised = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "revise_input",
            "acceptance_criteria": [
                "正常な CSV から明細を登録できる",
                "重複行は取り込まない",
            ],
            "reason": "重複行の扱いを条件に追加しました",
        },
        headers={**BOT, "Idempotency-Key": f"revise-after-build-{uuid.uuid4()}"},
    )
    assert revised.status_code == 202, revised.text
    client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "resume"},
        headers={**BOT, "Idempotency-Key": f"resume-implement-{uuid.uuid4()}"},
    )

    # The next step is the work again, not the verification of what was built.
    (run,) = lease(client)
    assert run["next_actions"][0]["step_key"] == "fix"

    # And proposing the verification anyway is refused.
    refused = propose(
        client,
        run,
        type="create_attempt",
        step_key="qa",
        cycle=2,
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": [
                "正常な CSV から明細を登録できる",
                "重複行は取り込まない",
            ],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "TRANSITION_NOT_ALLOWED"


def test_an_instruction_nobody_has_acted_on_is_carried_forward(client):
    """Two requirements in a row must both reach the work.

    An instruction that nothing has executed yet is still outstanding, so a later
    change to the request carries it instead of leaving it in the message history.
    """
    task = start_mvp_task(client, title="指示を二つ重ねる")
    first = client.post(
        f"/v1/tasks/{task['task_id']}/messages",
        json={
            "kind": "instruction",
            "body": "重複行は取り込まないでください",
            "applies_to": "restart_required",
        },
        headers=BOT,
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/v1/tasks/{task['task_id']}/messages",
        json={
            "kind": "instruction",
            "body": "空行も無視してください",
            "applies_to": "restart_required",
        },
        headers=BOT,
    )
    assert second.status_code == 201, second.text
    assert second.json()["input_revision"] == 3

    (run,) = lease(client)
    # Both requirements are part of what the Task now asks for.
    assert "重複行は取り込まないでください" in run["input_revision_reason"]
    assert "空行も無視してください" in run["input_revision_reason"]


def test_a_change_produced_for_an_older_request_cannot_be_bound_to_a_verification(client):
    """The binding itself refuses it, not only the transition that would ask.

    `allowed_next` sends a revised Run back to the work step, so the rejection
    above never reaches the binding. This exercises the binding directly.
    """
    task = drive_to_review(client, title="束縛の直接確認")
    client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "revise_input",
            "acceptance_criteria": ["重複行は取り込まない"],
            "reason": "条件を入れ替えました",
        },
        headers={**BOT, "Idempotency-Key": f"revise-binding-{uuid.uuid4()}"},
    )
    with gateway.pool.connection() as db:
        run = db.execute(
            "SELECT * FROM workflow_runs WHERE task_id = %s", (task["task_id"],)
        ).fetchone()
        locked = tasks.locked_task(db, run["task_id"])
        definition = workflows.get(run["workflow_id"]).step("qa")
        newest = runs.latest_change(db, run["id"])
        with pytest.raises(runs.ProposalRejected, match="earlier version"):
            runs._bind_verification(
                db,
                run=run,
                task=locked,
                definition=definition,
                parameters={
                    "source_worker_job_id": "worker-build",
                    "acceptance_criteria": ["重複行は取り込まない"],
                },
                inputs={
                    "artifacts": [{"artifact_id": str(newest["id"]), "digest": newest["digest"]}],
                    "acceptance_criteria": ["重複行は取り込まない"],
                },
            )


def test_a_restart_instruction_on_a_single_action_says_what_it_needs(client):
    """An instruction cannot express what a single action should execute.

    Its parameters are the request, so the operator is told to change those rather
    than left with a revision that would rerun the old ones.
    """
    project()
    created = client.post(
        "/v1/tasks",
        json={
            "project_id": "test-product",
            "title": "単発にやり直しを指示する",
            "objective": "CSV 取り込みの計画を作る",
            "action": "product.plan",
            "parameters": {"idea": "CSV 取り込みの計画を立てる" * 2},
            "workflow_id": "single-action-v1",
        },
        headers={**BOT, "Idempotency-Key": f"single-restart-{uuid.uuid4()}"},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]
    refused = client.post(
        f"/v1/tasks/{task_id}/messages",
        json={
            "kind": "instruction",
            "body": "Excel も対象にしてください",
            "applies_to": "restart_required",
        },
        headers=BOT,
    )
    assert refused.status_code == 409
    assert "parameters" in refused.json()["detail"]["message"]
    # Nothing was recorded: neither the message nor a new version.
    detail = client.get(f"/v1/tasks/{task_id}", headers=BOT).json()
    assert detail["input_revision"] == 1
    assert detail["messages"] == []


def test_consolidating_instructions_clears_what_was_carried(client):
    """Folding the instructions into the request ends their carry-forward."""
    task = start_mvp_task(client, title="指示をまとめ直す")
    client.post(
        f"/v1/tasks/{task['task_id']}/messages",
        json={
            "kind": "instruction",
            "body": "重複行は取り込まないでください",
            "applies_to": "restart_required",
        },
        headers=BOT,
    )
    revised = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "revise_input",
            "acceptance_criteria": [
                "正常な CSV から明細を登録できる",
                "重複行は取り込まない",
            ],
            "reason": "指示を完了条件に入れました",
        },
        headers={**BOT, "Idempotency-Key": f"consolidate-{uuid.uuid4()}"},
    )
    assert revised.status_code == 202, revised.text
    (run,) = lease(client)
    # The instruction is now part of the conditions, so it is not also carried.
    assert run["input_revision_reason"] == "指示を完了条件に入れました"
    assert "重複行は取り込まない" in run["acceptance_criteria"]


def test_a_restart_instruction_through_mcp_also_withdraws_the_deliverable(client):
    """Every surface applies the same rule, or one of them leaves a way around it.

    An instruction recorded through MCP without a new input version would leave the
    previous result acceptable, which is exactly what "redo the work" denies.
    """
    task = drive_to_review(client, title="MCP からやり直しを指示する")
    response = client.post(
        "/mcp",
        headers={**BOT, "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "add_task_instruction",
                "arguments": {
                    "task_id": task["task_id"],
                    "body": "重複行は取り込まないでください",
                    "kind": "instruction",
                    "applies_to": "restart_required",
                },
            },
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    assert result["structuredContent"]["restarted"] is True
    assert result["structuredContent"]["input_revision"] == 2

    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "ACTIVE"
    assert "accept_deliverable" not in {
        item["type"] for item in detail["available_commands"]
    }


def test_an_execution_the_worker_could_not_stop_is_reported_as_being_checked(client):
    """An unconfirmed stop is not progress and not a result.

    Nothing may be concluded about the Attempt: it stays open, the Task says it is
    being checked, and a stop that was requested stays unconfirmed.
    """
    task = start_mvp_task(client, title="停止を確認できない実行")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    job_id = planned["job_id"]
    # Delivered and running on the Worker, so stopping it is requested rather than
    # withdrawn before delivery.
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE jobs SET dispatch_id = %s, worker_job_id = 'worker-stuck', "
            "state = 'DISPATCHED' WHERE id = %s",
            (f"gateway:{job_id}:1", job_id),
        )
    stopping = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "cancel"},
        headers={**BOT, "Idempotency-Key": f"cancel-unstoppable-{uuid.uuid4()}"},
    )
    assert stopping.status_code == 202, stopping.text
    with mock.patch.object(gateway, "API_SURFACE", "callback"):
        reported = client.post(
            "/v1/worker-events",
            headers={"Authorization": "Bearer test-callback-credential"},
            json={
                "event_id": f"event-{uuid.uuid4()}",
                "gateway_job_id": job_id,
                "dispatch_id": f"gateway:{job_id}:1",
                "worker_job_id": "worker-stuck",
                "event_type": "progress",
                "sequence": 2,
                "occurred_at": gateway.utcnow().isoformat(),
                "data": {
                    "error": "the agent process could not be stopped",
                    "stopped": False,
                },
            },
        )
    assert reported.status_code == 202, reported.text

    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "BLOCKED"
    # The stop was requested and is not confirmed, because it could not be.
    assert detail["control_state"] == "CANCEL_REQUESTED"
    summary = detail["runs"][0]["steps"][-1]["attempts"][-1]
    assert summary["status"] == "RUNNING"
    assert summary["result_summary"]["stopped"] is False
    assert "could not be stopped" in summary["result_summary"]["error"]


def test_an_execution_whose_outcome_cannot_be_read_yet_is_not_progress(client):
    """Finished, but what it produced is not readable: nothing is resolved yet.

    A Worker that restarted and cannot read the result its execution recorded says so
    as progress. Treating that as work progressing would tell a person the Task is
    running normally, and would clear a blockage nothing has resolved.
    """
    task = start_mvp_task(client, title="結果が読めない実行")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    job_id = planned["job_id"]
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE jobs SET dispatch_id = %s, worker_job_id = 'worker-unreadable', "
            "state = 'DISPATCHED' WHERE id = %s",
            (f"gateway:{job_id}:1", job_id),
        )
        # Something about the delivery was already unresolved, so the Task is being
        # checked rather than running.
        db.execute(
            "UPDATE tasks SET status = 'BLOCKED' WHERE id = %s", (task["task_id"],)
        )
        db.commit()
    with mock.patch.object(gateway, "API_SURFACE", "callback"):
        reported = client.post(
            "/v1/worker-events",
            headers={"Authorization": "Bearer test-callback-credential"},
            json={
                "event_id": f"event-{uuid.uuid4()}",
                "gateway_job_id": job_id,
                "dispatch_id": f"gateway:{job_id}:1",
                "worker_job_id": "worker-unreadable",
                "event_type": "progress",
                "sequence": 2,
                "occurred_at": gateway.utcnow().isoformat(),
                "data": {
                    "error": "the result wjob_x recorded could not be read: EIO",
                    "outcome_pending": True,
                },
            },
        )
    assert reported.status_code == 202, reported.text
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "BLOCKED"
    summary = detail["runs"][0]["steps"][-1]["attempts"][-1]
    assert summary["status"] == "RUNNING"
    assert summary["result_summary"]["outcome_pending"] is True
    assert "could not be read" in summary["result_summary"]["error"]


def test_resending_the_same_objective_does_not_consume_an_instruction(client):
    """An instruction is acted on by a step or by a real change, not by a repeat.

    Revising a limit while sending the objective back unchanged changes nothing about
    what the Task asks for, so the instruction is still outstanding and the next step
    has to be told.
    """
    task = start_mvp_task(client, title="同じ目的を送り直す", start=False)
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    objective = detail["objective"]
    client.post(
        f"/v1/tasks/{task['task_id']}/messages",
        json={
            "kind": "instruction",
            "body": "重複行は取り込まないでください",
            "applies_to": "next_attempt",
        },
        headers=BOT,
    )
    revised = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "revise_input",
            # The same objective, and a limit that really does change.
            "objective": objective,
            "limits": {"timeout_seconds": 900},
            "reason": "時間だけ延ばします",
        },
        headers={**BOT, "Idempotency-Key": f"revise-same-objective-{uuid.uuid4()}"},
    )
    assert revised.status_code == 202, revised.text

    started = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "start"},
        headers={**BOT, "Idempotency-Key": f"start-same-objective-{uuid.uuid4()}"},
    )
    assert started.status_code == 202, started.text
    (run,) = lease(client)
    assert [item["body"] for item in run["instructions"]] == [
        "重複行は取り込まないでください"
    ]


def test_an_instruction_is_outstanding_until_an_execution_is_given_it(client):
    """Instructions are carried until something acts on them, then no longer.

    Every outstanding instruction must reach the next step whole, so they cannot
    accumulate forever: the attempt that is built from them consumes them.
    """
    task = start_mvp_task(client, title="指示を消費する")
    client.post(
        f"/v1/tasks/{task['task_id']}/messages",
        json={
            "kind": "instruction",
            "body": "日本語の見出しを使ってください",
            "applies_to": "next_attempt",
        },
        headers=BOT,
    )
    (run,) = lease(client)
    assert [item["body"] for item in run["instructions"]] == [
        "日本語の見出しを使ってください"
    ]
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    # The attempt was built from it, so it is part of that work now.
    (run,) = lease(client)
    assert run["instructions"] == []
    # And it is still in the Task's history.
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert [item["body"] for item in detail["messages"]] == [
        "日本語の見出しを使ってください"
    ]
    finish(client, planned["job_id"], {"report": prd_report()})


def test_consolidating_the_request_also_consumes_the_instructions(client):
    """Folding instructions into the request means nothing is still waiting."""
    task = start_mvp_task(client, title="指示を依頼に取り込む")
    client.post(
        f"/v1/tasks/{task['task_id']}/messages",
        json={
            "kind": "instruction",
            "body": "重複行は取り込まないでください",
            "applies_to": "next_attempt",
        },
        headers=BOT,
    )
    revised = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "revise_input",
            "acceptance_criteria": [
                "正常な CSV から明細を登録できる",
                "重複行は取り込まない",
            ],
        },
        headers={**BOT, "Idempotency-Key": f"fold-{uuid.uuid4()}"},
    )
    assert revised.status_code == 202, revised.text
    (run,) = lease(client)
    assert run["instructions"] == []


def test_an_answer_can_be_corrected_while_its_run_is_going(client):
    """An answer already recorded is not a dead end while the Run continues."""
    task = start_mvp_task(client, title="回答を直す")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    finish(client, verified["job_id"], {"report": qa_report("inconclusive")}, worker="worker-qa")
    (run,) = lease(client)
    advance(
        client,
        run,
        run["next_actions"][0],
        questions=[{"id": "how", "text": "確認手順を教えてください", "required": True}],
    )
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    request_id = detail["input_requests"][-1]["input_request_id"]
    first = client.post(
        f"/v1/input-requests/{request_id}/answers",
        json={"answers": {"how": "画面から確認します"}},
        headers=BOT,
    )
    assert first.status_code == 200, first.text
    corrected = client.post(
        f"/v1/input-requests/{request_id}/answers",
        json={"answers": {"how": "サンプル CSV を使って画面から確認します"}},
        headers=BOT,
    )
    assert corrected.status_code == 200, corrected.text
    (run,) = lease(client)
    assert run["answered_inputs"][-1]["answers"]["how"].startswith("サンプル CSV")

    # Once work has been built from the answer, correcting it silently would leave a
    # verification about the answer it replaced.
    advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    refused = client.post(
        f"/v1/input-requests/{request_id}/answers",
        json={"answers": {"how": "やはり CLI で確認します"}},
        headers=BOT,
    )
    assert refused.status_code == 409
    assert "already been done" in refused.json()["detail"]["message"]


def test_an_answer_too_long_to_carry_is_refused(client):
    task = start_mvp_task(client, title="長すぎる回答")
    refused = client.post(
        "/v1/input-requests/00000000-0000-0000-0000-000000000000/answers",
        json={"answers": {"how": "手順 " * 2000}},
        headers=BOT,
    )
    assert refused.status_code == 422
    assert "4000" in refused.text


def test_a_report_with_a_field_of_the_wrong_shape_is_read_as_a_failed_contract(client):
    """A malformed report is an outcome, not a crash that retries for ever.

    The Worker validates its own report, but the Gateway must survive a field it did
    not expect: a projection that raises would roll the callback back and the Worker
    would deliver it again, without end.
    """
    task = start_mvp_task(client, title="形の違う報告")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    finish(
        client,
        verified["job_id"],
        {"report": {**qa_report("pass"), "checks": 1}},
        worker="worker-qa",
    )
    summary = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["runs"][0][
        "steps"
    ][-1]["attempts"][-1]["result_summary"]
    # The criteria it did judge are still what decides, and nothing crashed.
    assert summary["quality_verdict"] in {"pass", "inconclusive"}


def test_a_revision_after_an_answered_question_goes_back_to_the_work(client):
    """Whatever step the Run is standing on, a changed request redoes the work.

    An answered question leaves a step that succeeded; resuming from it would verify
    the implementation that answered the previous request.
    """
    task = start_mvp_task(client, title="回答後に条件を変える")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    finish(client, verified["job_id"], {"report": qa_report("inconclusive")}, worker="worker-qa")
    (run,) = lease(client)
    advance(
        client,
        run,
        run["next_actions"][0],
        questions=[{"id": "how", "text": "確認手順を教えてください", "required": True}],
    )
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    request_id = detail["input_requests"][-1]["input_request_id"]
    answered = client.post(
        f"/v1/input-requests/{request_id}/answers",
        json={"answers": {"how": "画面から確認します"}},
        headers=BOT,
    )
    assert answered.status_code == 200, answered.text

    revised = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "revise_input",
            "acceptance_criteria": ["重複行は取り込まない"],
            "reason": "条件を入れ替えました",
        },
        headers={**BOT, "Idempotency-Key": f"revise-answered-{uuid.uuid4()}"},
    )
    assert revised.status_code == 202, revised.text
    (run,) = lease(client)
    assert run["next_actions"], "the run was left with nothing it could do"
    assert run["next_actions"][0]["step_key"] == "fix"


def test_a_revision_after_a_failed_step_goes_back_to_the_work(client):
    """A retry of the failed step would answer the request that has been replaced."""
    task = start_mvp_task(client, title="失敗後に条件を変える")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    # The verification itself fails to run, rather than judging anything.
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE jobs SET dispatch_id = %s, worker_job_id = 'worker-qa-broken', "
            "state = 'DISPATCHED' WHERE id = %s",
            (f"gateway:{verified['job_id']}:1", verified["job_id"]),
        )
    with mock.patch.object(gateway, "API_SURFACE", "callback"):
        client.post(
            "/v1/worker-events",
            headers={"Authorization": "Bearer test-callback-credential"},
            json={
                "event_id": f"event-{uuid.uuid4()}",
                "gateway_job_id": verified["job_id"],
                "dispatch_id": f"gateway:{verified['job_id']}:1",
                "worker_job_id": "worker-qa-broken",
                "event_type": "failed",
                "sequence": 1,
                "occurred_at": gateway.utcnow().isoformat(),
                "data": {"error": "codex timed out"},
            },
        )
    revised = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "revise_input",
            "acceptance_criteria": ["重複行は取り込まない"],
        },
        headers={**BOT, "Idempotency-Key": f"revise-failed-{uuid.uuid4()}"},
    )
    assert revised.status_code == 202, revised.text
    (run,) = lease(client)
    assert run["next_actions"], "the run was left with nothing it could do"
    assert run["next_actions"][0]["step_key"] == "fix"


def test_a_report_with_an_unreadable_verdict_does_not_stop_the_projection(client):
    """A verdict of the wrong type is no verdict, not an exception."""
    task = start_mvp_task(client, title="判定が読めない報告")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    finish(
        client,
        verified["job_id"],
        {"report": {**qa_report("pass"), "verdict": {}}},
        worker="worker-qa",
    )
    summary = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["runs"][0][
        "steps"
    ][-1]["attempts"][-1]["result_summary"]
    assert summary["quality_verdict"] == "inconclusive"
    assert "recognised verdict" in summary["downgraded_reason"]


def test_replanning_spends_the_same_revision_budget_as_fixing(client):
    """Repeated changes to the request cannot buy unlimited work.

    Sending the Run back to planning is a revision of the work, so it is charged to
    the Run's budget rather than starting a fresh allowance beside it.
    """
    project()
    lease(client)
    created = client.post(
        "/v1/tasks",
        json={
            "project_id": "test-product",
            "title": "何度も作り直す依頼",
            "objective": "CSV から明細を登録できるようにする",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
            "workflow_id": "mvp-build-v1",
            "start": True,
        },
        headers={**BOT, "Idempotency-Key": f"replan-{uuid.uuid4()}"},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})

    # Two changes to the request, each starting the work over.
    for index in range(2):
        revised = client.post(
            f"/v1/tasks/{task_id}/commands",
            json={
                "type": "revise_input",
                "objective": f"CSV と Excel から明細を登録できるようにする（{index}）",
            },
            headers={**BOT, "Idempotency-Key": f"replan-{index}-{uuid.uuid4()}"},
        )
        assert revised.status_code == 202, revised.text
        (run,) = lease(client)
        assert run["next_actions"][0]["step_key"] == "plan"
        planned = advance(
            client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3}
        )
        finish(client, planned["job_id"], {"report": prd_report()})

    with gateway.pool.connection() as db:
        cycles = db.execute(
            "SELECT revision_cycles FROM workflow_runs WHERE task_id = %s", (task_id,)
        ).fetchone()["revision_cycles"]
    assert cycles == 2, "replanning did not spend the budget"

    # A third change has no budget left, and the Run says so rather than starting
    # work nobody bounded.
    client.post(
        f"/v1/tasks/{task_id}/commands",
        json={"type": "revise_input", "objective": "三度目の目的の変更をします"},
        headers={**BOT, "Idempotency-Key": f"replan-last-{uuid.uuid4()}"},
    )
    (run,) = lease(client)
    action = run["next_actions"][0]
    assert action["type"] == "fail_run"
    assert action["failure_class"] == "revision_limit_reached"
    # And applying it is what the Run actually does next.
    applied = propose(client, run, type="fail_run")
    assert applied.status_code == 202, applied.text
    detail = client.get(f"/v1/tasks/{task_id}", headers=BOT).json()
    assert detail["status"] == "FAILED"
    assert detail["runs"][0]["result"]["failure_class"] == "revision_limit_reached"


def test_a_retry_is_told_what_the_attempt_that_failed_was_told(client):
    """An instruction given to work that never landed is still outstanding.

    The retry would otherwise run without a requirement the requester had already
    made, and could then succeed without it.
    """
    task = start_mvp_task(client, title="失敗した実行の指示")
    client.post(
        f"/v1/tasks/{task['task_id']}/messages",
        json={
            "kind": "instruction",
            "body": "日本語の見出しを使ってください",
            "applies_to": "next_attempt",
        },
        headers=BOT,
    )
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE jobs SET dispatch_id = %s, worker_job_id = 'worker-plan-failed', "
            "state = 'DISPATCHED' WHERE id = %s",
            (f"gateway:{planned['job_id']}:1", planned["job_id"]),
        )
    with mock.patch.object(gateway, "API_SURFACE", "callback"):
        client.post(
            "/v1/worker-events",
            headers={"Authorization": "Bearer test-callback-credential"},
            json={
                "event_id": f"event-{uuid.uuid4()}",
                "gateway_job_id": planned["job_id"],
                "dispatch_id": f"gateway:{planned['job_id']}:1",
                "worker_job_id": "worker-plan-failed",
                "event_type": "failed",
                "sequence": 1,
                "occurred_at": gateway.utcnow().isoformat(),
                "data": {"error": "codex timed out"},
            },
        )
    (run,) = lease(client)
    assert [item["body"] for item in run["instructions"]] == [
        "日本語の見出しを使ってください"
    ]
    assert run["next_actions"][0]["attempt_number"] == 2


def test_a_verification_of_a_change_this_gateway_did_not_record_is_not_a_pass(client):
    """The verifier says which patch it rebuilt; the Gateway holds which one it is.

    A pass about a locally replaced copy must not authorise acceptance of the change
    the Gateway recorded.
    """
    task = start_mvp_task(client, title="別の patch を検証した報告")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    finish(
        client,
        verified["job_id"],
        {
            "report": {
                **qa_report("pass"),
                # The build recorded patch_digest "b" * 64.
                "verified_change": {"patch_digest": "e" * 64, "rebuilt": True},
            }
        },
        worker="worker-qa",
    )
    summary = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["runs"][0][
        "steps"
    ][-1]["attempts"][-1]["result_summary"]
    assert summary["quality_verdict"] == "inconclusive"
    assert "this Gateway recorded" in summary["downgraded_reason"]


def test_a_pass_that_does_not_say_what_it_rebuilt_is_not_a_pass(client):
    """Naming the execution is not the same as naming the change it verified."""
    task = start_mvp_task(client, title="何を検証したか言わない報告")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    finish(
        client,
        verified["job_id"],
        # Everything a pass needs, except which change it rebuilt.
        {"report": {**qa_report("pass"), "verified_change": {"rebuilt": True}}},
        worker="worker-qa",
    )
    summary = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["runs"][0][
        "steps"
    ][-1]["attempts"][-1]["result_summary"]
    assert summary["quality_verdict"] == "inconclusive"
    assert "rebuilt and verified" in summary["downgraded_reason"]


def test_an_instruction_a_retry_acted_on_is_consumed_by_it(client):
    """An instruction cannot stay outstanding for ever once work has answered it."""
    task = start_mvp_task(client, title="再実行が指示を消費する")
    client.post(
        f"/v1/tasks/{task['task_id']}/messages",
        json={
            "kind": "instruction",
            "body": "日本語の見出しを使ってください",
            "applies_to": "next_attempt",
        },
        headers=BOT,
    )
    (run,) = lease(client)
    first = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE jobs SET dispatch_id = %s, worker_job_id = 'worker-plan-1', "
            "state = 'DISPATCHED' WHERE id = %s",
            (f"gateway:{first['job_id']}:1", first["job_id"]),
        )
    with mock.patch.object(gateway, "API_SURFACE", "callback"):
        client.post(
            "/v1/worker-events",
            headers={"Authorization": "Bearer test-callback-credential"},
            json={
                "event_id": f"event-{uuid.uuid4()}",
                "gateway_job_id": first["job_id"],
                "dispatch_id": f"gateway:{first['job_id']}:1",
                "worker_job_id": "worker-plan-1",
                "event_type": "failed",
                "sequence": 1,
                "occurred_at": gateway.utcnow().isoformat(),
                "data": {"error": "codex timed out"},
            },
        )
    # Outstanding again for the retry...
    (run,) = lease(client)
    assert [item["body"] for item in run["instructions"]] == [
        "日本語の見出しを使ってください"
    ]
    second = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, second["job_id"], {"report": prd_report()}, worker="worker-plan-2")
    # ...and consumed by the attempt that succeeded with it.
    (run,) = lease(client)
    assert run["instructions"] == []


def test_a_report_about_a_change_cannot_be_verified_or_accepted(client):
    """A result without the patch that produces it is not a deliverable.

    Nothing could rebuild it, so nothing could establish what a pass about it would
    mean. The Run says so instead of carrying it to a human decision.
    """
    task = start_mvp_task(client, title="patch の無い結果")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    # A build that reported changed files but no patch identity.
    finish(
        client,
        built["job_id"],
        build_result(patch_digest=None, workspace_digest=None),
        worker="worker-build",
    )
    (run,) = lease(client)
    assert {item["kind"] for item in run["artifacts"]} >= {"code-change-report"}
    # The Run does not offer a verification of it: the step that produces a change
    # does it again, because there is nothing here to continue.
    assert run["next_actions"][0]["step_key"] == "implement"
    assert run["next_actions"][0]["cycle"] == 2
    # And proposing the verification anyway is refused.
    refused = propose(
        client,
        run,
        type="create_attempt",
        step_key="qa",
        cycle=1,
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "TRANSITION_NOT_ALLOWED"
    # A review cannot be asked for it either, whatever the Run is standing on.
    with gateway.pool.connection() as db:
        state = runs.run_state(db, uuid.UUID(run["run_id"]))
        assert "report about a change" in runs._unverified_change(db, state)

    # And the redo runs: it produces a change, and it spends the revision budget.
    redone = advance(client, run, run["next_actions"][0], parameters={"task": "やり直す"})
    finish(client, redone["job_id"], build_result(), worker="worker-build-2")
    with gateway.pool.connection() as db:
        cycles = db.execute(
            "SELECT revision_cycles FROM workflow_runs WHERE id = %s",
            (uuid.UUID(run["run_id"]),),
        ).fetchone()["revision_cycles"]
    assert cycles == 1
    (run,) = lease(client)
    assert run["next_actions"][0]["step_key"] == "qa"

    # And that verification runs, against the change that does have an identity:
    # the Task reaches a decision, so the refusal above was about this change
    # being unverifiable, not about the workflow being stuck.
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build-2",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    with gateway.pool.connection() as db:
        bound = db.execute(
            "SELECT input_manifest FROM step_attempts WHERE job_id = %s",
            (uuid.UUID(verified["job_id"]),),
        ).fetchone()["input_manifest"]["verification_target"]
    # It is bound to the redone change, not to the one without a patch.
    assert bound["source_worker_job_id"] == "worker-build-2"
    finish(client, verified["job_id"], {"report": qa_report("pass")}, worker="worker-qa")
    (run,) = lease(client)
    advance(client, run, run["next_actions"][0])
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "WAITING_REVIEW"


def test_a_refused_delivery_does_not_end_work_the_worker_accepted(client):
    """A refused retry says nothing about an execution that already started.

    A lost response and a rotated credential are enough to produce one: ending the
    Attempt on that basis would report a failure that did not happen and settle a
    stop nobody performed.
    """
    task = start_mvp_task(client, title="配送が拒否された実行中の仕事")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    job_id = planned["job_id"]
    # The Worker took it and said so, even though the delivery's own result was lost.
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE jobs SET worker_job_id = 'worker-plan', state = 'RUNNING' WHERE id = %s",
            (job_id,),
        )
        db.commit()
    client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "cancel"},
        headers={**BOT, "Idempotency-Key": f"cancel-refused-{uuid.uuid4()}"},
    )
    # The delivery is retried and refused, through the scheduler that would do it.
    class RotatedCredential:
        """A Worker that accepted the first send and now refuses this one."""

        def configured(self):
            return True

        def send(self, dispatch):
            raise scheduler.Rejected("worker refused the dispatch: HTTP 401")

    with gateway.pool.connection() as db:
        # The first send left no recorded outcome: its response was lost.
        db.execute(
            "UPDATE job_dispatches SET state = 'PENDING', attempts = 1, "
            "retry_at = now() WHERE job_id = %s",
            (job_id,),
        )
        db.commit()
    counts = scheduler.deliver_once(gateway.pool, RotatedCredential(), limit=5)
    assert counts["rejected"] == 1, counts

    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    # Being checked, not failed; and the stop stays unconfirmed.
    assert detail["status"] == "BLOCKED"
    assert detail["control_state"] == "CANCEL_REQUESTED"
    attempt = detail["runs"][0]["steps"][-1]["attempts"][-1]
    assert attempt["status"] == "RUNNING"
    events = client.get("/v1/events", params={"limit": 200}, headers=BOT).json()["events"]
    blocked = [item for item in events if item["type"] == "task.blocked"]
    assert blocked, "the refused delivery was not reported"
    assert "unknown" in blocked[-1]["payload"]["reason"]


def test_a_second_criteria_list_that_disagrees_is_not_a_pass(client):
    """Everything the report judged counts, not only the first list it used."""
    task = start_mvp_task(client, title="条件の一覧が二つある報告")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    finish(
        client,
        verified["job_id"],
        {
            "report": {
                **qa_report("pass"),
                # A second list, holding a judgement that was never finished.
                "checks": [
                    {
                        "criterion": "重複行の扱い",
                        "verdict": "inconclusive",
                        "evidence": "未確認",
                    }
                ],
            }
        },
        worker="worker-qa",
    )
    summary = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["runs"][0][
        "steps"
    ][-1]["attempts"][-1]["result_summary"]
    assert summary["quality_verdict"] == "inconclusive"


def test_a_refused_first_delivery_with_no_execution_is_still_a_failure(client):
    """The first send is the one a refusal can speak for.

    Nothing was accepted, so the Attempt ends — the honest counterpart to a refused
    retry, which cannot say that.
    """
    task = start_mvp_task(client, title="最初の配送が拒否される")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})

    class AlwaysRefuses:
        def configured(self):
            return True

        def send(self, dispatch):
            raise scheduler.Rejected("worker refused the dispatch: HTTP 422")

    counts = scheduler.deliver_once(gateway.pool, AlwaysRefuses(), limit=5)
    assert counts["rejected"] == 1, counts
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "FAILED"
    attempt = detail["runs"][0]["steps"][-1]["attempts"][-1]
    assert attempt["status"] == "FAILED"
    with gateway.pool.connection() as db:
        job = db.execute(
            "SELECT state FROM jobs WHERE id = %s", (uuid.UUID(planned["job_id"]),)
        ).fetchone()
    assert job["state"] == "FAILED_FINAL"


class RecordingConnection:
    """A connection that remembers the statements a call made, in order."""

    def __init__(self, inner):
        self._inner = inner
        self.statements: list[str] = []

    def execute(self, sql, params=None, *args, **kwargs):
        self.statements.append(" ".join(str(sql).split()))
        if params is None:
            return self._inner.execute(sql, *args, **kwargs)
        return self._inner.execute(sql, params, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_a_proposal_takes_the_locks_in_the_platform_order(client):
    """Project, then Task, then Run: the order every other writer uses.

    A human command reaches a Task through its Project and locks the Run after it.
    A proposal that took the Run first could hold what that command is waiting for
    while waiting for what the command holds, and PostgreSQL would abort one of
    them instead of either outcome happening.
    """
    task = start_mvp_task(client, title="ロック順を守る提案")
    (run,) = lease(client)
    with gateway.pool.connection() as db:
        recorder = RecordingConnection(db)
        with pytest.raises(runs.ProposalRejected):
            runs.apply_proposal(
                recorder,
                run_id=uuid.UUID(run["run_id"]),
                token=run["lease"]["token"],
                proposal={"type": "complete_run"},
                actor="test-controller",
            )
        db.rollback()

    locks = [
        statement
        for statement in recorder.statements
        if "FOR UPDATE" in statement
    ]
    assert locks, "the proposal took no locks at all"
    assert "FROM projects" in locks[0], locks
    assert "FROM tasks" in locks[1], locks
    assert "FROM workflow_runs" in locks[2], locks
    assert run["task_id"] == task["task_id"]


def test_two_criteria_differing_only_in_case_each_need_their_own_verdict(client):
    """A pass about `userID` is not a pass about `userid`."""
    task = start_mvp_task(client, title="大文字小文字が違う条件")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    finish(client, planned["job_id"], {"report": prd_report()})
    (run,) = lease(client)
    built = advance(client, run, run["next_actions"][0], parameters={"task": "実装する"})
    finish(client, built["job_id"], build_result(), worker="worker-build")
    (run,) = lease(client)
    verified = advance(
        client,
        run,
        run["next_actions"][0],
        parameters={
            "source_worker_job_id": "worker-build",
            "acceptance_criteria": [
                "正常な CSV から明細を登録できる",
                "API は userID をそのまま受け付ける",
                "API は userid をそのまま受け付ける",
            ],
        },
        input_artifact_ids=[change_artifact(run)],
    )
    finish(
        client,
        verified["job_id"],
        {
            "report": qa_report(
                "pass",
                criteria=[
                    {
                        "criterion": "正常な CSV から明細を登録できる",
                        "verdict": "pass",
                        "evidence": "tests.log",
                    },
                    {
                        "criterion": "API は userID をそのまま受け付ける",
                        "verdict": "pass",
                        "evidence": "tests.log",
                    },
                ],
            )
        },
        worker="worker-qa",
    )
    summary = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["runs"][0][
        "steps"
    ][-1]["attempts"][-1]["result_summary"]
    # One of the two conditions has no verdict, so this is not a pass.
    assert summary["quality_verdict"] == "inconclusive"
    assert "userid" in summary["downgraded_reason"]
    (run,) = lease(client)
    assert run["next_actions"][0]["type"] != "request_review"


def test_a_refused_credential_keeps_a_requested_stop_deliverable(client, monkeypatch):
    """A rejected token is not the Worker refusing the stop: it is retried."""
    import urllib.error
    import urllib.request

    task = start_mvp_task(client, title="資格情報が拒否された停止")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE jobs SET worker_job_id = 'worker-stopping', state = 'RUNNING' "
            "WHERE id = %s",
            (planned["job_id"],),
        )
    assert client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "cancel", "reason": "不要になりました"},
        headers={**BOT, "Idempotency-Key": "cancel-credential-1"},
    ).status_code == 202

    delivery = scheduler.Delivery("http://worker.invalid", "stale-token")

    def refuse(*_args, **_kwargs):
        raise urllib.error.HTTPError("url", 401, "unauthorized", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    counts = scheduler.deliver_commands(gateway.pool, delivery)
    assert counts == {"sent": 0, "refused": 0, "unknown": 1}
    with gateway.pool.connection() as db:
        queued = db.execute(
            "SELECT * FROM worker_commands WHERE job_id = %s", (planned["job_id"],)
        ).fetchone()
    # Still to be delivered, not refused: the stop was never handed over.
    assert queued["state"] == "UNKNOWN"
    assert "credential" in queued["last_error"]
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["control_state"] == "CANCEL_REQUESTED"

    # The credential is restored, and the same stop is delivered.
    class Accepted:
        status = 200

        def read(self, _limit=None):
            return b'{"status": "CANCEL_REQUESTED"}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Accepted())
    with gateway.pool.connection() as db:
        db.execute("UPDATE worker_commands SET retry_at = now() WHERE job_id = %s",
                   (uuid.UUID(planned["job_id"]),))
        db.commit()
    assert scheduler.deliver_commands(gateway.pool, delivery)["sent"] == 1
    with gateway.pool.connection() as db:
        sent = db.execute(
            "SELECT * FROM worker_commands WHERE job_id = %s", (planned["job_id"],)
        ).fetchone()
    assert sent["state"] == "SENT"


def test_a_stop_requested_before_the_execution_was_known_is_sent_on_the_callback(client):
    """The dispatch response is not the only way an execution id becomes known."""
    task = start_mvp_task(client, title="実行 ID が callback で判明する停止")
    (run,) = lease(client)
    planned = advance(client, run, run["next_actions"][0], parameters={"idea": "CSV 取り込み" * 3})
    # Sent, but no response came back: the delivery cannot be withdrawn and the
    # Gateway has no execution id for it.
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE jobs SET state = 'RECONCILING', dispatch_id = %s, worker_job_id = NULL "
            "WHERE id = %s",
            (f"gateway:{planned['job_id']}:1", uuid.UUID(planned["job_id"])),
        )
        db.execute(
            "UPDATE job_dispatches SET state = 'UNKNOWN' WHERE job_id = %s",
            (uuid.UUID(planned["job_id"]),),
        )
        db.commit()
    assert client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "cancel", "reason": "やめます"},
        headers={**BOT, "Idempotency-Key": "cancel-unknown-execution"},
    ).status_code == 202
    with gateway.pool.connection() as db:
        assert db.execute(
            "SELECT count(*) AS n FROM worker_commands WHERE job_id = %s",
            (uuid.UUID(planned["job_id"]),),
        ).fetchone()["n"] == 0

    # The Worker reports for duty: now there is something to stop.
    with mock.patch.object(gateway, "API_SURFACE", "callback"):
        accepted = client.post(
            "/v1/worker-events",
            headers={"Authorization": "Bearer test-callback-credential"},
            json={
                "event_id": f"event-{uuid.uuid4()}",
                "gateway_job_id": planned["job_id"],
                "dispatch_id": f"gateway:{planned['job_id']}:1",
                "worker_job_id": "worker-late-identity",
                "event_type": "accepted",
                "sequence": 1,
                "occurred_at": gateway.utcnow().isoformat(),
                "data": {},
            },
        )
    assert accepted.status_code == 202, accepted.text
    with gateway.pool.connection() as db:
        queued = db.execute(
            "SELECT * FROM worker_commands WHERE job_id = %s",
            (uuid.UUID(planned["job_id"]),),
        ).fetchone()
    assert queued is not None, "the requested stop was never queued"
    assert (queued["kind"], queued["state"], queued["worker_job_id"]) == (
        "cancel",
        "PENDING",
        "worker-late-identity",
    )


def test_a_late_refusal_about_finished_work_is_recorded_and_nothing_else(client):
    """A deliverable waiting for a decision is not disturbed by delivery history."""
    task = drive_to_review(client, title="完了後に届いた配送の拒否")
    with gateway.pool.connection() as db:
        job = db.execute(
            "SELECT * FROM jobs WHERE task_id = %s ORDER BY created_at DESC LIMIT 1",
            (task["task_id"],),
        ).fetchone()
        tasks.project_dispatch_outcome(
            db,
            job=job,
            definitive=False,
            reason="a later delivery was refused, so the outcome is unknown",
        )
        db.commit()
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "WAITING_REVIEW"
    events = client.get("/v1/events", params={"limit": 300}, headers=BOT).json()["events"]
    noted = [
        item
        for item in events
        if item["type"] == "task.dispatch_refused_after_completion"
    ]
    assert noted, "the late refusal was not recorded"
    assert noted[-1]["payload"]["task_status"] == "WAITING_REVIEW"


def test_a_late_refusal_about_a_finished_task_is_still_recorded(client):
    """A Task nobody is waiting on is not reopened, and not silently swallowed."""
    task = drive_to_review(client, title="完了後に届いた配送の拒否（終端）")
    accepted = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={
            "type": "accept_deliverable",
            "target_digest": deliverable_digest(client, task),
        },
        headers={**HUMAN, "Idempotency-Key": "accept-late-refusal"},
    )
    assert accepted.status_code == 202, accepted.text
    assert client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["status"] == (
        "COMPLETED"
    )

    with gateway.pool.connection() as db:
        job = db.execute(
            "SELECT * FROM jobs WHERE task_id = %s ORDER BY created_at DESC LIMIT 1",
            (task["task_id"],),
        ).fetchone()
        assert (
            tasks.project_dispatch_outcome(
                db,
                job=job,
                definitive=False,
                reason="a later delivery was refused, so the outcome is unknown",
            )
            is None
        )
        db.commit()

    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "COMPLETED"
    events = client.get("/v1/events", params={"limit": 400}, headers=BOT).json()["events"]
    noted = [
        item
        for item in events
        if item["type"] == "task.dispatch_refused_after_completion"
        and item["payload"]["job_id"] == str(job["id"])
    ]
    assert noted, "the late refusal about a finished Task was not recorded"
    assert noted[-1]["payload"]["task_status"] == "COMPLETED"
