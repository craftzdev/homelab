"""Task ledger invariants against disposable PostgreSQL."""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest
from psycopg.errors import UniqueViolation

from app import main as gateway
from app import scheduler, tasks, workflows

BOT = {"Authorization": "Bearer test-gateway-credential"}


def project(project_id="test-product"):
    now = gateway.utcnow()
    with gateway.pool.connection() as db:
        db.execute(
            "INSERT INTO projects (id,title,idea,state,created_at,updated_at) "
            "VALUES (%s,'test','test idea','REGISTERED',%s,%s) ON CONFLICT DO NOTHING",
            (project_id, now, now),
        )
    return project_id


def create(client, **overrides):
    body = {
        "project_id": project(overrides.pop("project_id", "test-product")),
        "title": "CSV の取り込みを追加する",
        "objective": "ユーザーが CSV を選択して明細を登録できるようにする",
        "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        "action": "code.build",
        **overrides,
    }
    key = overrides.pop("idempotency_key", None) or f"task-{uuid.uuid4()}"
    response = client.post(
        "/v1/tasks", json=body, headers={**BOT, "Idempotency-Key": key}
    )
    assert response.status_code == 201, response.text
    return response.json()


def prd_report(**overrides):
    """The report shape app/agent_executor.py requires for product.plan."""
    return {
        "title": "CSV 取り込み",
        "slug": "csv-import",
        "problem": "明細を手入力している",
        "target_audience": "個人事業主",
        "value_proposition": "CSV から一括登録できる",
        "features": ["CSV アップロード"],
        "acceptance_criteria": ["正常な CSV から明細を登録できる"],
        "analytics_events": ["csv_imported"],
        **overrides,
    }


def qa_report(verdict="pass", *, criteria=None, **overrides):
    """The report shape app/agent_executor.py requires for qa.review."""
    return {
        "verdict": verdict,
        "summary": "検証結果",
        "acceptance_criteria": criteria
        if criteria is not None
        else [{"criterion": "CSV を取り込める", "verdict": verdict, "evidence": "tests.log"}],
        "risks": [],
        **overrides,
    }


def build_result(**overrides):
    """The result shape app/build_executor.py returns for code.build/code.fix."""
    return {
        "mode": "codex",
        "succeeded": True,
        "project_id": "test-product",
        "branch": "codex/build",
        "base_commit": "0" * 40,
        "changed_files": ["app/main.py"],
        "tests": [{"name": "pytest", "passed": True}],
        "artifacts": ["changes.patch", "result.json"],
        "patch_digest": "b" * 64,
        "workspace_digest": "c" * 64,
        "change_manifest": [{"path": "app/main.py", "sha256": "d" * 64, "state": "present"}],
        **overrides,
    }


def worker_event(client, job_id, event_type, data=None, sequence=1, worker="worker-1"):
    # Worker callbacks are served by the separate callback surface, which runs
    # the same application with a different credential.
    with mock.patch.object(gateway, "API_SURFACE", "callback"):
        return client.post(
            "/v1/worker-events",
            headers={"Authorization": "Bearer test-callback-credential"},
            json={
                "event_id": f"event-{uuid.uuid4()}",
                "gateway_job_id": job_id,
                "dispatch_id": f"gateway:{job_id}:1",
                "worker_job_id": worker,
                "event_type": event_type,
                "sequence": sequence,
                "occurred_at": gateway.utcnow().isoformat(),
                "data": data or {},
            },
        )


def test_task_is_registered_without_starting_execution(client):
    task = create(client)
    assert task["status"] == "READY"
    assert task["stage_key"] == "intake"
    assert task["revision"] == 1
    assert task["source"] == "api"
    assert task["job_id"] is None
    assert task["input_revisions"][0]["acceptance_criteria"] == [
        "正常な CSV から明細を登録できる"
    ]
    # A registered request can be started, abandoned, or edited before it runs.
    assert {command["type"] for command in task["available_commands"]} == {
        "start",
        "cancel",
        "revise_input",
    }


def test_started_task_carries_one_run_step_and_attempt(client):
    task = create(client, start=True)
    assert task["status"] == "ACTIVE"
    assert task["stage_key"] == "implementation"
    assert task["job_id"] is not None

    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    (run,) = detail["runs"]
    assert run["workflow_id"] == "single-action-v1"
    (step,) = run["steps"]
    assert (step["logical_key"], step["status"], step["action"]) == (
        "execute",
        "RUNNING",
        "code.build",
    )
    (attempt,) = step["attempts"]
    assert attempt["attempt_number"] == 1
    assert attempt["job_id"] == task["job_id"]
    assert attempt["status"] == "RUNNING"

    job = client.get(f"/v1/jobs/{task['job_id']}", headers=BOT).json()
    assert job["task_id"] == task["task_id"]
    assert job["attempt_id"] == attempt["attempt_id"]


