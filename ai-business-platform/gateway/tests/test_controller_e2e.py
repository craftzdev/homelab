"""The real Workflow Controller driving a real Gateway.

The Gateway decides what may happen next and the Controller decides when to look;
this exercises both implementations together, so a contract the two disagree
about fails here rather than in the cluster. The Controller's own code is used
unchanged — only its transport is pointed at this Gateway instead of HTTP.

Skipped unless the Agent repository is mounted: run it with

    docker compose -f compose.e2e.yaml run --rm e2e
"""

import importlib.util
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from app import main as gateway
from app import scheduler

BOT = {"Authorization": "Bearer test-gateway-credential"}
HUMAN = {**BOT, "X-Human-Approval-Token": "test-human-credential"}
CONTROLLER = {"Authorization": "Bearer test-controller-credential"}


def _load_agent_controller(agent_path: str):
    """Load the Agent's Controller from its own files.

    Both repositories ship a package called `app`, and the Gateway's is already
    imported, so the Agent's modules are loaded by path and registered under the
    names its own imports use.
    """
    root = Path(agent_path) / "app"
    if not (root / "workflow_controller.py").is_file():
        return None
    loaded = None
    for name in ("context_builder", "workflow_controller"):
        spec = importlib.util.spec_from_file_location(f"app.{name}", root / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"app.{name}"] = module
        spec.loader.exec_module(module)
        loaded = module
    return loaded


