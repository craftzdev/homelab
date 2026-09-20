"""One application lifespan and isolated database for the entire suite."""
import os

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict

TEST_DSN = os.environ["TEST_DATABASE_URL"]
if conninfo_to_dict(TEST_DSN).get("dbname") != "gateway_test":
    raise RuntimeError("tests require the dedicated gateway_test database")
os.environ.update(
    DATABASE_URL=TEST_DSN,
    GATEWAY_API_TOKEN="test-gateway-credential",
    WORKER_CALLBACK_TOKEN="test-callback-credential",
    HUMAN_APPROVAL_TOKEN="test-human-credential",
    HUMAN_APPROVAL_ACTOR="craftz",
    CLOUDFLARE_ACCESS_REQUIRED="false",
    CONTROLLER_API_TOKEN="test-controller-credential",
    COMFYUI_BASE_URL="",
    API_SURFACE="public",
)
from app import main as gateway


@pytest.fixture(scope="session")
def client():
    with TestClient(gateway.app) as client:
        yield client


@pytest.fixture(autouse=True)
def clean_database(client, monkeypatch):
    with gateway.pool.connection() as db:
        db.execute(
            "TRUNCATE worker_events, approvals, project_events, jobs, projects, tasks, "
            "workflow_runs, workflow_steps, step_attempts, artifacts, task_messages, "
            "platform_events, commands, decisions, input_requests, controllers, "
            "job_dispatches, worker_commands, workers, worker_overrides CASCADE"
        )
        db.execute("UPDATE event_counter SET last_cursor = 0")