def test_a_controller_driven_workflow_cannot_start_without_a_controller(client):
    project()
    response = client.post(
        "/v1/tasks",
        json={
            "project_id": "test-product",
            "title": "MVP を作る",
            "objective": "最初の版を作る",
            "workflow_id": "mvp-build-v1",
            "start": True,
        },
        headers={**BOT, "Idempotency-Key": "mvp-start-attempt"},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "CONTROLLER_UNAVAILABLE"

    # Registering the same request without starting it is still allowed.
    registered = client.post(
        "/v1/tasks",
        json={
            "project_id": "test-product",
            "title": "MVP を作る",
            "objective": "最初の版を作る",
            "workflow_id": "mvp-build-v1",
        },
        headers={**BOT, "Idempotency-Key": "mvp-register-only"},
    )
    assert registered.status_code == 201
    unavailable = {
        command["type"]: command["reason"]
        for command in registered.json()["unavailable_commands"]
    }
    assert unavailable["start"] == "CONTROLLER_UNAVAILABLE"


def test_unknown_workflow_is_rejected_with_the_available_candidates(client):
    project()
    response = client.post(
        "/v1/tasks",
        json={
            "project_id": "test-product",
            "title": "何か",
            "objective": "何かする",
            "workflow_id": "content-production-v1",
        },
        headers={**BOT, "Idempotency-Key": "unknown-workflow"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "CAPABILITY_UNAVAILABLE"


def test_directly_submitted_job_gets_its_own_card(client):
    project()
    response = client.post(
        "/v1/jobs",
        json={
            "action": "product.plan",
            "project_id": "test-product",
            "environment": "preview",
        },
        headers={**BOT, "Idempotency-Key": "legacy-submit-1"},
    )
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["task_id"] is not None

    task = client.get(f"/v1/tasks/{job['task_id']}", headers=BOT).json()
    assert task["workflow_id"] == "single-action-v1"
    assert task["stage_key"] == "planning"
    assert task["source"] == "api"
    assert task["runs"][0]["steps"][0]["attempts"][0]["job_id"] == job["job_id"]


def test_idempotent_replay_does_not_create_a_second_card(client):
    project()
    body = {
        "action": "product.plan",
        "project_id": "test-product",
        "environment": "preview",
    }
    headers = {**BOT, "Idempotency-Key": "legacy-submit-2"}
    first = client.post("/v1/jobs", json=body, headers=headers).json()
    second = client.post("/v1/jobs", json=body, headers=headers).json()
    assert first["job_id"] == second["job_id"]
    assert second["task_id"] == first["task_id"]
    listing = client.get("/v1/tasks", headers=BOT).json()
    assert len(listing["tasks"]) == 1


def test_completed_action_finishes_the_card_and_records_its_artifact(client):
    task = create(client, action="product.plan", start=True)
    report = prd_report()
    assert worker_event(
        client, task["job_id"], "completed", {"report": report}
    ).status_code == 202

    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "COMPLETED"
    assert detail["stage_key"] == "done"
    assert detail["active_run_id"] is None
    assert detail["runs"][0]["status"] == "COMPLETED"
    (artifact,) = detail["artifacts"]
    assert artifact["kind"] == "prd"
    assert artifact["digest"] == tasks.canonical_digest(report)[0]

    metadata = client.get(f"/v1/artifacts/{artifact['artifact_id']}", headers=BOT)
    assert metadata.status_code == 200
    assert "content" not in metadata.json()
    content = client.get(
        f"/v1/artifacts/{artifact['artifact_id']}/content", headers=BOT
    ).json()
    assert content["content"] == report


def test_job_success_and_quality_verdict_stay_separate(client):
    task = create(client, action="qa.review", start=True)
    worker_event(client, task["job_id"], "completed", {"report": qa_report("fail")})

    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    attempt = detail["runs"][0]["steps"][0]["attempts"][0]
    assert attempt["status"] == "SUCCEEDED"
    assert attempt["job_state"] == "SUCCEEDED"
    assert attempt["result_summary"]["quality_verdict"] == "fail"
    assert detail["artifacts"][0]["manifest"]["quality_verdict"] == "fail"


def test_qa_report_without_a_verdict_is_inconclusive_not_pass(client):
    task = create(client, action="qa.review", start=True)
    worker_event(
        client,
        task["job_id"],
        "completed",
        {"report": qa_report(verdict="unknown-value")},
    )
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert (
        detail["runs"][0]["steps"][0]["attempts"][0]["result_summary"]["quality_verdict"]
        == "inconclusive"
    )


def test_failed_execution_fails_the_card(client):
    task = create(client, start=True)
    worker_event(client, task["job_id"], "failed", {"error": "build failed"})
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "FAILED"
    assert detail["runs"][0]["status"] == "FAILED"
    attempt = detail["runs"][0]["steps"][0]["attempts"][0]
    assert attempt["status"] == "FAILED"
    assert attempt["failure_class"] == "worker_reported_failure"


def test_sibling_tasks_in_one_project_keep_their_own_state(client):
    first = create(client, title="CSV 取込", action="code.build", start=True)
    second = create(client, title="検索機能", action="product.plan", start=True)
    worker_event(client, second["job_id"], "completed", {"report": prd_report()})

    first_detail = client.get(f"/v1/tasks/{first['task_id']}", headers=BOT).json()
    second_detail = client.get(f"/v1/tasks/{second['task_id']}", headers=BOT).json()
    assert first_detail["status"] == "ACTIVE"
    assert first_detail["artifacts"] == []
    assert second_detail["status"] == "COMPLETED"
    assert len(second_detail["artifacts"]) == 1


def test_late_callback_cannot_revive_a_finished_attempt(client):
    task = create(client, action="product.plan", start=True)
    worker_event(client, task["job_id"], "completed", {"report": prd_report()})
    replay = worker_event(
        client, task["job_id"], "progress", {"note": "late"}, sequence=2
    )
    assert replay.status_code == 409
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "COMPLETED"


def test_metadata_update_detects_a_concurrent_change(client):
    task = create(client)
    stale = client.patch(
        f"/v1/tasks/{task['task_id']}",
        json={"priority": "high", "expected_revision": task["revision"] + 5},
        headers=BOT,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "REVISION_CONFLICT"
    assert stale.json()["detail"]["current_revision"] == task["revision"]

    applied = client.patch(
        f"/v1/tasks/{task['task_id']}",
        json={"priority": "high"},
        headers={**BOT, "If-Match": str(task["revision"])},
    )
    assert applied.status_code == 200
    assert applied.json()["priority"] == "high"
    assert applied.json()["revision"] == task["revision"] + 1


def test_metadata_update_cannot_change_state(client):
    task = create(client)
    response = client.patch(
        f"/v1/tasks/{task['task_id']}", json={"status": "COMPLETED"}, headers=BOT
    )
    assert response.status_code == 422


def test_instructions_are_recorded_with_how_they_apply(client):
    """A Workflow whose steps are built from the Run carries instructions."""
    task = create(client, action=None, workflow_id="mvp-build-v1")
    response = client.post(
        f"/v1/tasks/{task['task_id']}/messages",
        json={
            "body": "重複する明細の挙動も検証してください",
            "kind": "instruction",
            "applies_to": "next_attempt",
        },
        headers=BOT,
    )
    assert response.status_code == 201
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["messages"][0]["applies_to"] == "next_attempt"
    assert detail["messages"][0]["input_revision"] == 1


def test_a_single_action_refuses_an_instruction_nothing_would_read(client):
    """Its execution is built from its parameters, not from messages about it."""
    task = create(client)
    refused = client.post(
        f"/v1/tasks/{task['task_id']}/messages",
        json={
            "body": "重複する明細の挙動も検証してください",
            "kind": "instruction",
            "applies_to": "next_attempt",
        },
        headers=BOT,
    )
    assert refused.status_code == 409
    assert "parameters" in refused.json()["detail"]["message"]
    # A comment is still a comment: it is recorded and read by people.
    noted = client.post(
        f"/v1/tasks/{task['task_id']}/messages",
        json={"body": "あとで確認します", "kind": "comment"},
        headers=BOT,
    )
    assert noted.status_code == 201


def test_a_single_action_refuses_references_nothing_would_read(client):
    project()
    refused = client.post(
        "/v1/tasks",
        json={
            "project_id": "test-product",
            "title": "参照だけ付けた依頼",
            "objective": "CSV から明細を登録できるようにする",
            "action": "product.plan",
            "parameters": {"idea": "CSV 取り込みの計画を立てる"},
            "context_refs": ["https://example.com/仕様"],
        },
        headers={**BOT, "Idempotency-Key": f"refs-{uuid.uuid4()}"},
    )
    assert refused.status_code == 422
    assert "parameters" in refused.text


def test_artifact_is_not_readable_under_another_project(client):
    task = create(client, action="product.plan", start=True)
    worker_event(client, task["job_id"], "completed", {"report": prd_report()})
    artifact_id = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()[
        "artifacts"
    ][0]["artifact_id"]
    response = client.get(
        f"/v1/artifacts/{artifact_id}", params={"project_id": "other-product"}, headers=BOT
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "ARTIFACT_MISMATCH"


def test_event_feed_is_gapless_and_snapshot_resumes_from_its_cursor(client):
    first = create(client, start=True)
    snapshot = client.get("/v1/snapshot", headers=BOT).json()
    assert [task["task_id"] for task in snapshot["tasks"]] == [first["task_id"]]

    second = create(client, title="あとの依頼")
    feed = client.get("/v1/events", params={"after": snapshot["cursor"]}, headers=BOT).json()
    assert [event["type"] for event in feed["events"]] == ["task.created"]
    assert feed["events"][0]["aggregate_id"] == second["task_id"]

    everything = client.get("/v1/events", headers=BOT).json()
    cursors = [event["cursor"] for event in everything["events"]]
    assert cursors == list(range(1, len(cursors) + 1))
    assert [event["type"] for event in everything["events"]] == [
        "task.created",
        "task.run_started",
        "task.created",
    ]


def test_listing_filters_and_pages(client):
    created = [create(client, title=f"依頼 {index}") for index in range(3)]
    first_page = client.get("/v1/tasks", params={"limit": 2}, headers=BOT).json()
    assert len(first_page["tasks"]) == 2
    assert first_page["has_more"] is True
    second_page = client.get(
        "/v1/tasks", params={"limit": 2, "cursor": first_page["next_cursor"]}, headers=BOT
    ).json()
    assert len(second_page["tasks"]) == 1
    assert {task["task_id"] for task in first_page["tasks"] + second_page["tasks"]} == {
        task["task_id"] for task in created
    }
    assert first_page["stage_counts"] == {"intake": 3}

    assert client.get(
        "/v1/tasks", params={"q": "依頼 1"}, headers=BOT
    ).json()["stage_counts"] == {"intake": 3}
    assert len(client.get("/v1/tasks", params={"q": "依頼 1"}, headers=BOT).json()["tasks"]) == 1
    assert client.get("/v1/tasks", params={"attention_only": True}, headers=BOT).json()[
        "tasks"
    ] == []


def test_project_aggregate_counts_tasks_instead_of_one_latest_job(client):
    create(client, start=True)
    blocked = create(client)
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE tasks SET status = 'WAITING_REVIEW' WHERE id = %s",
            (blocked["task_id"],),
        )
    summary = client.get("/v1/projects/test-product", headers=BOT).json()["tasks"]
    assert summary == {"task_count": 2, "active_task_count": 2, "attention_count": 1}


def test_catalog_does_not_claim_unverified_capability(client):
    catalog = client.get("/v1/catalog", headers=BOT).json()
    assert catalog["catalog_completeness"] == "gateway_only"
    assert all(action["availability"] == "unverified" for action in catalog["actions"])
    drivers = {
        workflow["workflow_id"]: workflow["driver"]
        for workflow in catalog["workflows"]
        if workflow["startable"]
    }
    assert drivers == {"single-action-v1": "gateway", "mvp-build-v1": "controller"}


def test_one_active_run_per_task_is_enforced_by_the_database(client):
    task = create(client, start=True)
    now = gateway.utcnow()
    with pytest.raises(UniqueViolation):
        with gateway.pool.connection() as db:
            db.execute(
                "INSERT INTO workflow_runs (id,task_id,workflow_id,workflow_version,"
                "input_revision,status,orchestration_mode,created_at,updated_at) "
                "VALUES (%s,%s,'single-action-v1','1.0.0',1,'ACTIVE','workflow-v1',%s,%s)",
                (uuid.uuid4(), task["task_id"], now, now),
            )


def test_one_non_terminal_attempt_per_step_is_enforced_by_the_database(client):
    task = create(client, start=True)
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    step_id = detail["runs"][0]["steps"][0]["step_id"]
    now = gateway.utcnow()
    with pytest.raises(UniqueViolation):
        with gateway.pool.connection() as db:
            db.execute(
                "INSERT INTO step_attempts (id,step_id,attempt_number,status,created_at) "
                "VALUES (%s,%s,2,'RUNNING',%s)",
                (uuid.uuid4(), step_id, now),
            )


def test_one_job_belongs_to_one_attempt(client):
    task = create(client, start=True)
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    step_id = detail["runs"][0]["steps"][0]["step_id"]
    now = gateway.utcnow()
    with pytest.raises(UniqueViolation):
        with gateway.pool.connection() as db:
            db.execute(
                "INSERT INTO step_attempts (id,step_id,attempt_number,job_id,status,created_at) "
                "VALUES (%s,%s,2,%s,'SUCCEEDED',%s)",
                (uuid.uuid4(), step_id, task["job_id"], now),
            )


def test_mcp_task_tools_are_published(client):
    response = client.post(
        "/mcp",
        headers={**BOT, "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert {
        "list_capabilities",
        "list_workflows",
        "create_task",
        "get_task",
        "list_tasks",
        "request_task_action",
        "add_task_instruction",
    } <= names


def mcp_call(client, name, arguments):
    response = client.post(
        "/mcp",
        headers={**BOT, "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()["result"]
    assert not payload.get("isError"), payload
    return payload["structuredContent"]


def test_task_created_through_mcp_is_recorded_as_bot_work(client):
    project()
    created = mcp_call(
        client,
        "create_task",
        {
            "project_id": "test-product",
            "title": "このアイデアの MVP を実装して",
            "objective": "最小構成で動くものを作る",
            "action": "code.build",
            "start": True,
        },
    )
    assert created["source"] == "grok"
    assert created["status"] == "ACTIVE"
    fetched = mcp_call(client, "get_task", {"task_id": created["task_id"]})
    assert fetched["task_id"] == created["task_id"]
    listed = mcp_call(client, "list_tasks", {"project_id": "test-product"})
    assert [task["task_id"] for task in listed["tasks"]] == [created["task_id"]]


def test_bot_cannot_declare_its_request_as_human_operation(client):
    project()
    listing = client.post(
        "/mcp",
        headers={**BOT, "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ).json()["result"]["tools"]
    schema = next(tool for tool in listing if tool["name"] == "create_task")["inputSchema"]
    assert "source" not in schema["properties"]

    # Even when a client sends one anyway, provenance comes from the surface.
    created = mcp_call(
        client,
        "create_task",
        {
            "project_id": "test-product",
            "title": "偽装",
            "objective": "human として登録したい",
            "action": "code.build",
            "source": "human",
        },
    )
    assert created["source"] == "grok"
    assert created["created_by"] == gateway.MCP_PRINCIPAL


def test_mcp_submitted_job_is_attributed_to_the_bot(client):
    project()
    job = mcp_call(
        client,
        "submit_job",
        {
            "action": "product.plan",
            "project_id": "test-product",
            "environment": "preview",
            "idempotency_key": "grok-submit-1",
        },
    )
    task = client.get(f"/v1/tasks/{job['task_id']}", headers=BOT).json()
    assert task["source"] == "grok"
    assert task["created_by"] == gateway.MCP_PRINCIPAL


def test_workflow_definitions_reject_broken_graphs():
    unreachable = workflows.Workflow(
        id="broken",
        version="1.0.0",
        entry_step="a",
        completion="output_contract",
        steps={
            "a": workflows.Step(
                key="a", stage="planning", action="x", agent="y", output_contract="z"
            ),
            "b": workflows.Step(
                key="b", stage="qa", action="x", agent="y", output_contract="z"
            ),
        },
    )
    with pytest.raises(workflows.WorkflowDefinitionError, match="unreachable"):
        workflows.validate(unreachable)

    unknown_target = workflows.Workflow(
        id="broken",
        version="1.0.0",
        entry_step="a",
        completion="output_contract",
        steps={
            "a": workflows.Step(
                key="a",
                stage="planning",
                action="x",
                agent="y",
                output_contract="z",
                on_success="missing",
            )
        },
    )
    with pytest.raises(workflows.WorkflowDefinitionError, match="unknown transition"):
        workflows.validate(unknown_target)

    undeclared_cycle = workflows.Workflow(
        id="broken",
        version="1.0.0",
        entry_step="a",
        completion="output_contract",
        steps={
            "a": workflows.Step(
                key="a",
                stage="planning",
                action="x",
                agent="y",
                output_contract="z",
                on_success="b",
            ),
            "b": workflows.Step(
                key="b",
                stage="qa",
                action="x",
                agent="y",
                output_contract="z",
                on_success="a",
            ),
        },
    )
    with pytest.raises(workflows.WorkflowDefinitionError, match="revision entry"):
        workflows.validate(undeclared_cycle)


def test_shipped_workflows_are_valid():
    for workflow in workflows.WORKFLOWS.values():
        workflows.validate(workflow)
    assert workflows.MVP_BUILD_V1.steps["fix"].revision_entry is True


def video_job(client):
    """A persisted video job with its card, without needing ComfyUI configured."""
    project()
    now = gateway.utcnow()
    job_id = uuid.uuid4()
    with gateway.pool.connection() as db:
        db.execute(
            "INSERT INTO jobs (id,idempotency_key,project_id,action,environment,state,"
            "input,created_at,updated_at) VALUES (%s,%s,'test-product','video.generate',"
            "'preview','RUNNING',%s,%s,%s)",
            (
                job_id,
                str(job_id),
                json.dumps({"payload": {"parameters": {"prompt": "a"}}, "sha256": "x"}),
                now,
                now,
            ),
        )
        job = db.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
        attached = tasks.attach_legacy_job(
            db, job=job, actor="test", source="api", title="動画生成"
        )
        db.commit()
    return attached["task"]["id"], job_id


def test_video_result_unknown_blocks_the_card_without_claiming_an_outcome(client):
    task_id, job_id = video_job(client)

    with gateway.pool.connection() as db:
        from app import video

        video.set_result(
            db,
            job_id,
            "NEEDS_REVIEW",
            {"error": "submission_outcome_unknown", "resubmit_safe": False},
        )
    detail = client.get(f"/v1/tasks/{task_id}", headers=BOT).json()
    assert detail["status"] == "BLOCKED"
    assert detail["runs"][0]["steps"][0]["attempts"][0]["status"] == "RUNNING"
    blocked = [
        event
        for event in client.get("/v1/events", headers=BOT).json()["events"]
        if event["type"] == "task.blocked"
    ]
    assert blocked[0]["payload"]["reason"] == "submission_outcome_unknown"


def test_registered_task_can_be_started_later_with_its_frozen_request(client):
    task = create(client, action="product.plan")
    assert task["status"] == "READY"

    command = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "start", "expected_revision": task["revision"]},
        headers={**BOT, "Idempotency-Key": "start-task-1"},
    )
    assert command.status_code == 202, command.text
    accepted = command.json()
    assert accepted["status"] == "SUCCEEDED"
    assert accepted["idempotent_replay"] is False

    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "ACTIVE"
    assert detail["stage_key"] == "planning"
    attempt = detail["runs"][0]["steps"][0]["attempts"][0]
    assert attempt["job_id"] == accepted["result"]["job_id"]
    assert attempt["input_manifest"]["action"] == "product.plan"

    status_url = client.get(accepted["status_url"], headers=BOT).json()
    assert status_url["command_id"] == accepted["command_id"]


def test_resent_command_returns_the_first_result_without_running_again(client):
    task = create(client, action="product.plan")
    body = {"type": "start"}
    headers = {**BOT, "Idempotency-Key": "start-task-resend"}
    first = client.post(f"/v1/tasks/{task['task_id']}/commands", json=body, headers=headers).json()
    second = client.post(f"/v1/tasks/{task['task_id']}/commands", json=body, headers=headers).json()
    assert second["command_id"] == first["command_id"]
    assert second["idempotent_replay"] is True
    assert second["result"]["job_id"] == first["result"]["job_id"]
    with gateway.pool.connection() as db:
        jobs = db.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"]
        attempts = db.execute("SELECT count(*) AS n FROM step_attempts").fetchone()["n"]
    assert (jobs, attempts) == (1, 1)


def test_same_key_with_a_different_request_is_rejected(client):
    task = create(client, action="product.plan")
    headers = {**BOT, "Idempotency-Key": "start-task-mismatch"}
    client.post(f"/v1/tasks/{task['task_id']}/commands", json={"type": "start"}, headers=headers)
    conflict = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "start", "reason": "別の要求"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "REVISION_CONFLICT"


def test_starting_a_running_task_is_refused_with_its_reason(client):
    task = create(client, action="product.plan", start=True)
    response = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "start"},
        headers={**BOT, "Idempotency-Key": "start-task-twice"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "INVALID_STATE"


def test_command_on_a_stale_revision_is_refused(client):
    task = create(client, action="product.plan")
    response = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "start", "expected_revision": task["revision"] + 3},
        headers={**BOT, "Idempotency-Key": "start-task-stale"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "REVISION_CONFLICT"
    assert client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["status"] == "READY"


def test_a_controller_driven_task_reports_why_it_cannot_start(client):
    project()
    registered = client.post(
        "/v1/tasks",
        json={
            "project_id": "test-product",
            "title": "MVP を作る",
            "objective": "最初の版を作る",
            "workflow_id": "mvp-build-v1",
        },
        headers={**BOT, "Idempotency-Key": "mvp-no-action"},
    ).json()
    response = client.post(
        f"/v1/tasks/{registered['task_id']}/commands",
        json={"type": "start"},
        headers={**BOT, "Idempotency-Key": "start-mvp-task"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONTROLLER_UNAVAILABLE"


def test_bot_can_start_a_task_through_mcp(client):
    project()
    created = mcp_call(
        client,
        "create_task",
        {
            "project_id": "test-product",
            "title": "分析して",
            "objective": "指標を確認する",
            "action": "growth.plan",
        },
    )
    command = mcp_call(
        client,
        "request_task_action",
        {"task_id": created["task_id"], "type": "start", "idempotency_key": "grok-start-1"},
    )
    assert command["status"] == "SUCCEEDED"
    assert mcp_call(client, "get_task", {"task_id": created["task_id"]})["status"] == "ACTIVE"


# --- Delivery outcomes -----------------------------------------------------


class FakeDelivery:
    """Stands in for the Worker endpoint the scheduler delivers to."""

    def __init__(self, outcome="accept", *, worker_job_id="worker-1", configured=True):
        self.outcome = outcome
        self.worker_job_id = worker_job_id
        self._configured = configured
        self.sent = []

    def configured(self):
        return self._configured

    def send(self, dispatch):
        self.sent.append(dispatch)
        if self.outcome == "accept":
            return self.worker_job_id
        if self.outcome == "reject":
            raise scheduler.Rejected("worker refused the dispatch: HTTP 422")
        raise scheduler.Unknown("worker did not answer")


def deliver(client, delivery, times=1):
    for _ in range(times):
        with gateway.pool.connection() as db:
            # Due immediately, so a test never waits out the backoff.
            db.execute("UPDATE job_dispatches SET retry_at = %s", (gateway.utcnow(),))
        scheduler.deliver_once(gateway.pool, delivery)


def test_a_job_is_queued_for_delivery_in_the_same_commit(client):
    task = create(client, start=True)
    with gateway.pool.connection() as db:
        dispatch = db.execute(
            "SELECT * FROM job_dispatches WHERE job_id = %s", (task["job_id"],)
        ).fetchone()
    # Registered and queued together; nothing has been sent yet.
    assert dispatch["state"] == "PENDING"
    assert dispatch["dispatch_id"] == f"gateway:{task['job_id']}:1"
    assert dispatch["endpoint"] == "build"
    assert client.get(f"/v1/jobs/{task['job_id']}", headers=BOT).json()["state"] == "QUEUED"


def test_delivery_records_the_worker_execution_it_reached(client):
    task = create(client, start=True)
    delivery = FakeDelivery(worker_job_id="worker-accepted")
    deliver(client, delivery)
    job = client.get(f"/v1/jobs/{task['job_id']}", headers=BOT).json()
    assert job["state"] == "DISPATCHED"
    assert job["worker_job_id"] == "worker-accepted"
    with gateway.pool.connection() as db:
        assert db.execute(
            "SELECT state FROM job_dispatches WHERE job_id = %s", (task["job_id"],)
        ).fetchone()["state"] == "ACCEPTED"
    assert client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["status"] == "ACTIVE"


def test_a_refused_delivery_fails_the_card_instead_of_leaving_it_running(client):
    task = create(client, start=True)
    deliver(client, FakeDelivery("reject"))
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "FAILED"
    attempt = detail["runs"][0]["steps"][0]["attempts"][0]
    assert attempt["failure_class"] == "dispatch_rejected"
    assert client.get(f"/v1/jobs/{task['job_id']}", headers=BOT).json()["state"] == "FAILED_FINAL"
    types = [event["type"] for event in client.get("/v1/events", headers=BOT).json()["events"]]
    assert "task.dispatch_failed" in types


def test_an_unconfigured_worker_is_a_refusal_the_gateway_made_itself(client):
    task = create(client, start=True)
    deliver(client, FakeDelivery(configured=False))
    assert client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["status"] == "FAILED"


def test_delivery_is_retried_before_the_outcome_is_called_unknown(client):
    task = create(client, start=True)
    delivery = FakeDelivery("unknown")
    deliver(client, delivery, times=3)
    # Still being retried: the card is not failed and not blocked yet.
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "ACTIVE"
    assert client.get(f"/v1/jobs/{task['job_id']}", headers=BOT).json()["state"] == "QUEUED"
    assert len(delivery.sent) == 3
    assert {dispatch["dispatch_id"] for dispatch in delivery.sent} == {
        f"gateway:{task['job_id']}:1"
    }  # the same delivery, never a second one

    deliver(client, delivery, times=4)
    blocked = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert blocked["status"] == "BLOCKED"
    assert client.get(f"/v1/jobs/{task['job_id']}", headers=BOT).json()["state"] == "RECONCILING"
    # Not terminal: the Worker may have taken it, so its result is still accepted.
    assert blocked["runs"][0]["steps"][0]["attempts"][0]["status"] == "RUNNING"


def test_an_undelivered_job_still_accepts_the_real_result(client):
    task = create(client, action="product.plan", start=True)
    deliver(client, FakeDelivery("unknown"), times=7)
    assert client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["status"] == "BLOCKED"
    accepted = callback(client, task["job_id"], worker_job_id="worker-late")
    assert accepted.status_code == 202, accepted.text
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "COMPLETED"


# --- Callback provenance ---------------------------------------------------


def dispatched(client, **overrides):
    """A started task whose job records the delivery it was handed to."""
    task = create(client, start=True, **overrides)
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE jobs SET dispatch_id = %s, worker_job_id = 'worker-1', "
            "state = 'DISPATCHED' WHERE id = %s",
            (f"gateway:{task['job_id']}:1", task["job_id"]),
        )
    return task


def callback(client, job_id, **overrides):
    payload = {
        "event_id": f"event-{uuid.uuid4()}",
        "gateway_job_id": job_id,
        "dispatch_id": f"gateway:{job_id}:1",
        "worker_job_id": "worker-1",
        "event_type": "completed",
        "sequence": 1,
        "occurred_at": gateway.utcnow().isoformat(),
        "data": {"report": prd_report()},
        **overrides,
    }
    with mock.patch.object(gateway, "API_SURFACE", "callback"):
        return client.post(
            "/v1/worker-events",
            headers={"Authorization": "Bearer test-callback-credential"},
            json=payload,
        )


def test_callback_from_another_dispatch_is_rejected_and_quarantined(client):
    task = dispatched(client)
    response = callback(client, task["job_id"], dispatch_id="gateway:someone-else:1")
    assert response.status_code == 409
    assert "dispatch" in response.json()["detail"]
    quarantined = [
        event
        for event in client.get("/v1/events", headers=BOT).json()["events"]
        if event["type"] == "job.callback_quarantined"
    ]
    assert quarantined and quarantined[0]["payload"]["reason"].startswith("event does not")
    assert client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["status"] == "ACTIVE"


def test_callback_from_another_worker_execution_is_rejected(client):
    task = dispatched(client)
    response = callback(client, task["job_id"], worker_job_id="worker-somebody-else")
    assert response.status_code == 409
    assert "worker execution" in response.json()["detail"]


def test_matching_callback_is_accepted(client):
    task = dispatched(client, action="product.plan")
    accepted = callback(client, task["job_id"])
    assert accepted.status_code == 202, accepted.text
    assert client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["status"] == "COMPLETED"


# --- Output contract and quality evidence ---------------------------------


def test_completion_without_the_required_output_is_not_a_completion(client):
    task = create(client, action="product.plan", start=True)
    assert worker_event(client, task["job_id"], "completed", {}).status_code == 202
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    attempt = detail["runs"][0]["steps"][0]["attempts"][0]
    assert attempt["status"] == "FAILED"
    assert attempt["failure_class"] == "output_contract_unsatisfied"
    # The execution itself is still reported as having succeeded.
    assert attempt["result_summary"]["job_state"] == "SUCCEEDED"
    assert detail["status"] == "FAILED"
    assert detail["artifacts"] == []


def verdict_summary(client, task):
    return client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["runs"][0][
        "steps"
    ][0]["attempts"][0]["result_summary"]


def test_reported_pass_without_per_criterion_verdicts_is_not_a_pass(client):
    task = create(client, action="qa.review", start=True)
    # The criteria are named but not judged one by one.
    worker_event(
        client,
        task["job_id"],
        "completed",
        {"report": qa_report("pass", criteria=["CSV を取り込める"])},
    )
    summary = verdict_summary(client, task)
    assert summary["reported_verdict"] == "pass"
    assert summary["quality_verdict"] == "inconclusive"
    assert "per-criterion verdicts" in summary["downgraded_reason"]


def test_reported_pass_over_a_failing_criterion_is_not_a_pass(client):
    task = create(client, action="qa.review", start=True)
    worker_event(
        client,
        task["job_id"],
        "completed",
        {
            "report": qa_report(
                "pass",
                criteria=[
                    {"criterion": "取り込める", "verdict": "pass"},
                    {"criterion": "不正行を確認できる", "verdict": "fail"},
                ],
                target_digest="a" * 64,
                source_worker_job_id="worker-1",
            )
        },
    )
    summary = verdict_summary(client, task)
    assert summary["reported_verdict"] == "pass"
    assert summary["quality_verdict"] == "fail"
    assert "did not pass" in summary["downgraded_reason"]


def test_reported_pass_naming_an_unknown_target_is_not_a_pass(client):
    task = create(client, action="qa.review", start=True)
    worker_event(
        client,
        task["job_id"],
        "completed",
        {"report": qa_report("pass", target_artifact_id=str(uuid.uuid4()))},
    )
    summary = verdict_summary(client, task)
    assert summary["quality_verdict"] == "inconclusive"
    assert "target" in summary["downgraded_reason"]


def test_reported_pass_naming_another_projects_execution_is_not_a_pass(client):
    from tests.test_approvals import seed

    seed("other-product")
    task = create(client, action="qa.review", start=True)
    worker_event(
        client,
        task["job_id"],
        "completed",
        {"report": qa_report("pass", source_worker_job_id="worker-build-1")},
    )
    summary = verdict_summary(client, task)
    assert summary["quality_verdict"] == "inconclusive"
    assert "in this project" in summary["downgraded_reason"]


def test_pass_bound_to_this_projects_execution_is_accepted(client):
    """The QA request itself names the execution it reviewed, in this project."""
    build = create(client, action="code.build", start=True)
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE jobs SET worker_job_id = 'worker-build-for-qa' WHERE id = %s",
            (build["job_id"],),
        )
    # The change that execution produced, as this Gateway recorded it: that is what
    # a verification of it is bound to.
    worker_event(
        client, build["job_id"], "completed", build_result(), worker="worker-build-for-qa"
    )
    task = create(
        client,
        action="qa.review",
        parameters={"source_worker_job_id": "worker-build-for-qa"},
        start=True,
    )
    # The Task's own condition is part of what this action was asked for, so the
    # report has to judge it — and, as the real Worker does, it says which change it
    # rebuilt and verified.
    worker_event(
        client,
        task["job_id"],
        "completed",
        {
            "report": qa_report(
                "pass",
                criteria=[
                    {
                        "criterion": "正常な CSV から明細を登録できる",
                        "verdict": "pass",
                        "evidence": "tests.log",
                    }
                ],
                source_worker_job_id="worker-build-for-qa",
                verified_change={
                    "patch_digest": "b" * 64,
                    "base_commit": "0" * 40,
                    "rebuilt": True,
                },
            )
        },
    )
    summary = verdict_summary(client, task)
    assert (summary["reported_verdict"], summary["quality_verdict"]) == (
        "pass",
        "pass",
    ), summary


def test_a_verification_of_nothing_recorded_is_not_a_pass(client):
    """Without a recorded change to check it against, a pass establishes nothing."""
    # An execution of this project that this Gateway holds no recorded change for:
    # there is nothing for a report about it to be a verification of.
    with gateway.pool.connection() as db:
        db.execute(
            "INSERT INTO jobs (id, idempotency_key, project_id, action, environment, "
            "state, input, worker_job_id, created_at, updated_at) VALUES "
            "(%s, %s, %s, 'code.build', 'preview', 'SUCCEEDED', '{}', %s, now(), now())",
            (uuid.uuid4(), f"elsewhere-{uuid.uuid4()}", project("test-product"),
             "worker-built-elsewhere"),
        )
        db.commit()
    task = create(
        client,
        action="qa.review",
        parameters={"source_worker_job_id": "worker-built-elsewhere"},
        start=True,
    )
    worker_event(
        client,
        task["job_id"],
        "completed",
        {
            "report": qa_report(
                "pass",
                criteria=[
                    {
                        "criterion": "正常な CSV から明細を登録できる",
                        "verdict": "pass",
                        "evidence": "tests.log",
                    }
                ],
                source_worker_job_id="worker-built-elsewhere",
            )
        },
    )
    summary = verdict_summary(client, task)
    assert summary["reported_verdict"] == "pass"
    assert summary["quality_verdict"] == "inconclusive"
    assert "recorded" in summary["downgraded_reason"]


def test_a_single_action_is_judged_by_the_conditions_the_task_states(client):
    """A Task's conditions are the request, not a note beside it.

    A verification that answers only what the parameters happened to repeat has not
    covered what the person asked for, so its pass does not stand.
    """
    build = create(client, action="code.build", start=True)
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE jobs SET worker_job_id = 'worker-build-for-strict-qa' WHERE id = %s",
            (build["job_id"],),
        )
    # What the action was asked for reaches the Worker as well.
    with gateway.pool.connection() as db:
        requested = db.execute(
            "SELECT input FROM jobs WHERE id = %s", (uuid.UUID(build["job_id"]),)
        ).fetchone()["input"]
    assert requested["payload"]["parameters"]["acceptance_criteria"] == [
        "正常な CSV から明細を登録できる"
    ]

    task = create(
        client,
        action="qa.review",
        acceptance_criteria=["ログイン前に OTP を求める"],
        parameters={
            "source_worker_job_id": "worker-build-for-strict-qa",
            "acceptance_criteria": ["ログイン画面が表示される"],
        },
        start=True,
    )
    worker_event(
        client,
        task["job_id"],
        "completed",
        {
            "report": qa_report(
                "pass",
                criteria=[
                    {
                        "criterion": "ログイン画面が表示される",
                        "verdict": "pass",
                        "evidence": "tests.log",
                    }
                ],
            )
        },
    )
    summary = verdict_summary(client, task)
    assert summary["reported_verdict"] == "pass"
    assert summary["quality_verdict"] == "inconclusive"
    assert "OTP" in summary["downgraded_reason"]


def test_code_change_without_its_digests_is_recorded_as_a_report(client):
    task = create(client, action="code.build", start=True)
    # An executor that reports work without identifying the change it produced
    # (an older Worker) must not be recorded as a code-change manifest.
    worker_event(
        client,
        task["job_id"],
        "completed",
        build_result(patch_digest=None, workspace_digest=None),
    )
    artifact = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["artifacts"][0]
    assert artifact["kind"] == "code-change-report"


def test_code_change_manifest_is_recorded_when_its_digests_are_present(client):
    task = create(client, action="code.build", start=True)
    # What the build executor returns now: patch and workspace identity.
    worker_event(client, task["job_id"], "completed", build_result())
    artifact = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["artifacts"][0]
    assert artifact["kind"] == "code-change"


def test_video_artifact_keeps_the_verified_digest_size_and_expiry(client):
    # Video jobs are driven by the Gateway's own runner, not a Worker callback.
    task_id, job_id = video_job(client)
    with gateway.pool.connection() as db:
        job = db.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
        tasks.project_worker_event(
            db,
            job=job,
            event_type="completed",
            data={
                "artifact": {
                    "sha256": "d" * 64,
                    "bytes": 1234,
                    "media_type": "video/mp4",
                    "path": "/data/videos/x.mp4",
                    "retention_days": 7,
                }
            },
            occurred_at=gateway.utcnow(),
            actor="video-runner",
        )
        db.commit()
    artifact = client.get(f"/v1/tasks/{task_id}", headers=BOT).json()["artifacts"][0]
    assert artifact["digest"] == "d" * 64
    assert artifact["size"] == 1234
    assert artifact["media_type"] == "video/mp4"
    assert artifact["expires_at"] is not None


# --- Project isolation ----------------------------------------------------


def test_workflow_task_does_not_clear_another_task_release_candidate(client):
    from tests.test_approvals import candidate

    _body, approval_target = candidate(client)
    assert approval_target["target_sha256"]
    # A separate Task's build keeps its evidence in its own Run.
    create(client, action="code.build", start=True)
    project_state = client.get("/v1/projects/test-product", headers=BOT).json()
    assert project_state["release_candidate"] is not None
    assert project_state["state"] == "QA_PASSED"


def test_directly_submitted_build_still_invalidates_the_candidate(client):
    from tests.test_approvals import candidate

    candidate(client)
    client.post(
        "/v1/jobs",
        json={
            "action": "code.build",
            "project_id": "test-product",
            "environment": "preview",
        },
        headers={**BOT, "Idempotency-Key": "legacy-build-invalidates"},
    )
    assert client.get("/v1/projects/test-product", headers=BOT).json()["release_candidate"] is None


def test_workflow_completion_does_not_overwrite_project_evidence(client):
    from tests.test_approvals import seed

    seed()
    before = client.get("/v1/projects/test-product", headers=BOT).json()
    task = create(client, action="qa.review", start=True)
    worker_event(client, task["job_id"], "completed", {"report": {"verdict": "fail"}})
    after = client.get("/v1/projects/test-product", headers=BOT).json()
    assert after["qa_job_id"] == before["qa_job_id"]
    assert after["state"] == before["state"]
    assert after["tasks"]["task_count"] == 1


def test_qa_cannot_be_pointed_at_another_projects_workspace(client):
    from tests.test_approvals import seed

    seed("other-product")
    project("test-product")
    response = client.post(
        "/v1/jobs",
        json={
            "action": "qa.review",
            "project_id": "test-product",
            "environment": "preview",
            "parameters": {"source_worker_job_id": "worker-build-1"},
        },
        headers={**BOT, "Idempotency-Key": "cross-project-qa"},
    )
    assert response.status_code == 403
    assert "another project" in response.json()["detail"]


def test_unknown_source_execution_is_rejected(client):
    project()
    response = client.post(
        "/v1/jobs",
        json={
            "action": "qa.review",
            "project_id": "test-product",
            "environment": "preview",
            "parameters": {"source_worker_job_id": "worker-does-not-exist"},
        },
        headers={**BOT, "Idempotency-Key": "unknown-source-qa"},
    )
    assert response.status_code == 422


# --- Synchronisation contract ---------------------------------------------


def test_snapshot_lists_projects_without_tasks_and_their_state(client):
    project("empty-product")
    create(client, project_id="busy-product", start=True)
    snapshot = client.get("/v1/snapshot", headers=BOT).json()
    projects = {item["project_id"]: item for item in snapshot["projects"]}
    assert projects["empty-product"]["task_count"] == 0
    assert projects["empty-product"]["state"] == "REGISTERED"
    assert projects["busy-product"]["active_task_count"] == 1


def test_project_changes_are_published_on_the_shared_feed(client):
    created = client.post(
        "/v1/projects",
        json={
            "project_id": "feed-product",
            "title": "Feed",
            "idea": "A sufficiently long business idea for the feed test.",
        },
        headers=BOT,
    )
    assert created.status_code == 201, created.text
    events = client.get("/v1/events", headers=BOT).json()["events"]
    project_events = [event for event in events if event["aggregate_type"] == "project"]
    assert project_events
    assert project_events[0]["aggregate_id"] == "feed-product"


def test_an_uncommitted_event_hides_every_later_cursor(client):
    first = create(client)
    baseline = client.get("/v1/events", headers=BOT).json()["latest_cursor"]

    with gateway.pool.connection() as held:
        held.execute("BEGIN")
        tasks.record_event(
            held,
            aggregate_type="task",
            aggregate_id=first["task_id"],
            type="task.test_event",
            actor="test",
        )
        # While that allocation is uncommitted no reader may move past it.
        assert client.get("/v1/snapshot", headers=BOT).json()["cursor"] == baseline
        feed = client.get("/v1/events", params={"after": baseline}, headers=BOT).json()
        assert feed["events"] == []
        held.commit()

    feed = client.get("/v1/events", params={"after": baseline}, headers=BOT).json()
    assert [event["cursor"] for event in feed["events"]] == [baseline + 1]


def test_retried_creation_returns_the_original_task(client):
    project()
    body = {
        "project_id": "test-product",
        "title": "同じ依頼",
        "objective": "一度だけ登録する",
        "action": "product.plan",
        "start": True,
    }
    headers = {**BOT, "Idempotency-Key": "create-task-retry"}
    first = client.post("/v1/tasks", json=body, headers=headers).json()
    second = client.post("/v1/tasks", json=body, headers=headers).json()
    assert second["task_id"] == first["task_id"]
    assert second["job_id"] == first["job_id"]
    assert second["idempotent_replay"] is True
    with gateway.pool.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM tasks").fetchone()["n"] == 1
        assert db.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 1


def test_retried_creation_with_a_changed_request_is_rejected(client):
    project()
    headers = {**BOT, "Idempotency-Key": "create-task-changed"}
    body = {
        "project_id": "test-product",
        "title": "最初の依頼",
        "objective": "一度だけ登録する",
        "action": "product.plan",
    }
    assert client.post("/v1/tasks", json=body, headers=headers).status_code == 201
    changed = client.post(
        "/v1/tasks", json={**body, "title": "別の依頼"}, headers=headers
    )
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "REVISION_CONFLICT"


def test_job_cannot_be_linked_to_two_attempts(client):
    task = create(client, start=True)
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    attempt_id = detail["runs"][0]["steps"][0]["attempts"][0]["attempt_id"]
    project("second-product")
    other = client.post(
        "/v1/jobs",
        json={
            "action": "product.plan",
            "project_id": "second-product",
            "environment": "preview",
        },
        headers={**BOT, "Idempotency-Key": "second-job-for-attempt"},
    ).json()
    with pytest.raises(UniqueViolation):
        with gateway.pool.connection() as db:
            db.execute(
                "UPDATE jobs SET attempt_id = %s WHERE id = %s",
                (attempt_id, other["job_id"]),
            )


def test_workflow_definitions_reject_undeclared_output_references():
    broken = workflows.Workflow(
        id="broken",
        version="1.0.0",
        entry_step="plan",
        completion="output_contract",
        steps={
            "plan": workflows.Step(
                key="plan",
                stage="planning",
                action="product.plan",
                agent="a",
                output_contract="prd-v1",
                outputs=("prd",),
                on_success="build",
            ),
            "build": workflows.Step(
                key="build",
                stage="implementation",
                action="code.build",
                agent="b",
                output_contract="code-change-v1",
                input_from="plan.missing_output",
            ),
        },
    )
    with pytest.raises(workflows.WorkflowDefinitionError, match="not produced"):
        workflows.validate(broken)


def test_unverified_result_cannot_reach_deliverable_acceptance():
    broken = workflows.Workflow(
        id="broken",
        version="1.0.0",
        entry_step="qa",
        completion="accepted_deliverable",
        completion_requires=("qa_pass",),
        steps={
            "qa": workflows.Step(
                key="qa",
                stage="qa",
                action="qa.review",
                agent="q",
                output_contract="qa-report-v1",
                outputs=("report",),
                verification=True,
                on_success="review",
                on_inconclusive="review",
            ),
            "review": workflows.Step(
                key="review",
                stage="review",
                kind="human_review",
                decision="accept_deliverable",
            ),
        },
    )
    with pytest.raises(
        workflows.WorkflowDefinitionError, match="only a passing verification"
    ):
        workflows.validate(broken)
    assert workflows.MVP_BUILD_V1.steps["qa"].on_inconclusive == "request_input"


class RecordingConnection:
    """Proxies a real connection while recording the statements it runs."""

    def __init__(self, inner):
        self.inner = inner
        self.statements: list[str] = []

    def execute(self, query, params=None):
        self.statements.append(" ".join(str(query).split()))
        return self.inner.execute(query, params) if params is not None else self.inner.execute(query)

    def commit(self):
        self.inner.commit()

    def position(self, needle: str) -> int:
        return next(
            index for index, sql in enumerate(self.statements) if needle in sql
        )


def test_capacity_is_admitted_after_the_project_lock_and_before_any_event(
    client, monkeypatch
):
    # Taking video admission before the project lock let a Task creation and a
    # direct submission for one project wait on each other.
    monkeypatch.setattr(gateway.video, "configured", lambda: True)
    project()
    request = gateway.JobCreate(
        action="video.generate",
        project_id="test-product",
        environment="preview",
        parameters={"prompt": "a test prompt"},
    )
    with gateway.pool.connection() as db:
        recorder = RecordingConnection(db)
        job, created = gateway.insert_job(recorder, request, "lock-order-video")
        assert created
        tasks.attach_legacy_job(recorder, job=job, actor="test", source="api")
        db.commit()
    assert (
        recorder.position("FROM projects WHERE id = %s FOR UPDATE")
        < recorder.position("pg_advisory_xact_lock(734859203)")
        < recorder.position("UPDATE event_counter")
    )


def test_concurrent_task_creation_for_one_project_does_not_deadlock(client):
    project()
    def submit(index: int):
        return client.post(
            "/v1/tasks",
            json={
                "project_id": "test-product",
                "title": f"同時依頼 {index}",
                "objective": "並行して登録する",
                "action": "product.plan",
                "start": True,
            },
            headers={**BOT, "Idempotency-Key": f"concurrent-create-{index}"},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(submit, range(8)))
    assert [response.status_code for response in responses] == [201] * 8
    assert len(client.get("/v1/tasks", params={"limit": 20}, headers=BOT).json()["tasks"]) == 8


def test_a_delivery_outcome_is_classified_by_what_it_proves(client):
    refused = create(client, action="product.plan", start=True)
    deliver(client, FakeDelivery("reject"))
    assert client.get(f"/v1/tasks/{refused['task_id']}", headers=BOT).json()["status"] == "FAILED"


def test_a_dispatch_is_bound_before_the_worker_can_answer(client):
    task = create(client, action="product.plan", start=True)
    with gateway.pool.connection() as db:
        recorded = db.execute(
            "SELECT dispatch_id FROM jobs WHERE id = %s", (task["job_id"],)
        ).fetchone()["dispatch_id"]
    # Recorded when the delivery was queued, before anything could be sent.
    assert recorded == f"gateway:{task['job_id']}:1"
    assert callback(client, task["job_id"], dispatch_id="gateway:other:1").status_code == 409


def test_a_diagnostic_probe_satisfies_its_own_contract(client):
    task = create(
        client, action="code.build", parameters={"operation": "self_test"}, start=True
    )
    worker_event(
        client,
        task["job_id"],
        "completed",
        {
            "mode": "self_test",
            "executor": "build",
            "platform": "Linux",
            "commands": {"git": True, "codex": True},
        },
    )
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "COMPLETED"
    assert detail["artifacts"][0]["kind"] == "code-change-report"


def test_a_cursor_that_cannot_be_continued_requires_a_snapshot(client):
    create(client)
    latest = client.get("/v1/events", headers=BOT).json()["latest_cursor"]
    ahead = client.get("/v1/events", params={"after": latest + 5}, headers=BOT)
    assert ahead.status_code == 400
    assert ahead.json()["detail"]["code"] == "SNAPSHOT_REQUIRED"

    with gateway.pool.connection() as db:
        db.execute("DELETE FROM platform_events WHERE cursor <= %s", (latest,))
        create_again = None
    create(client)
    # The events between an old cursor and the retained feed are gone.
    stale = client.get("/v1/events", params={"after": 0}, headers=BOT)
    assert stale.status_code == 400
    assert stale.json()["detail"]["code"] == "SNAPSHOT_REQUIRED"
    assert create_again is None


def test_retried_mcp_creation_without_a_key_returns_the_same_task(client):
    project()
    arguments = {
        "project_id": "test-product",
        "title": "同じ依頼",
        "objective": "一度だけ登録する",
        "action": "product.plan",
        "start": True,
    }
    first = mcp_call(client, "create_task", arguments)
    second = mcp_call(client, "create_task", arguments)
    assert second["task_id"] == first["task_id"]
    assert second["idempotent_replay"] is True
    with gateway.pool.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 1


def test_a_deliberately_repeated_request_can_still_be_registered(client):
    project()
    arguments = {
        "project_id": "test-product",
        "title": "同じ依頼",
        "objective": "もう一度実行する",
        "action": "product.plan",
    }
    first = mcp_call(client, "create_task", arguments)
    second = mcp_call(
        client, "create_task", {**arguments, "idempotency_key": "grok-second-run"}
    )
    assert second["task_id"] != first["task_id"]


def test_workflow_definitions_reject_unknown_bare_references():
    broken = workflows.Workflow(
        id="broken",
        version="1.0.0",
        entry_step="build",
        completion="output_contract",
        steps={
            "build": workflows.Step(
                key="build",
                stage="implementation",
                action="code.build",
                agent="b",
                output_contract="code-change-v1",
                input_from="whatever_the_agent_finds",
            )
        },
    )
    with pytest.raises(workflows.WorkflowDefinitionError, match="not produced"):
        workflows.validate(broken)


def test_deliverable_acceptance_requires_a_declared_verification():
    broken = workflows.Workflow(
        id="broken",
        version="1.0.0",
        entry_step="build",
        completion="accepted_deliverable",
        completion_requires=("qa_pass",),
        steps={
            "build": workflows.Step(
                key="build",
                stage="implementation",
                action="code.build",
                agent="b",
                output_contract="code-change-v1",
                on_success="review",
            ),
            "review": workflows.Step(
                key="review",
                stage="review",
                kind="human_review",
                decision="accept_deliverable",
            ),
        },
    )
    with pytest.raises(workflows.WorkflowDefinitionError, match="declared as the verification"):
        workflows.validate(broken)


def test_accepting_a_deliverable_must_state_its_quality_gate():
    broken = workflows.Workflow(
        id="broken",
        version="1.0.0",
        entry_step="review",
        completion="accepted_deliverable",
        steps={
            "review": workflows.Step(
                key="review",
                stage="review",
                kind="human_review",
                decision="accept_deliverable",
            )
        },
    )
    with pytest.raises(workflows.WorkflowDefinitionError, match="quality gate"):
        workflows.validate(broken)


def test_a_pause_defers_completion_until_the_task_is_resumed(client):
    task = create(client, action="product.plan", start=True)
    paused = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "pause"},
        headers={**BOT, "Idempotency-Key": "pause-single-action"},
    )
    assert paused.status_code == 202
    assert paused.json()["result"]["control_state"] == "PAUSE_REQUESTED"

    worker_event(client, task["job_id"], "completed", {"report": prd_report()})
    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    # The execution finished, but a paused Task does not complete itself.
    assert detail["control_state"] == "PAUSED"
    assert detail["status"] != "COMPLETED"
    assert detail["runs"][0]["steps"][0]["attempts"][0]["status"] == "SUCCEEDED"
    assert detail["runs"][0]["status"] == "ACTIVE"

    resumed = client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "resume"},
        headers={**BOT, "Idempotency-Key": "resume-single-action"},
    )
    assert resumed.status_code == 202
    final = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert final["status"] == "COMPLETED"
    assert final["stage_key"] == "done"
    assert final["artifacts"][0]["kind"] == "prd"


def test_a_delivery_claimed_by_a_stopped_scheduler_is_retried(client):
    task = create(client, start=True)
    with gateway.pool.connection() as db:
        # A scheduler claimed this delivery and never settled it.
        db.execute(
            "UPDATE job_dispatches SET state = 'SENDING', attempts = 1, updated_at = %s",
            (gateway.utcnow() - gateway.timedelta(seconds=tasks.CLAIM_LEASE_SECONDS + 5),),
        )
    delivery = FakeDelivery(worker_job_id="worker-after-recovery")
    scheduler.deliver_once(gateway.pool, delivery)
    assert [dispatch["dispatch_id"] for dispatch in delivery.sent] == [
        f"gateway:{task['job_id']}:1"
    ]
    job = client.get(f"/v1/jobs/{task['job_id']}", headers=BOT).json()
    assert job["state"] == "DISPATCHED"
    assert job["worker_job_id"] == "worker-after-recovery"


def test_a_stop_request_claimed_by_a_stopped_scheduler_is_retried(client):
    task = create(client, start=True)
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE jobs SET worker_job_id = 'worker-running', state = 'RUNNING' WHERE id = %s",
            (task["job_id"],),
        )
    client.post(
        f"/v1/tasks/{task['task_id']}/commands",
        json={"type": "cancel"},
        headers={**BOT, "Idempotency-Key": "cancel-for-recovery"},
    )
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE worker_commands SET state = 'SENDING', attempts = 1, updated_at = %s",
            (gateway.utcnow() - gateway.timedelta(seconds=tasks.CLAIM_LEASE_SECONDS + 5),),
        )

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