def _load_agent_app(agent_path: str):
    """Load the Agent's dispatch service from its own files.

    Its configuration is read at import time, so the environment is set first.
    The registry, profiles and schemas are the repository's real ones: a handoff
    the Controller builds has to satisfy them here, not only in the Gateway.
    """
    root = Path(agent_path)
    if not (root / "app" / "main.py").is_file():
        return None
    os.environ.update(
        {
            "AGENT_API_TOKEN": "agent-e2e-credential",
            "WORKER_API_TOKEN": "worker-e2e-credential",
            "WORKER_BASE_URL": "http://worker.e2e",
            "AGENT_DATA_DIR": tempfile.mkdtemp(prefix="agent-e2e-"),
            "AGENT_REGISTRY_FILE": str(root / "profiles/capabilities.yaml"),
            "AGENT_PROFILES_DIR": str(root / "profiles"),
            "AGENT_SCHEMAS_DIR": str(root / "schemas"),
        }
    )
    # `app.registry` is the name the Agent's own imports use; the Gateway has no
    # module of that name, so registering it collides with nothing.
    for name, module_name in (("registry", "app.registry"), ("main", "agent_app_main")):
        spec = importlib.util.spec_from_file_location(
            module_name, root / "app" / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return sys.modules["agent_app_main"]


controller_module = _load_agent_controller(os.environ.get("AGENT_APP_PATH", ""))
if controller_module is None:
    pytest.skip(
        "mount the Agent repository and set AGENT_APP_PATH to run this",
        allow_module_level=True,
    )
agent_module = _load_agent_app(os.environ.get("AGENT_APP_PATH", ""))


class GatewayOverTestClient(controller_module.GatewayClient):
    """The Controller's transport, pointed at this Gateway's internal surface."""

    def __init__(self, client):
        self.client = client
        self.base_url = ""
        self.token = "test-controller-credential"
        self.timeout = 5

    def _request(self, method, path, body=None):
        with mock.patch.object(gateway, "API_SURFACE", "internal"):
            response = self.client.request(method, path, json=body, headers=CONTROLLER)
        if response.status_code >= 400:
            detail = response.json().get("detail")
            code = detail.get("code") if isinstance(detail, dict) else None
            raise controller_module.GatewayError(
                f"gateway returned HTTP {response.status_code}",
                code=code,
                status=response.status_code,
            )
        return response.json()


def project(project_id="e2e-product"):
    now = gateway.utcnow()
    with gateway.pool.connection() as db:
        db.execute(
            "INSERT INTO projects (id,title,idea,state,created_at,updated_at) "
            "VALUES (%s,'e2e','an end to end business idea','REGISTERED',%s,%s) "
            "ON CONFLICT DO NOTHING",
            (project_id, now, now),
        )
    return project_id


def _stamped(job_id, data):
    """What the real Worker adds to a verification report it produces.

    It names the execution it verified and the patch it rebuilt; the Gateway checks
    both against what it recorded, so a fixture that omitted them would be testing a
    report no Worker produces.
    """
    report = (data or {}).get("report")
    if not isinstance(report, dict) or "verdict" not in report:
        return data
    with gateway.pool.connection() as db:
        attempt = db.execute(
            "SELECT input_manifest FROM step_attempts WHERE job_id = %s", (job_id,)
        ).fetchone()
    manifest = (attempt or {}).get("input_manifest") or {}
    stamped = dict(report)
    source = (manifest.get("parameters") or {}).get("source_worker_job_id")
    if source:
        stamped.setdefault("source_worker_job_id", source)
    bound = manifest.get("verification_target") or {}
    if bound.get("artifact_id"):
        with gateway.pool.connection() as db:
            artifact = db.execute(
                "SELECT manifest FROM artifacts WHERE id = %s",
                (uuid.UUID(str(bound["artifact_id"])),),
            ).fetchone()
        content = ((artifact or {}).get("manifest") or {}).get("content") or {}
        if content.get("patch_digest"):
            stamped.setdefault(
                "verified_change",
                {
                    "patch_digest": content["patch_digest"],
                    "base_commit": content.get("base_commit"),
                    "rebuilt": True,
                },
            )
    return {**data, "report": stamped}


def worker_report(client, job_id, data, worker):
    """Report a Worker completion for the job the Gateway queued for delivery.

    A verification report names the execution it verified, because the real
    Worker stamps the source it was given into the report it returns.
    """
    data = _stamped(job_id, data)
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


def latest_job(step_key):
    """The job the Controller's proposal created for one step."""
    with gateway.pool.connection() as db:
        row = db.execute(
            """
            SELECT a.job_id FROM step_attempts a
              JOIN workflow_steps s ON s.id = a.step_id
             WHERE s.logical_key = %s
             ORDER BY a.created_at DESC LIMIT 1
            """,
            (step_key,),
        ).fetchone()
    assert row is not None, f"no attempt was created for {step_key}"
    return str(row["job_id"])


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


def build_result():
    return {
        "mode": "codex",
        "succeeded": True,
        "project_id": "e2e-product",
        "base_commit": "0" * 40,
        "changed_files": ["app/main.py"],
        "tests": [{"name": "pytest", "passed": True}],
        "patch_digest": "b" * 64,
        "workspace_digest": "c" * 64,
        "change_manifest": [{"path": "app/main.py", "sha256": "d" * 64, "state": "present"}],
        "artifacts": ["changes.patch"],
    }


def qa_report(verdict, criteria=None):
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


def test_the_controller_and_gateway_drive_one_request_to_acceptance(client):
    controller = controller_module.Controller(
        GatewayOverTestClient(client), owner="e2e-controller"
    )
    # The Gateway only starts a Controller-driven Workflow while a Controller is
    # actually evaluating Runs.
    controller.tick()

    project()
    created = client.post(
        "/v1/tasks",
        json={
            "project_id": "e2e-product",
            "title": "CSV 取り込みの MVP を作る",
            "objective": "ユーザーが CSV を選択して明細を登録できるようにする",
            "acceptance_criteria": ["正常な CSV から明細を登録できる"],
            "workflow_id": "mvp-build-v1",
            "start": True,
        },
        headers={**BOT, "Idempotency-Key": f"e2e-{uuid.uuid4()}"},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]

    # 1. The Controller proposes the plan the Gateway allows, with input it built
    #    from the Task's own objective and criteria.
    outcome = controller.tick()
    assert [item["applied"] for item in outcome["applied"]] == ["create_attempt"]
    with gateway.pool.connection() as db:
        manifest = db.execute(
            "SELECT input_manifest FROM step_attempts ORDER BY created_at DESC LIMIT 1"
        ).fetchone()["input_manifest"]
    assert "CSV" in manifest["parameters"]["idea"]
    worker_report(client, latest_job("plan"), {"report": prd_report()}, "worker-plan")

    # 2. Implementation, handed the PRD it must follow.
    assert [item["applied"] for item in controller.tick()["applied"]] == ["create_attempt"]
    with gateway.pool.connection() as db:
        manifest = db.execute(
            "SELECT input_manifest FROM step_attempts ORDER BY created_at DESC LIMIT 1"
        ).fetchone()["input_manifest"]
    assert manifest["input_artifacts"], "the build was not given the PRD"
    worker_report(client, latest_job("implement"), build_result(), "worker-build")

    # 3. Verification, bound to the execution that produced the change.
    assert [item["applied"] for item in controller.tick()["applied"]] == ["create_attempt"]
    with gateway.pool.connection() as db:
        manifest = db.execute(
            "SELECT input_manifest FROM step_attempts ORDER BY created_at DESC LIMIT 1"
        ).fetchone()["input_manifest"]
    assert manifest["parameters"]["source_worker_job_id"] == "worker-build"
    # QA fails, so the Workflow returns to a fix rather than to acceptance.
    worker_report(client, latest_job("qa"), {"report": qa_report("fail")}, "worker-qa")

    # 4. The fix continues the change it was given.
    assert [item["applied"] for item in controller.tick()["applied"]] == ["create_attempt"]
    with gateway.pool.connection() as db:
        manifest = db.execute(
            "SELECT input_manifest FROM step_attempts ORDER BY created_at DESC LIMIT 1"
        ).fetchone()["input_manifest"]
    assert manifest["parameters"]["source_worker_job_id"] == "worker-build"
    assert "未達の受け入れ条件" in manifest["parameters"]["task"]
    worker_report(client, latest_job("fix"), build_result(), "worker-fix")

    # 5. Verification again, now against the fixed change, and it passes.
    assert [item["applied"] for item in controller.tick()["applied"]] == ["create_attempt"]
    worker_report(client, latest_job("qa"), {"report": qa_report("pass")}, "worker-qa-2")

    # 6. A passing verification asks for the human decision.
    assert [item["applied"] for item in controller.tick()["applied"]] == ["request_review"]
    detail = client.get(f"/v1/tasks/{task_id}", headers=BOT).json()
    assert detail["status"] == "WAITING_REVIEW"
    target = next(
        item["target_digest"]
        for item in detail["available_commands"]
        if item["type"] == "accept_deliverable"
    )

    # 7. The human accepts, and only a human can.
    assert client.post(
        f"/v1/tasks/{task_id}/commands",
        json={"type": "accept_deliverable", "target_digest": target},
        headers={**BOT, "Idempotency-Key": "e2e-accept-bot"},
    ).status_code == 401
    accepted = client.post(
        f"/v1/tasks/{task_id}/commands",
        json={"type": "accept_deliverable", "target_digest": target},
        headers={**HUMAN, "Idempotency-Key": "e2e-accept"},
    )
    assert accepted.status_code == 202, accepted.text

    final = client.get(f"/v1/tasks/{task_id}", headers=BOT).json()
    assert final["status"] == "COMPLETED"
    assert final["stage_key"] == "done"
    assert [step["logical_key"] for step in final["runs"][0]["steps"]] == [
        "plan",
        "implement",
        "qa",
        "fix",
        "qa",
        "review",
    ]
    assert {artifact["kind"] for artifact in final["artifacts"]} == {
        "prd",
        "code-change",
        "qa-report",
    }
    # Nothing is left for the Controller, and the audit trail is complete.
    assert controller.tick()["applied"] == []
    types = [
        event["type"]
        for event in client.get("/v1/events", params={"limit": 500}, headers=BOT).json()[
            "events"
        ]
    ]
    for expected in (
        "task.created",
        "task.run_started",
        "task.attempt_created",
        "task.attempt_completed",
        "task.review_requested",
        "task.completed",
    ):
        assert expected in types, expected


def test_the_controller_reports_what_it_cannot_build(client):
    """A Run missing what a step needs is reported, not advanced with guesses."""
    controller = controller_module.Controller(
        GatewayOverTestClient(client), owner="e2e-controller"
    )
    controller.tick()
    project()
    created = client.post(
        "/v1/tasks",
        json={
            "project_id": "e2e-product",
            "title": "目的が短い依頼",
            "objective": "短い",
            "workflow_id": "mvp-build-v1",
            "start": True,
        },
        headers={**BOT, "Idempotency-Key": f"e2e-short-{uuid.uuid4()}"},
    )
    assert created.status_code == 201, created.text
    outcome = controller.tick()
    assert outcome["applied"] == []
    assert "at least 20" in json.dumps(outcome["skipped"], ensure_ascii=False)
    # Nothing was executed on the strength of an objective nobody can work from.
    with gateway.pool.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM step_attempts").fetchone()["n"] == 0


class AgentDelivery(scheduler.Delivery):
    """The Gateway's delivery, pointed at the Agent running in this process."""

    def __init__(self, agent_client):
        self.base_url = "http://agent.e2e"
        self.token = "agent-e2e-credential"
        self.timeout = 5
        self.client = agent_client

    def configured(self) -> bool:
        return True

    def send(self, dispatch):
        response = self.client.post(
            f"/v1/jobs/{dispatch['endpoint']}",
            json=dispatch["payload"],
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if response.status_code >= 400:
            if response.status_code < 500 and response.status_code not in {408, 429}:
                raise scheduler.Rejected(
                    f"agent refused the dispatch: HTTP {response.status_code} {response.text}"
                )
            raise scheduler.Unknown(f"agent unavailable: HTTP {response.status_code}")
        return response.json()["worker_job_id"]


class WorkerDouble:
    """The Worker's side of the dispatch contract, without running Codex.

    It records exactly what the Agent forwarded, so the parameters the Controller
    built and the Agent validated are the ones checked here.
    """

    def __init__(self):
        self.received = []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data)
        self.received.append(body)
        worker_job_id = f"wjob_{uuid.uuid4().hex}"
        return _HttpResponse(
            {"worker_job_id": worker_job_id, "status": "ACCEPTED"}, status=202
        )


class _HttpResponse:
    def __init__(self, body, status=200):
        self.body = json.dumps(body).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_args):
        return self.body