def restart_bootstrap():
    """What a restarting process does to the database: re-apply the schema.

    Every piece of state lives in the database, so this is the whole of what a
    restart changes; the test client itself is left alone because closing its
    lifespan would close the pool the rest of the suite shares.
    """
    with gateway.pool.connection() as db:
        db.execute(gateway.SCHEMA_SQL)
        tasks.ensure_schema(db)
        db.commit()


def test_state_survives_a_restart_without_repeating_work(client):
    """A restart continues from the database instead of losing or doubling work."""
    task = create(client, action="product.plan", start=True)
    with gateway.pool.connection() as db:
        pending = db.execute(
            "SELECT state FROM job_dispatches WHERE job_id = %s", (task["job_id"],)
        ).fetchone()
    assert pending["state"] == "PENDING"

    restart_bootstrap()

    detail = client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()
    assert detail["status"] == "ACTIVE"
    assert detail["runs"][0]["steps"][0]["attempts"][0]["status"] == "RUNNING"

    # The delivery is still owed, and it is delivered exactly once.
    delivery = FakeDelivery(worker_job_id="worker-after-restart")
    deliver(client, delivery)
    assert len(delivery.sent) == 1
    worker_event(
        client,
        task["job_id"],
        "completed",
        {"report": prd_report()},
        worker="worker-after-restart",
    )
    assert client.get(f"/v1/tasks/{task['task_id']}", headers=BOT).json()["status"] == "COMPLETED"

    with gateway.pool.connection() as db:
        counts = db.execute(
            "SELECT (SELECT count(*) FROM jobs) AS jobs, "
            "(SELECT count(*) FROM step_attempts) AS attempts, "
            "(SELECT count(*) FROM job_dispatches) AS dispatches"
        ).fetchone()
    # One request, one execution, one delivery.
    assert (counts["jobs"], counts["attempts"], counts["dispatches"]) == (1, 1, 1)


def test_a_resent_submission_after_a_restart_returns_the_same_job(client):
    project()
    body = {
        "action": "product.plan",
        "project_id": "test-product",
        "environment": "preview",
    }
    headers = {**BOT, "Idempotency-Key": "submit-across-restart"}
    first = client.post("/v1/jobs", json=body, headers=headers).json()
    restart_bootstrap()
    replay = client.post("/v1/jobs", json=body, headers=headers).json()
    assert replay["job_id"] == first["job_id"]
    assert replay["task_id"] == first["task_id"]
    with gateway.pool.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 1


def test_the_gateway_says_when_nothing_can_run(client):
    inventory = client.get("/v1/workers", headers=BOT).json()
    # Nothing has reported yet, and that is said plainly rather than implied.
    assert inventory["workers"] == []
    assert inventory["can_execute"] is False
    assert "no worker has reported" in inventory["reason"]


def test_a_worker_report_is_recorded_with_how_old_it_is(client):
    class ReportingWorker:
        def configured(self):
            return True

        def set_intake(self, **_kwargs):
            raise AssertionError("no intake state was requested")

        def status(self):
            return {
                "logical_id": "ai-business-worker",
                "pool": "default",
                "instance_id": "abc123",
                "accepting_jobs": True,
                "intake_revision": 4,
                "max_concurrency": 1,
                "running": 1,
                "queued": 2,
                "callback_backlog": 0,
                "actions": ["code.build", "qa.review"],
            }

    scheduler.poll_worker(gateway.pool, ReportingWorker())
    inventory = client.get("/v1/workers", headers=BOT).json()
    (worker,) = inventory["workers"]
    assert worker["logical_id"] == "ai-business-worker"
    assert worker["connection_status"] == "HEALTHY"
    assert worker["accepting_jobs"] is True
    assert worker["queued"] == 2
    assert "qa.review" in worker["actions"]
    assert inventory["can_execute"] is True

    # A report that has gone quiet is unknown, not healthy.
    with gateway.pool.connection() as db:
        db.execute(
            "UPDATE workers SET observed_at = %s",
            (gateway.utcnow() - gateway.timedelta(seconds=tasks.WORKER_STALE_SECONDS + 5),),
        )
    stale = client.get("/v1/workers", headers=BOT).json()
    assert stale["workers"][0]["connection_status"] == "UNKNOWN"
    assert stale["workers"][0]["accepting_jobs"] is None
    assert stale["can_execute"] is False