def deliver(pool, agent_client):
    """Run the Gateway's dispatch loop once, through the Agent."""
    return scheduler.deliver_once(pool, AgentDelivery(agent_client), limit=5)


def report_from_worker(client, payload, data, worker_job_id):
    """The callback the Worker sends for what it was actually given."""
    data = _stamped(uuid.UUID(payload["gateway_job_id"]), data)
    with mock.patch.object(gateway, "API_SURFACE", "callback"):
        response = client.post(
            "/v1/worker-events",
            headers={"Authorization": "Bearer test-callback-credential"},
            json={
                "event_id": f"event-{uuid.uuid4()}",
                "gateway_job_id": payload["gateway_job_id"],
                "dispatch_id": payload["dispatch_id"],
                "worker_job_id": worker_job_id,
                "event_type": "completed",
                "sequence": 1,
                "occurred_at": gateway.utcnow().isoformat(),
                "data": data,
            },
        )
    assert response.status_code == 202, response.text


@pytest.mark.skipif(agent_module is None, reason="the Agent application is not mounted")
def test_the_handoffs_the_controller_builds_are_accepted_by_the_agent(
    client, monkeypatch
):
    """Gateway, Controller and Agent, each running its own code.

    The Controller builds every step's input, the Gateway admits and queues the
    dispatch, and the Agent validates it against its registry and JSON schemas
    before forwarding it. A handoff either of them disagrees about fails here.
    """
    worker = WorkerDouble()
    monkeypatch.setattr(agent_module.urllib.request, "urlopen", worker)
    with TestClient(agent_module.app) as agent_client:
        controller = controller_module.Controller(
            GatewayOverTestClient(client), owner="e2e-agent-controller"
        )
        controller.tick()
        project()
        created = client.post(
            "/v1/tasks",
            json={
                "project_id": "e2e-product",
                "title": "CSV 取り込みの MVP を作る",
                "objective": "ユーザーが CSV を選択して明細を登録できるようにする",
                "acceptance_criteria": ["正常な CSV から明細を登録できる"],
                "workflow_id": "mvp-build-v1",
                "limits": {"timeout_seconds": 900},
                "start": True,
            },
            headers={**BOT, "Idempotency-Key": f"agent-e2e-{uuid.uuid4()}"},
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["task_id"]

        results = {
            "product.plan": {"report": prd_report()},
            "code.build": build_result(),
            "code.fix": build_result(),
            "qa.review": {"report": qa_report("pass")},
        }
        for expected_action in ("product.plan", "code.build", "qa.review"):
            assert [item["applied"] for item in controller.tick()["applied"]] == [
                "create_attempt"
            ]
            counts = deliver(gateway.pool, agent_client)
            assert counts == {"accepted": 1, "rejected": 0, "unknown": 0}, counts
            forwarded = worker.received[-1]
            assert forwarded["action"] == expected_action
            # The Agent injects the managed profile and the requested limits
            # survive the whole path.
            assert forwarded["parameters"]["_agent_context"]["profile"]
            assert forwarded["limits"]["timeout_seconds"] == 900
            with gateway.pool.connection() as db:
                worker_job_id = db.execute(
                    "SELECT worker_job_id FROM jobs WHERE id = %s",
                    (uuid.UUID(forwarded["gateway_job_id"]),),
                ).fetchone()["worker_job_id"]
            assert worker_job_id, "the Gateway did not record the execution id"
            report_from_worker(client, forwarded, results[expected_action], worker_job_id)

        # A passing verification of the newest change asks for the human decision.
        assert [item["applied"] for item in controller.tick()["applied"]] == [
            "request_review"
        ]
        detail = client.get(f"/v1/tasks/{task_id}", headers=BOT).json()
        assert detail["status"] == "WAITING_REVIEW"
        target = next(
            item["target_digest"]
            for item in detail["available_commands"]
            if item["type"] == "accept_deliverable"
        )
        accepted = client.post(
            f"/v1/tasks/{task_id}/commands",
            json={"type": "accept_deliverable", "target_digest": target},
            headers={**HUMAN, "Idempotency-Key": f"agent-e2e-accept-{uuid.uuid4()}"},
        )
        assert accepted.status_code == 202, accepted.text
        final = client.get(f"/v1/tasks/{task_id}", headers=BOT).json()
        assert final["status"] == "COMPLETED"
        # Three dispatches, each delivered exactly once.
        assert len(worker.received) == 3
        assert len({item["dispatch_id"] for item in worker.received}) == 3


@pytest.mark.skipif(agent_module is None, reason="the Agent application is not mounted")
def test_a_dispatch_the_agent_refuses_is_not_retried(client, monkeypatch):
    """The Agent's refusal is definitive, so the Gateway stops instead of waiting."""
    worker = WorkerDouble()
    monkeypatch.setattr(agent_module.urllib.request, "urlopen", worker)
    with TestClient(agent_module.app) as agent_client:
        project()
        created = client.post(
            "/v1/tasks",
            json={
                "project_id": "e2e-product",
                "title": "スキーマに合わない依頼",
                "objective": "受け付けられない指示を送る",
                "action": "product.plan",
                # `idea` is required by the Agent's schema for this action.
                "parameters": {"notes": "何も指定しない"},
                "workflow_id": "single-action-v1",
                "start": True,
            },
            headers={**BOT, "Idempotency-Key": f"agent-refuse-{uuid.uuid4()}"},
        )
        assert created.status_code == 201, created.text
        counts = deliver(gateway.pool, agent_client)
        assert counts == {"accepted": 0, "rejected": 1, "unknown": 0}, counts
        assert worker.received == [], "a refused dispatch must not reach the Worker"
        detail = client.get(f"/v1/tasks/{created.json()['task_id']}", headers=BOT).json()
        assert detail["status"] == "FAILED"