def test_an_unreachable_worker_is_reported_as_such(client):
    class SilentWorker:
        def configured(self):
            return True

        def set_intake(self, **_kwargs):
            raise AssertionError("no intake state was requested")

        def status(self):
            raise scheduler.Unknown("worker did not answer the status request")

    scheduler.poll_worker(gateway.pool, SilentWorker())
    inventory = client.get("/v1/workers", headers=BOT).json()
    (worker,) = inventory["workers"]
    assert worker["reported_status"] == "UNREACHABLE"
    assert "did not answer" in worker["detail"]
    assert inventory["can_execute"] is False


def test_a_drained_worker_means_nothing_can_run(client):
    class DrainedWorker:
        def configured(self):
            return True

        def set_intake(self, **_kwargs):
            raise AssertionError("no intake state was requested")

        def status(self):
            return {
                "logical_id": "ai-business-worker",
                "accepting_jobs": False,
                "intake_revision": 7,
                "actions": ["code.build"],
            }

    scheduler.poll_worker(gateway.pool, DrainedWorker())
    inventory = client.get("/v1/workers", headers=BOT).json()
    assert inventory["workers"][0]["accepting_jobs"] is False
    assert inventory["can_execute"] is False
    assert "accepting jobs" in inventory["reason"]


class IntakeWorker:
    """A Worker that applies intake changes and reports what it applied."""

    def __init__(self, accepting=True, revision=0, refuse_older=True):
        self.accepting = accepting
        self.revision = revision
        self.refuse_older = refuse_older
        self.applied = []

    def configured(self):
        return True

    def set_intake(self, *, accepting_jobs, revision, actor, reason):
        if self.refuse_older and revision <= self.revision:
            raise scheduler.Rejected("the worker already applied a newer intake revision")
        self.applied.append({"accepting_jobs": accepting_jobs, "revision": revision})
        self.accepting = accepting_jobs
        self.revision = revision
        return {"accepting_jobs": accepting_jobs, "intake_revision": revision}

    def status(self):
        return {
            "logical_id": "ai-business-worker",
            "accepting_jobs": self.accepting,
            "intake_revision": self.revision,
            "actions": ["code.build"],
        }


def test_draining_a_worker_is_asked_for_here_and_applied_there(client, monkeypatch):
    monkeypatch.setenv("CONFIG_ADMIN_TOKEN", "c" * 64)
    monkeypatch.setenv("CONFIG_ADMIN_ACTOR", "operator")
    worker = IntakeWorker()
    scheduler.poll_worker(gateway.pool, worker)
    assert client.get("/v1/workers", headers=BOT).json()["can_execute"] is True

    drained = client.post(
        "/v1/workers/ai-business-worker/commands",
        json={"type": "drain", "reason": "メンテナンス"},
        headers={**BOT, "X-Config-Admin-Token": "c" * 64},
    )
    assert drained.status_code == 202
    assert drained.json()["desired_accepting_jobs"] is False
    # Asked for, not yet applied: the two are reported separately.
    inventory = client.get("/v1/workers", headers=BOT).json()["workers"][0]
    assert inventory["desired_accepting_jobs"] is False
    assert inventory["intake_applied"] is False

    scheduler.poll_worker(gateway.pool, worker)
    assert worker.applied[-1]["accepting_jobs"] is False
    applied = client.get("/v1/workers", headers=BOT).json()
    assert applied["workers"][0]["intake_applied"] is True
    assert applied["workers"][0]["accepting_jobs"] is False
    assert applied["can_execute"] is False
    assert applied["workers"][0]["desired_reason"] == "メンテナンス"

    # Nothing re-sends it once applied, and resuming is another revision.
    before = len(worker.applied)
    scheduler.poll_worker(gateway.pool, worker)
    assert len(worker.applied) == before
    client.post(
        "/v1/workers/ai-business-worker/commands",
        json={"type": "resume"},
        headers={**BOT, "X-Config-Admin-Token": "c" * 64},
    )
    scheduler.poll_worker(gateway.pool, worker)
    assert worker.applied[-1]["accepting_jobs"] is True
    assert client.get("/v1/workers", headers=BOT).json()["can_execute"] is True


def test_an_unapplied_drain_stays_visible_as_unapplied(client, monkeypatch):
    monkeypatch.setenv("CONFIG_ADMIN_TOKEN", "c" * 64)
    monkeypatch.setenv("CONFIG_ADMIN_ACTOR", "operator")
    class RefusingWorker(IntakeWorker):
        def set_intake(self, **_kwargs):
            raise scheduler.Unknown("worker did not answer the intake change")

    worker = RefusingWorker()
    scheduler.poll_worker(gateway.pool, worker)
    client.post(
        "/v1/workers/ai-business-worker/commands",
        json={"type": "drain"},
        headers={**BOT, "X-Config-Admin-Token": "c" * 64},
    )
    scheduler.poll_worker(gateway.pool, worker)
    inventory = client.get("/v1/workers", headers=BOT).json()["workers"][0]
    # The Worker is still accepting, and the board can see the request is pending.
    assert inventory["accepting_jobs"] is True
    assert inventory["desired_accepting_jobs"] is False
    assert inventory["intake_applied"] is False


def test_a_worker_command_is_recorded_as_an_event(client, monkeypatch):
    monkeypatch.setenv("CONFIG_ADMIN_TOKEN", "c" * 64)
    monkeypatch.setenv("CONFIG_ADMIN_ACTOR", "operator")
    client.post(
        "/v1/workers/ai-business-worker/commands",
        json={"type": "drain", "reason": "点検"},
        headers={**BOT, "X-Config-Admin-Token": "c" * 64},
    )
    events = [
        event
        for event in client.get("/v1/events", headers=BOT).json()["events"]
        if event["aggregate_type"] == "worker"
    ]
    assert events[-1]["type"] == "worker.intake_requested"
    assert events[-1]["payload"]["reason"] == "点検"


def test_the_one_open_run_index_is_replaced_on_an_existing_database(client):
    """Applying the schema to an older database must update the index predicate.

    `CREATE INDEX IF NOT EXISTS` keeps an index that already exists under the same
    name, so a database created before SUPERSEDED became terminal would keep
    treating a superseded Run as the one open Run and refuse the replacement.
    """
    with gateway.pool.connection() as db:
        db.execute("DROP INDEX IF EXISTS workflow_runs_one_open_per_task_v2")
        db.execute(
            "CREATE UNIQUE INDEX workflow_runs_one_open_per_task ON workflow_runs(task_id) "
            "WHERE status NOT IN ('COMPLETED','FAILED','CANCELLED')"
        )
        db.commit()
    with gateway.pool.connection() as db:
        db.execute(tasks.SCHEMA_SQL)
        db.commit()
    with gateway.pool.connection() as db:
        rows = db.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'workflow_runs' "
            "AND (indexname LIKE '%%one_open%%' OR indexname LIKE '%%one_active%%')"
        ).fetchall()
    names = {row["indexname"] for row in rows}
    assert names == {"workflow_runs_one_open_per_task_v2"}
    assert "SUPERSEDED" in rows[0]["indexdef"]


def test_capacity_held_by_an_unconfirmed_execution_is_not_offered(client):
    """A slot held by an execution nobody could stop is not capacity.

    The Worker counts it against its own concurrency; the Gateway must say the same,
    or the board shows a Worker that cannot actually take work as ready.
    """
    class HoldingWorker:
        def configured(self):
            return True

        def set_intake(self, **_kwargs):
            raise AssertionError("no intake state was requested")

        def status(self):
            return {
                "logical_id": "ai-business-worker",
                "pool": "default",
                "instance_id": "abc123",
                "accepting_jobs": True,
                "intake_revision": 1,
                "max_concurrency": 1,
                "running": 0,
                "not_stopped": 1,
                "queued": 0,
                "callback_backlog": 0,
                "actions": ["code.build"],
            }

    scheduler.poll_worker(gateway.pool, HoldingWorker())
    inventory = client.get("/v1/workers", headers=BOT).json()
    (worker,) = inventory["workers"]
    assert worker["not_stopped"] == 1
    assert worker["usable_slots"] == 0
    assert inventory["can_execute"] is False
    assert "could not be confirmed stopped" in inventory["reason"]
