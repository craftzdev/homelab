from __future__ import annotations

import hashlib
import asyncio
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Literal

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, status
from jwt import PyJWKClient
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.requests import Request
from starlette.responses import JSONResponse, FileResponse

from app import config_releases, configuration, internal_api, scheduler, task_api, tasks, video

API_SURFACE = os.environ.get("API_SURFACE", "public")
DATABASE_URL = os.environ["DATABASE_URL"]
GATEWAY_API_TOKEN = os.environ["GATEWAY_API_TOKEN"]
WORKER_CALLBACK_TOKEN = os.environ["WORKER_CALLBACK_TOKEN"]
WORKER_BASE_URL = os.environ.get("WORKER_BASE_URL", "").rstrip("/")
WORKER_API_TOKEN = os.environ.get("WORKER_API_TOKEN", "")
HUMAN_APPROVAL_TOKEN = os.environ.get("HUMAN_APPROVAL_TOKEN", "")
HUMAN_APPROVAL_ACTOR = os.environ.get("HUMAN_APPROVAL_ACTOR", "").strip()
CLOUDFLARE_ACCESS_REQUIRED = os.environ.get(
    "CLOUDFLARE_ACCESS_REQUIRED", "false"
).lower() in {"1", "true", "yes"}
CLOUDFLARE_ACCESS_TEAM_DOMAIN = (
    os.environ.get("CLOUDFLARE_ACCESS_TEAM_DOMAIN", "")
    .removeprefix("https://")
    .rstrip("/")
)
CLOUDFLARE_ACCESS_AUD = os.environ.get("CLOUDFLARE_ACCESS_AUD", "")

# Principals are still one shared Gateway credential, so a request's recorded
# actor and source say which surface proved it, never what the client claimed.
# Separate credentials per human, bot and service arrive with the command API.
REST_PRINCIPAL = "credential:gateway-token"
MCP_PRINCIPAL = "credential:gateway-token#mcp"
CONTROLLER_PRINCIPAL = "credential:controller-token"
# The Workflow Controller advances work, which the public surface may not. It has
# its own credential and is expected to reach the Gateway over the internal
# network only; an unset token disables the internal API entirely.
CONTROLLER_API_TOKEN = os.environ.get("CONTROLLER_API_TOKEN", "")

pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=5,
    open=False,
    kwargs={"row_factory": dict_row},
)

mcp = MCPServer(
    name="ai-business-gateway",
    title="AI Business Gateway",
    version="0.1.0",
    instructions=(
        "Register business ideas, submit bounded Agent jobs, and inspect results. "
        "Production, deployment, publishing, and other irreversible actions are not "
        "available through this MCP server."
    ),
)
mcp_http_app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    json_response=True,
    stateless_http=True,
    max_request_body_size=262_144,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "gateway.craftz.dev",
            "gateway.craftz.dev:443",
            "127.0.0.1:*",
            "localhost:*",
            "testserver",
        ],
        allowed_origins=["https://gateway.craftz.dev"],
    ),
)


SCHEMA_SQL = """
SELECT pg_advisory_xact_lock(734859201);
CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    action TEXT NOT NULL,
    environment TEXT NOT NULL,
    state TEXT NOT NULL,
    input JSONB NOT NULL,
    worker_job_id TEXT,
    last_event_sequence BIGINT NOT NULL DEFAULT 0,
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS last_event_sequence BIGINT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS worker_events (
    event_id TEXT PRIMARY KEY,
    gateway_job_id UUID NOT NULL REFERENCES jobs(id),
    dispatch_id TEXT NOT NULL,
    worker_job_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    sequence BIGINT NOT NULL,
    payload JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    UNIQUE (gateway_job_id, sequence)
);

ALTER TABLE worker_events ADD COLUMN IF NOT EXISTS dispatch_id TEXT;
UPDATE worker_events
   SET dispatch_id = 'legacy:' || event_id
 WHERE dispatch_id IS NULL;
ALTER TABLE worker_events ALTER COLUMN dispatch_id SET NOT NULL;

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    idea TEXT NOT NULL,
    state TEXT NOT NULL,
    prd JSONB,
    repository_url TEXT,
    build_job_id UUID,
    qa_job_id UUID,
    production_url TEXT,
    analytics JSONB,
    growth_plan JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id UUID PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_pending_per_kind
    ON approvals(project_id, kind) WHERE state = 'PENDING';

CREATE TABLE IF NOT EXISTS project_events (
    id UUID PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE projects ADD COLUMN IF NOT EXISTS release_candidate JSONB;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS target JSONB;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS target_sha256 TEXT;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS approved_by TEXT;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ;

-- Legacy approvals cannot authorize a release without an immutable target.
UPDATE projects SET state = 'QA_REVIEW_REQUIRED', updated_at = NOW()
WHERE state IN ('AWAITING_APPROVAL', 'DEPLOY_APPROVED') AND id IN (
    SELECT project_id FROM approvals
    WHERE state IN ('PENDING', 'APPROVED') AND (target IS NULL OR expires_at IS NULL)
);
WITH invalidated AS (
    UPDATE approvals SET state = 'INVALIDATED', resolved_at = NOW()
    WHERE state IN ('PENDING', 'APPROVED') AND (target IS NULL OR expires_at IS NULL)
    RETURNING id, project_id
)
INSERT INTO project_events (id, project_id, event_type, payload, created_at)
SELECT gen_random_uuid(), project_id, 'approval.invalidated',
       jsonb_build_object('approval_id', id, 'reason', 'legacy_unbound_approval'), NOW()
FROM invalidated;
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    pool.open()
    pool.wait()
    with pool.connection() as connection:
        connection.execute(SCHEMA_SQL)
        tasks.ensure_schema(connection)
        connection.execute(configuration.SCHEMA_SQL)
        connection.execute(config_releases.SCHEMA_SQL)
        connection.commit()
    runner = None
    if API_SURFACE == "public" and video.configured():
        runner = video.VideoRunner(pool)
        runner.thread.start()
    dispatcher = None
    delivery = scheduler.Delivery(WORKER_BASE_URL, WORKER_API_TOKEN)
    if API_SURFACE == "public" and delivery.configured():
        # Also runnable as its own process; an advisory lock keeps one active.
        dispatcher = scheduler.Scheduler(pool, delivery)
        dispatcher.thread.start()
    try:
        async with mcp_http_app.router.lifespan_context(mcp_http_app):
            yield
    finally:
        if runner:
            runner.stop.set()
            await asyncio.to_thread(runner.thread.join)
        if dispatcher:
            dispatcher.stop.set()
            await asyncio.to_thread(dispatcher.thread.join)
        pool.close()


app = FastAPI(
    title="AI Business Gateway",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def authenticate_mcp(request: Request, call_next):
    """Apply the same two-layer authentication to every MCP request."""
    if request.url.path == "/mcp" or request.url.path.startswith("/mcp/"):
        try:
            require_surface("public")
            _verify_access_jwt(request.headers.get("Cf-Access-Jwt-Assertion"))
            _verify_bearer(request.headers.get("Authorization"), GATEWAY_API_TOKEN)
        except HTTPException as error:
            headers = (
                {"WWW-Authenticate": "Bearer"} if error.status_code == 401 else None
            )
            return JSONResponse(
                status_code=error.status_code,
                content={"detail": error.detail},
                headers=headers,
            )
    return await call_next(request)


def require_surface(expected: str) -> None:
    if API_SURFACE != expected:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")


def _verify_bearer(authorization: str | None, expected: str) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        )
    supplied = authorization.removeprefix("Bearer ")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        )


@lru_cache(maxsize=1)
def _access_jwk_client() -> PyJWKClient:
    return PyJWKClient(
        f"https://{CLOUDFLARE_ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs",
        cache_keys=True,
        lifespan=300,
    )


def _verify_access_jwt(assertion: str | None) -> None:
    if not CLOUDFLARE_ACCESS_REQUIRED:
        return
    if not CLOUDFLARE_ACCESS_TEAM_DOMAIN or not CLOUDFLARE_ACCESS_AUD:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cloudflare Access verification is not configured",
        )
    if not assertion:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        )

    try:
        signing_key = _access_jwk_client().get_signing_key_from_jwt(assertion)
        jwt.decode(
            assertion,
            signing_key.key,
            algorithms=["RS256"],
            audience=CLOUDFLARE_ACCESS_AUD,
            issuer=f"https://{CLOUDFLARE_ACCESS_TEAM_DOMAIN}",
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except jwt.PyJWTError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        ) from error


def require_gateway_token(
    authorization: str | None = Header(default=None),
    cf_access_jwt_assertion: str | None = Header(
        default=None, alias="Cf-Access-Jwt-Assertion"
    ),
) -> None:
    require_surface("public")
    _verify_access_jwt(cf_access_jwt_assertion)
    _verify_bearer(authorization, GATEWAY_API_TOKEN)


def require_callback_token(authorization: str | None = Header(default=None)) -> None:
    require_surface("callback")
    _verify_bearer(authorization, WORKER_CALLBACK_TOKEN)


def require_controller_token(authorization: str | None = Header(default=None)) -> None:
    # Served only by the internal surface, which is reachable on the Tailnet and
    # never through the public ingress that Grok and the browser use.
    require_surface("internal")
    if not CONTROLLER_API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the internal workflow API is not configured",
        )
    _verify_bearer(authorization, CONTROLLER_API_TOKEN)


def require_human_approval_token(
    human_approval_token: str | None = Header(
        default=None, alias="X-Human-Approval-Token"
    ),
) -> None:
    if not HUMAN_APPROVAL_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="human approval is not configured",
        )
    if not human_approval_token or not hmac.compare_digest(
        human_approval_token, HUMAN_APPROVAL_TOKEN
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        )


def require_approval_actor(_: None = Depends(require_human_approval_token)) -> str:
    """Identity is bound to the authenticated human token, never a caller header."""
    if not HUMAN_APPROVAL_ACTOR or len(HUMAN_APPROVAL_ACTOR) > 200:
        raise HTTPException(
            status_code=503, detail="human approval actor is not configured"
        )
    return HUMAN_APPROVAL_ACTOR


class ProjectCreate(BaseModel):
    project_id: str = Field(
        min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$"
    )
    title: str = Field(min_length=1, max_length=200)
    idea: str = Field(min_length=20, max_length=8000)


class ProjectRepositoryUpdate(BaseModel):
    repository_url: str = Field(
        min_length=10, max_length=2000, pattern=r"^https://github\.com/"
    )


class ReleaseTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commit_sha: str | None = Field(
        default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
    )
    image_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    environment: Literal["production"] = "production"

    @model_validator(mode="after")
    def immutable_reference_required(self):
        if self.commit_sha is None and self.image_digest is None:
            raise ValueError("a full commit SHA or immutable image digest is required")
        return self


class ReleaseCandidateCreate(ReleaseTarget):
    build_job_id: uuid.UUID
    qa_job_id: uuid.UUID


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accept_inconclusive_qa: bool = False


class ProjectProductionUpdate(ReleaseTarget):
    approval_id: uuid.UUID
    production_url: str = Field(min_length=10, max_length=2000, pattern=r"^https://")


class ProjectAnalyticsUpdate(BaseModel):
    analytics: dict[str, Any] = Field(min_length=1, max_length=50)


class JobCreate(BaseModel):
    action: Literal[
        "video.generate",
        "product.plan",
        "qa.review",
        "growth.plan",
        "browser.research",
        "analytics.read",
        "stripe.read",
        "code.build",
        "code.fix",
        "test.run",
        "git.pull_request.create",
        "deploy.preview",
        "deploy.production",
        "content.draft",
        "content.publish",
    ]
    project_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9._-]+$")
    environment: Literal["research", "preview", "production"]
    parameters: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def limits_must_be_enforced(self):
        unknown = sorted(set(self.limits) - set(tasks.EXECUTION_LIMITS))
        if unknown:
            raise ValueError(
                f"these limits are not enforced by any execution: {', '.join(unknown)}"
            )
        return self

    @model_validator(mode="after")
    def validate_video(self):
        if self.action == "video.generate":
            self.parameters = video.VideoParameters.model_validate(self.parameters).model_dump()
            self.limits = video.VideoLimits.model_validate(self.limits).model_dump()
        return self


MCPAction = Literal[
    "video.generate",
    "product.plan",
    "qa.review",
    "growth.plan",
    "browser.research",
    "analytics.read",
    "stripe.read",
    "code.build",
    "code.fix",
    "test.run",
]
MCPEnvironment = Literal["research", "preview"]


class WorkerEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    gateway_job_id: uuid.UUID
    dispatch_id: str = Field(min_length=1, max_length=100)
    worker_job_id: str = Field(min_length=1, max_length=100)
    event_type: Literal["accepted", "started", "progress", "completed", "failed"]
    sequence: int = Field(ge=1)
    occurred_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


# A job that has not reached one of these is still capable of changing evidence.
TERMINAL_JOB_STATES = ("SUCCEEDED", "FAILED_FINAL", "CANCELLED")

WORKER_ENDPOINTS = {
    "product.plan": "planning",
    "qa.review": "qa",
    "growth.plan": "growth",
    "browser.research": "browser",
    "analytics.read": "data",
    "stripe.read": "data",
    "code.build": "build",
    "code.fix": "build",
    "test.run": "test",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def qa_project_state(result: dict[str, Any] | None) -> str:
    """Map a completed QA report to the legacy business workflow state.

    A pass must at least carry the acceptance criteria it judged; the review's
    binding to the build it verified is checked separately when a release
    candidate is registered. The Task ledger applies the stricter per-criterion
    rule, and reports the difference rather than rewriting either verdict.
    """
    report = (result or {}).get("report")
    if not isinstance(report, dict):
        return "QA_REVIEW_REQUIRED"
    verdict = report.get("verdict")
    criteria = report.get("acceptance_criteria")
    if verdict == "pass" and isinstance(criteria, list) and criteria:
        return "QA_PASSED"
    if verdict == "fail":
        return "QA_FAILED"
    return "QA_REVIEW_REQUIRED"


def queue_dispatch(connection: Any, job_id: uuid.UUID, request: JobCreate) -> None:
    """Queue this Job's delivery in the transaction that registered it.

    Nothing is sent from here: the durable scheduler owns delivery, so the
    execution survives the end of this request and every retry reuses the same
    dispatch id.
    """
    if request.action == "video.generate":
        return  # The video runner owns these end to end.
    endpoint = WORKER_ENDPOINTS.get(request.action)
    if endpoint is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="action has no enabled worker executor",
        )
    dispatch_id = f"gateway:{job_id}:1"
    payload = {
        "gateway_job_id": str(job_id),
        "dispatch_id": dispatch_id,
        "action": request.action,
        "project_id": request.project_id,
        "parameters": request.parameters,
        "limits": request.limits,
    }
    tasks.enqueue_dispatch(
        connection,
        job_id=job_id,
        dispatch_id=dispatch_id,
        endpoint=endpoint,
        payload=payload,
        request_hash=hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest(),
    )
    # Bound the callback to this delivery before it can arrive.
    connection.execute(
        "UPDATE jobs SET dispatch_id = %s, updated_at = %s WHERE id = %s",
        (dispatch_id, utcnow(), job_id),
    )


def _public_job(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": str(row["id"]),
        "project_id": row["project_id"],
        "action": row["action"],
        "environment": row["environment"],
        "state": row["state"],
        "worker_job_id": row["worker_job_id"],
        "result": row["result"],
        # Every accepted job belongs to a Task, so one execution can be followed
        # from the board as well as from its job id.
        "task_id": str(row["task_id"]) if row.get("task_id") else None,
        "attempt_id": str(row["attempt_id"]) if row.get("attempt_id") else None,
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _validate_source_references(
    connection: Any, request: JobCreate
) -> None:
    """A referenced execution must belong to the project the job runs in.

    Knowing a worker job id is not access to it: QA must not be pointed at
    another project's workspace.
    """
    reference = request.parameters.get("source_worker_job_id")
    if not isinstance(reference, str) or not reference:
        return
    owner = connection.execute(
        "SELECT project_id FROM jobs WHERE worker_job_id = %s "
        "ORDER BY created_at DESC LIMIT 1",
        (reference,),
    ).fetchone()
    if owner is None:
        raise HTTPException(
            status_code=422, detail="source_worker_job_id is not a known execution"
        )
    if owner["project_id"] != request.project_id:
        raise HTTPException(
            status_code=403,
            detail="source_worker_job_id belongs to another project",
        )


def insert_job(
    connection: Any,
    request: JobCreate,
    idempotency_key: str,
    *,
    orchestration_mode: str = "legacy",
) -> tuple[dict[str, Any], bool]:
    """Insert one job inside the caller's transaction, without committing.

    Sharing this with Task creation keeps a Task, its Attempt and its Job in a
    single commit: an executed job can never exist without the ledger row that
    reports it.
    """
    if request.environment == "production" or request.action not in {*WORKER_ENDPOINTS, "video.generate"}:
        raise HTTPException(
            status_code=403, detail="production/irreversible execution is disabled"
        )
    now = utcnow()
    job_id = uuid.uuid7() if hasattr(uuid, "uuid7") else uuid.uuid4()
    payload = request.model_dump(mode="json")
    payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()

    # One lock order everywhere: project, then Task, then Run, then execution
    # capacity and the rows beneath them. A Controller proposal takes the same
    # order before it reaches this function (runs._locked_run_and_task).
    connection.execute(
        "SELECT id FROM projects WHERE id = %s FOR UPDATE",
        (request.project_id,),
    ).fetchone()
    _validate_source_references(connection, request)
    if request.action == "video.generate":
        if not video.configured():
            raise HTTPException(status_code=503, detail="video generation is not configured")
        # Serialize admission, but permit exact idempotent replays even at capacity.
        connection.execute("SELECT pg_advisory_xact_lock(734859203)")
        existing = connection.execute(
            "SELECT * FROM jobs WHERE idempotency_key=%s", (idempotency_key,)
        ).fetchone()
        if existing:
            if existing["input"]["sha256"] != payload_hash:
                raise ValueError("idempotency key already used with a different request")
            return existing, False
        count = connection.execute(
            "SELECT count(*) AS n FROM jobs WHERE action='video.generate' "
            "AND state IN ('QUEUED','VIDEO_SUBMITTING','RUNNING')"
        ).fetchone()["n"]
        if count >= 8:
            raise HTTPException(status_code=429, detail="video queue is full")
    row = connection.execute(
        """
        INSERT INTO jobs (
            id, idempotency_key, project_id, action, environment,
            state, input, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, 'QUEUED', %s, %s, %s)
        RETURNING *
        """,
        (
            job_id,
            idempotency_key,
            request.project_id,
            request.action,
            request.environment,
            json.dumps({"payload": payload, "sha256": payload_hash}),
            now,
            now,
        ),
    ).fetchone()
    # A directly submitted build or QA replaces the project's single build/QA
    # evidence, so a candidate bound to the old evidence must not survive it.
    # A Workflow Task keeps its evidence in its own Run, and only invalidates a
    # candidate when that candidate's own evidence changes.
    if orchestration_mode == "legacy" and request.action in {
        "code.build",
        "code.fix",
        "qa.review",
    }:
        _invalidate_approvals(connection, request.project_id, "new_build_or_qa")
    queue_dispatch(connection, row["id"], request)
    return row, True


def _persist_job(
    request: JobCreate,
    idempotency_key: str,
    *,
    actor: str = REST_PRINCIPAL,
    source: str = "api",
) -> tuple[dict[str, Any], bool]:
    """Persist a job once and return the existing row on an idempotent replay."""
    payload_hash = hashlib.sha256(
        json.dumps(request.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    with pool.connection() as connection:
        try:
            row, created = insert_job(connection, request, idempotency_key)
            if created:
                attached = tasks.attach_legacy_job(
                    connection, job=row, actor=actor, source=source
                )
                row = dict(row)
                row["task_id"] = attached["task"]["id"]
                row["attempt_id"] = attached["attempt_id"]
            connection.commit()
            return row, created
        except UniqueViolation:
            connection.rollback()
            row = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = %s", (idempotency_key,)
            ).fetchone()
            if row["input"]["sha256"] != payload_hash:
                raise ValueError(
                    "idempotency key already used with a different request"
                )
            return row, False


def _load_job(job_id: uuid.UUID) -> dict[str, Any] | None:
    with pool.connection() as connection:
        return connection.execute(
            "SELECT * FROM jobs WHERE id = %s", (job_id,)
        ).fetchone()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "surface": API_SURFACE}


@app.get("/ready")
def ready() -> dict[str, str]:
    with pool.connection() as connection:
        connection.execute("SELECT 1").fetchone()
    return {"status": "ready", "surface": API_SURFACE}


def _public_project(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": row["id"],
        "title": row["title"],
        "idea": row["idea"],
        "state": row["state"],
        "prd": row["prd"],
        "repository_url": row["repository_url"],
        "build_job_id": str(row["build_job_id"]) if row["build_job_id"] else None,
        "qa_job_id": str(row["qa_job_id"]) if row["qa_job_id"] else None,
        "production_url": row["production_url"],
        "analytics": row["analytics"],
        "growth_plan": row["growth_plan"],
        "release_candidate": row.get("release_candidate"),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _project_event(
    connection: Any,
    project_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    actor: str = "gateway",
) -> None:
    connection.execute(
        "INSERT INTO project_events (id, project_id, event_type, payload, created_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (uuid.uuid4(), project_id, event_type, json.dumps(payload), utcnow()),
    )
    # Same transaction, so a consumer rebuilding from snapshot plus feed learns
    # about project, candidate and approval changes as well as Tasks.
    tasks.record_project_event(
        connection,
        project_id=project_id,
        event_type=event_type,
        payload=payload,
        actor=actor,
    )


def _locked_project(connection: Any, project_id: str) -> dict[str, Any]:
    project = connection.execute(
        "SELECT * FROM projects WHERE id = %s FOR UPDATE", (project_id,)
    ).fetchone()
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _invalidate_approvals(connection: Any, project_id: str, reason: str) -> None:
    rows = connection.execute(
        "UPDATE approvals SET state = 'INVALIDATED', resolved_at = %s "
        "WHERE project_id = %s AND state IN ('PENDING', 'APPROVED') RETURNING id",
        (utcnow(), project_id),
    ).fetchall()
    for row in rows:
        _project_event(
            connection,
            project_id,
            "approval.invalidated",
            {"approval_id": str(row["id"]), "reason": reason},
        )
    connection.execute(
        "UPDATE projects SET release_candidate = NULL, updated_at = %s, "
        "state = CASE WHEN state IN ('AWAITING_APPROVAL', 'DEPLOY_APPROVED') "
        "THEN 'QA_REVIEW_REQUIRED' ELSE state END WHERE id = %s",
        (utcnow(), project_id),
    )


def _validate_candidate_jobs(
    connection: Any, project: dict[str, Any], target: dict[str, Any]
) -> str:
    if any(
        str(project[key]) != str(target[key]) for key in ("build_job_id", "qa_job_id")
    ):
        raise HTTPException(
            status_code=409, detail="candidate does not match current build/QA"
        )
    # Anything that has not reached a terminal state may still change the
    # evidence, including an execution whose delivery outcome is unknown.
    active = connection.execute(
        "SELECT id FROM jobs WHERE project_id = %s AND action IN ('code.build','code.fix','qa.review') "
        "AND state <> ALL(%s) LIMIT 1",
        (project["id"], list(TERMINAL_JOB_STATES)),
    ).fetchone()
    if active:
        raise HTTPException(status_code=409, detail="build or QA is still running")
    build = connection.execute(
        "SELECT * FROM jobs WHERE id = %s", (target["build_job_id"],)
    ).fetchone()
    qa = connection.execute(
        "SELECT * FROM jobs WHERE id = %s", (target["qa_job_id"],)
    ).fetchone()
    if (
        not build
        or not qa
        or build["project_id"] != project["id"]
        or qa["project_id"] != project["id"]
        or build["state"] != "SUCCEEDED"
        or qa["state"] != "SUCCEEDED"
        or build["action"] not in {"code.build", "code.fix"}
        or qa["action"] != "qa.review"
    ):
        raise HTTPException(
            status_code=409, detail="successful build and QA evidence are required"
        )
    source = (
        qa["input"].get("payload", {}).get("parameters", {}).get("source_worker_job_id")
    )
    if not source or source != build["worker_job_id"]:
        raise HTTPException(status_code=409, detail="QA did not review this build")
    qa_state = qa_project_state(qa["result"])
    if qa_state == "QA_FAILED":
        raise HTTPException(status_code=409, detail="QA failed")
    return qa_state


def _current_candidate(connection: Any, project: dict[str, Any]) -> dict[str, Any]:
    target = project.get("release_candidate")
    if not target:
        raise HTTPException(
            status_code=409, detail="register an immutable release candidate first"
        )
    if target["repository_url"] != project["repository_url"]:
        raise HTTPException(status_code=409, detail="candidate repository changed")
    if _validate_candidate_jobs(connection, project, target) != target["qa_state"]:
        raise HTTPException(status_code=409, detail="candidate QA evidence changed")
    return target


def _target_hash(target: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(target, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _public_approval(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, (uuid.UUID, datetime)):
            result[key] = (
                value.isoformat() if isinstance(value, datetime) else str(value)
            )
    result["effective_state"] = (
        "EXPIRED"
        if row["state"] in {"PENDING", "APPROVED"}
        and row.get("expires_at")
        and row["expires_at"] <= utcnow()
        else row["state"]
    )
    return result


def _locked_approval(
    connection: Any, approval_id: uuid.UUID
) -> tuple[dict[str, Any], dict[str, Any]]:
    locator = connection.execute(
        "SELECT project_id FROM approvals WHERE id = %s", (approval_id,)
    ).fetchone()
    if locator is None:
        raise HTTPException(status_code=404, detail="approval not found")
    project = _locked_project(connection, locator["project_id"])
    approval = connection.execute(
        "SELECT * FROM approvals WHERE id = %s FOR UPDATE", (approval_id,)
    ).fetchone()
    return project, approval


def _check_approval(
    connection: Any, project: dict[str, Any], approval: dict[str, Any], state: str
) -> None:
    if approval["kind"] != "production.deploy":
        raise HTTPException(
            status_code=409, detail="approval does not authorize production deployment"
        )
    if approval["state"] != state:
        raise HTTPException(status_code=409, detail=f"approval is not {state.lower()}")
    if not approval.get("expires_at") or approval["expires_at"] <= utcnow():
        raise HTTPException(
            status_code=409, detail="approval expired; request a new approval"
        )
    target = _current_candidate(connection, project)
    if approval["target"] != target or approval["target_sha256"] != _target_hash(
        target
    ):
        raise HTTPException(status_code=409, detail="approval target changed")


@app.put("/v1/projects/{project_id}/release-candidate")
def register_release_candidate(
    project_id: str,
    request: ReleaseCandidateCreate,
    _: None = Depends(require_gateway_token),
    actor: str = Depends(require_approval_actor),
) -> dict[str, Any]:
    with pool.connection() as connection:
        project = _locked_project(connection, project_id)
        if not project["repository_url"]:
            raise HTTPException(
                status_code=409, detail="project repository is required"
            )
        target = request.model_dump(mode="json")
        qa_state = _validate_candidate_jobs(connection, project, target)
        target.update(
            candidate_id=str(uuid.uuid4()),
            repository_url=project["repository_url"],
            qa_state=qa_state,
            binding_source="human_attested",
        )
        _invalidate_approvals(connection, project_id, "release_candidate_registered")
        connection.execute(
            "UPDATE projects SET release_candidate = %s, state = %s, updated_at = %s WHERE id = %s",
            (json.dumps(target), qa_state, utcnow(), project_id),
        )
        _project_event(
            connection,
            project_id,
            "release_candidate.registered",
            {"actor": actor, "target": target, "target_sha256": _target_hash(target)},
        )
        connection.commit()
    return {
        "project_id": project_id,
        "target": target,
        "target_sha256": _target_hash(target),
    }


@app.get("/v1/projects/{project_id}/approvals")
def list_project_approvals(
    project_id: str, _: None = Depends(require_gateway_token)
) -> dict[str, Any]:
    with pool.connection() as connection:
        rows = connection.execute(
            "SELECT * FROM approvals WHERE project_id = %s ORDER BY requested_at DESC",
            (project_id,),
        ).fetchall()
    return {"approvals": [_public_approval(row) for row in rows]}


@app.get("/v1/approvals/{approval_id}")
def get_approval(
    approval_id: uuid.UUID, _: None = Depends(require_gateway_token)
) -> dict[str, Any]:
    with pool.connection() as connection:
        row = connection.execute(
            "SELECT * FROM approvals WHERE id = %s", (approval_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return _public_approval(row)


@app.post("/v1/projects", status_code=status.HTTP_201_CREATED)
def create_project(
    request: ProjectCreate, _: None = Depends(require_gateway_token)
) -> dict[str, Any]:
    now = utcnow()
    with pool.connection() as connection:
        try:
            row = connection.execute(
                """
                INSERT INTO projects (id, title, idea, state, created_at, updated_at)
                VALUES (%s, %s, %s, 'IDEA_SUBMITTED', %s, %s)
                RETURNING *
                """,
                (request.project_id, request.title, request.idea, now, now),
            ).fetchone()
            _project_event(
                connection, request.project_id, "idea.submitted", request.model_dump()
            )
            connection.commit()
        except UniqueViolation as error:
            connection.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="project already exists"
            ) from error
    return _public_project(row)


@app.get("/v1/projects/{project_id}")
def get_project(
    project_id: str, _: None = Depends(require_gateway_token)
) -> dict[str, Any]:
    with pool.connection() as connection:
        row = connection.execute(
            "SELECT * FROM projects WHERE id = %s", (project_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
            )
        # Aggregate the Project's Tasks rather than letting the newest callback
        # decide what the whole Project is doing.
        summary = tasks.project_summary(connection, project_id)
    return {**_public_project(row), "tasks": summary}


@app.put("/v1/projects/{project_id}/repository")
def register_project_repository(
    project_id: str,
    request: ProjectRepositoryUpdate,
    _: None = Depends(require_gateway_token),
    __: None = Depends(require_human_approval_token),
) -> dict[str, Any]:
    with pool.connection() as connection:
        row = connection.execute(
            """
            UPDATE projects
               SET repository_url = %s, state = 'REPOSITORY_READY', updated_at = %s
             WHERE id = %s AND state IN ('PRD_READY', 'REPOSITORY_READY')
             RETURNING *
            """,
            (request.repository_url, utcnow(), project_id),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="project must have an approved PRD before repository registration",
            )
        _project_event(
            connection, project_id, "repository.registered", request.model_dump()
        )
        connection.commit()
    return _public_project(row)


@app.post("/v1/projects/{project_id}/approval", status_code=status.HTTP_201_CREATED)
def request_production_approval(
    project_id: str,
    request: ApprovalRequest | None = None,
    _: None = Depends(require_gateway_token),
) -> dict[str, Any]:
    request = request or ApprovalRequest()
    now = utcnow()
    approval_id = uuid.uuid4()
    with pool.connection() as connection:
        project = _locked_project(connection, project_id)
        target = _current_candidate(connection, project)
        expired = connection.execute(
            "UPDATE approvals SET state = 'EXPIRED' WHERE project_id = %s "
            "AND state IN ('PENDING','APPROVED') AND expires_at <= %s RETURNING id",
            (project_id, now),
        ).fetchall()
        for row in expired:
            _project_event(
                connection,
                project_id,
                "approval.expired",
                {"approval_id": str(row["id"])},
            )
        if expired and project["state"] in {"AWAITING_APPROVAL", "DEPLOY_APPROVED"}:
            project["state"] = target["qa_state"]
        if project["state"] not in {"QA_PASSED", "QA_REVIEW_REQUIRED"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "project must pass QA or require an explicit human QA review "
                    "before production approval"
                ),
            )
        approval = connection.execute(
            "INSERT INTO approvals (id, project_id, kind, state, requested_at, target, target_sha256, expires_at) "
            "VALUES (%s, %s, 'production.deploy', 'PENDING', %s, %s, %s, %s) RETURNING *",
            (
                approval_id,
                project_id,
                now,
                json.dumps(target),
                _target_hash(target),
                now + timedelta(seconds=request.ttl_seconds),
            ),
        ).fetchone()
        connection.execute(
            "UPDATE projects SET state = 'AWAITING_APPROVAL', updated_at = %s WHERE id = %s",
            (now, project_id),
        )
        _project_event(
            connection,
            project_id,
            "approval.requested",
            {
                "approval_id": str(approval_id),
                "target": target,
                "target_sha256": approval["target_sha256"],
                "expires_at": approval["expires_at"].isoformat(),
            },
        )
        connection.commit()
    return {"approval_id": str(approval_id), **_public_approval(approval)}


@app.post("/v1/approvals/{approval_id}/approve")
def approve_production(
    approval_id: uuid.UUID,
    request: ApprovalDecision,
    _: None = Depends(require_gateway_token),
    actor: str = Depends(require_approval_actor),
) -> dict[str, Any]:
    now = utcnow()
    with pool.connection() as connection:
        project, approval = _locked_approval(connection, approval_id)
        _check_approval(connection, project, approval, "PENDING")
        if (
            project["state"] != "AWAITING_APPROVAL"
            or request.target_sha256 != approval["target_sha256"]
        ):
            raise HTTPException(
                status_code=409,
                detail="reviewed target does not match pending approval",
            )
        if (
            approval["target"]["qa_state"] == "QA_REVIEW_REQUIRED"
            and not request.accept_inconclusive_qa
        ):
            raise HTTPException(
                status_code=409,
                detail="explicit acknowledgement of inconclusive QA required",
            )
        approval = connection.execute(
            "UPDATE approvals SET state = 'APPROVED', resolved_at = %s, approved_by = %s "
            "WHERE id = %s RETURNING *",
            (now, actor, approval_id),
        ).fetchone()
        connection.execute(
            "UPDATE projects SET state = 'DEPLOY_APPROVED', updated_at = %s WHERE id = %s",
            (now, approval["project_id"]),
        )
        _project_event(
            connection,
            approval["project_id"],
            "approval.approved",
            {
                "approval_id": str(approval_id),
                "approved_by": actor,
                "target": approval["target"],
                "target_sha256": approval["target_sha256"],
                "expires_at": approval["expires_at"].isoformat(),
                "accept_inconclusive_qa": request.accept_inconclusive_qa,
            },
        )
        connection.commit()
    return {"approval_id": str(approval_id), **_public_approval(approval)}


@app.post("/v1/approvals/{approval_id}/cancel")
def cancel_production_approval(
    approval_id: uuid.UUID,
    _: None = Depends(require_gateway_token),
    actor: str = Depends(require_approval_actor),
) -> dict[str, Any]:
    """Withdraw pending or unused granted authority without losing the QA decision."""
    now = utcnow()
    with pool.connection() as connection:
        _locked_approval(connection, approval_id)
        approval = connection.execute(
            "UPDATE approvals SET state = 'CANCELLED', resolved_at = %s "
            "WHERE id = %s AND state IN ('PENDING', 'APPROVED') RETURNING project_id",
            (now, approval_id),
        ).fetchone()
        if approval is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="approval is not pending"
            )
        project = connection.execute(
            "SELECT qa_job_id FROM projects WHERE id = %s FOR UPDATE",
            (approval["project_id"],),
        ).fetchone()
        qa_state = "QA_REVIEW_REQUIRED"
        if project is not None and project["qa_job_id"] is not None:
            qa_job = connection.execute(
                "SELECT result FROM jobs WHERE id = %s", (project["qa_job_id"],)
            ).fetchone()
            if qa_job is not None and isinstance(qa_job["result"], dict):
                qa_state = qa_project_state(qa_job["result"])
        connection.execute(
            "UPDATE projects SET state = %s, updated_at = %s WHERE id = %s",
            (qa_state, now, approval["project_id"]),
        )
        _project_event(
            connection,
            approval["project_id"],
            "approval.cancelled",
            {
                "approval_id": str(approval_id),
                "restored_state": qa_state,
                "actor": actor,
            },
        )
        connection.commit()
    return {
        "approval_id": str(approval_id),
        "project_id": approval["project_id"],
        "state": "CANCELLED",
        "project_state": qa_state,
    }


@app.put("/v1/projects/{project_id}/production")
def record_production_release(
    project_id: str,
    request: ProjectProductionUpdate,
    _: None = Depends(require_gateway_token),
    actor: str = Depends(require_approval_actor),
) -> dict[str, Any]:
    with pool.connection() as connection:
        project, approval = _locked_approval(connection, request.approval_id)
        if project["id"] != project_id:
            raise HTTPException(
                status_code=409, detail="approval belongs to another project"
            )
        _check_approval(connection, project, approval, "APPROVED")
        if not approval["approved_by"]:
            raise HTTPException(
                status_code=409, detail="approval has no authenticated actor"
            )
        for key in ("commit_sha", "image_digest", "environment"):
            if getattr(request, key) != approval["target"][key]:
                raise HTTPException(
                    status_code=409, detail="release does not match approved target"
                )
        row = connection.execute(
            "UPDATE projects SET production_url = %s, state = 'LIVE', updated_at = %s "
            "WHERE id = %s AND state = 'DEPLOY_APPROVED' RETURNING *",
            (request.production_url, utcnow(), project_id),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="production deployment has not been approved",
            )
        connection.execute(
            "UPDATE approvals SET state = 'CONSUMED', consumed_at = %s WHERE id = %s",
            (utcnow(), request.approval_id),
        )
        _project_event(
            connection,
            project_id,
            "production.released",
            {
                **request.model_dump(mode="json"),
                "actor": actor,
                "approved_by": approval["approved_by"],
                "target_sha256": approval["target_sha256"],
            },
        )
        connection.commit()
    return _public_project(row)


@app.put("/v1/projects/{project_id}/analytics")
def record_project_analytics(
    project_id: str,
    request: ProjectAnalyticsUpdate,
    _: None = Depends(require_gateway_token),
) -> dict[str, Any]:
    preview_validation = request.analytics.get("scope") == "preview_validation"
    with pool.connection() as connection:
        if preview_validation:
            row = connection.execute(
                "UPDATE projects SET analytics = %s, state = 'VALIDATION_MEASURING', "
                "updated_at = %s WHERE id = %s AND production_url IS NULL "
                "AND state IN ('QA_PASSED', 'QA_REVIEW_REQUIRED', "
                "'VALIDATION_MEASURING') RETURNING *",
                (json.dumps(request.analytics), utcnow(), project_id),
            ).fetchone()
        else:
            row = connection.execute(
                "UPDATE projects SET analytics = %s, state = 'MEASURING', updated_at = %s "
                "WHERE id = %s AND production_url IS NOT NULL RETURNING *",
                (json.dumps(request.analytics), utcnow(), project_id),
            ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "preview analytics require a completed QA review and "
                    "scope=preview_validation; other analytics require a recorded "
                    "production release"
                ),
            )
        event_type = (
            "analytics.validation_recorded"
            if preview_validation
            else "analytics.recorded"
        )
        _project_event(connection, project_id, event_type, request.model_dump())
        connection.commit()
    return _public_project(row)


@app.post("/v1/jobs", status_code=status.HTTP_202_ACCEPTED)
def create_job(
    request: JobCreate,
    _: None = Depends(require_gateway_token),
    idempotency_key: str = Header(min_length=8, max_length=200),
) -> dict[str, Any]:
    try:
        row, _created = _persist_job(request, idempotency_key)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error
    # Accepted means registered and queued for delivery, not yet dispatched.
    return _public_job(row)


@app.get("/v1/jobs/{job_id}")
def get_job(
    job_id: uuid.UUID, _: None = Depends(require_gateway_token)
) -> dict[str, Any]:
    row = _load_job(job_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="job not found"
        )
    return _public_job(row)


@app.get("/v1/jobs/{job_id}/video")
def get_video(job_id: uuid.UUID, _: None = Depends(require_gateway_token)):
    row = _load_job(job_id)
    if row is None or row["action"] != "video.generate" or row["state"] != "SUCCEEDED":
        raise HTTPException(status_code=404, detail="video not available")
    artifact = video.artifact_path(job_id)
    if artifact.is_symlink() or not artifact.is_file():
        raise HTTPException(status_code=410, detail="video expired or unavailable")
    return FileResponse(artifact, media_type="video/mp4", filename=f"{job_id}.mp4",
                        content_disposition_type="inline",
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@app.post("/v1/worker-events", status_code=status.HTTP_202_ACCEPTED)
def receive_worker_event(
    event: WorkerEvent, _: None = Depends(require_callback_token)
) -> dict[str, Any]:
    callback_job = _load_job(event.gateway_job_id)
    if callback_job and callback_job["action"] == "video.generate":
        raise HTTPException(status_code=409, detail="video jobs do not accept worker callbacks")
    state_map = {
        "accepted": "ACCEPTED",
        "started": "RUNNING",
        "progress": "RUNNING",
        "completed": "SUCCEEDED",
        # A stop the Gateway asked for is reported as cancelled, not as a failure.
        "failed": "CANCELLED" if event.data.get("cancelled") else "FAILED_FINAL",
    }
    now = utcnow()

    with pool.connection() as connection:
        existing_event = connection.execute(
            "SELECT gateway_job_id, sequence FROM worker_events WHERE event_id = %s",
            (event.event_id,),
        ).fetchone()
        if existing_event is not None:
            if (
                existing_event["gateway_job_id"] == event.gateway_job_id
                and existing_event["sequence"] == event.sequence
            ):
                return {"accepted": True, "event_id": event.event_id, "duplicate": True}
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="event id already used with different event metadata",
            )

        # One lock order everywhere: project, then Task, and only then the job
        # rows, Attempts and Run beneath it — a Controller proposal takes the same
        # two first (runs._locked_run_and_task). The exception is a Run's lease,
        # which is claimed and renewed on the Run row alone and takes nothing else,
        # so it cannot be one side of a cycle.
        if callback_job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="job not found"
            )
        connection.execute(
            "SELECT id FROM projects WHERE id = (SELECT project_id FROM jobs WHERE id = %s) FOR UPDATE",
            (event.gateway_job_id,),
        ).fetchone()
        if callback_job.get("task_id"):
            connection.execute(
                "SELECT id FROM tasks WHERE id = %s FOR UPDATE",
                (callback_job["task_id"],),
            ).fetchone()
        job = connection.execute(
            "SELECT * FROM jobs WHERE id = %s FOR UPDATE",
            (event.gateway_job_id,),
        ).fetchone()
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="job not found"
            )

        # Provenance, not just ordering: an event must come from the delivery and
        # the Worker execution this job was actually handed to.
        mismatch = _callback_binding_error(job, event)
        if mismatch is not None:
            # Record the quarantine on this connection, then roll the business
            # transaction back by raising. Opening a second pooled connection
            # while holding this one can exhaust the pool under load.
            _quarantine_callback(connection, job, event, mismatch)
            connection.commit()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=mismatch)

        if event.sequence <= job["last_event_sequence"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="event sequence must advance monotonically",
            )
        if job["state"] in TERMINAL_JOB_STATES:
            raise HTTPException(
                status_code=409, detail="terminal job evidence is immutable"
            )

        try:
            connection.execute(
                """
                INSERT INTO worker_events (
                    event_id, gateway_job_id, dispatch_id, worker_job_id,
                    event_type, sequence, payload, occurred_at, received_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    event.event_id,
                    event.gateway_job_id,
                    event.dispatch_id,
                    event.worker_job_id,
                    event.event_type,
                    event.sequence,
                    json.dumps(event.data),
                    event.occurred_at,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE jobs
                   SET state = %s,
                       worker_job_id = %s,
                       last_event_sequence = %s,
                       result = CASE WHEN %s IN ('completed', 'failed') THEN %s ELSE result END,
                       updated_at = %s
                 WHERE id = %s
                """,
                (
                    state_map[event.event_type],
                    event.worker_job_id,
                    event.sequence,
                    event.event_type,
                    json.dumps(event.data),
                    now,
                    event.gateway_job_id,
                ),
            )
            if (
                event.event_type not in {"completed", "failed"}
                and job.get("task_id")
                and event.worker_job_id
            ):
                # A stop can be asked for before anything is known to stop. This
                # event is the other way the execution's id becomes known — the
                # dispatch response is not the only one — so the requested stop is
                # queued here too, rather than waiting for a response that may
                # never arrive. The Task row is already locked above.
                stopping = connection.execute(
                    "SELECT control_state FROM tasks WHERE id = %s",
                    (job["task_id"],),
                ).fetchone()
                if stopping and stopping["control_state"] == "CANCEL_REQUESTED":
                    tasks.enqueue_worker_command(
                        connection,
                        job_id=job["id"],
                        worker_job_id=event.worker_job_id,
                        kind="cancel",
                        actor="gateway",
                    )
            # A Workflow Task keeps its evidence in its own Run and artifacts.
            # Only directly submitted jobs still write the Project's single
            # build/QA/PRD fields, so sibling Tasks cannot overwrite each other.
            legacy_projection = _is_legacy_job(connection, job)
            if event.event_type == "completed" and legacy_projection:
                if job["action"] in {"code.build", "code.fix", "qa.review"}:
                    _invalidate_approvals(
                        connection, job["project_id"], "build_or_qa_evidence_changed"
                    )
                transition = {
                    "product.plan": ("PRD_READY", "prd", event.data.get("report")),
                    "code.build": (
                        "PREVIEW_READY",
                        "build_job_id",
                        event.gateway_job_id,
                    ),
                    "code.fix": ("PREVIEW_READY", "build_job_id", event.gateway_job_id),
                    "qa.review": (
                        qa_project_state(event.data),
                        "qa_job_id",
                        event.gateway_job_id,
                    ),
                    "growth.plan": (
                        "GROWTH_REVIEW_READY",
                        "growth_plan",
                        event.data.get("report"),
                    ),
                }.get(job["action"])
                if transition is not None:
                    next_state, column, value = transition
                    if column in {"prd", "growth_plan"}:
                        value = json.dumps(value or {})
                    connection.execute(
                        f"UPDATE projects SET state = %s, {column} = %s, updated_at = %s "
                        "WHERE id = %s",
                        (next_state, value, now, job["project_id"]),
                    )
                    _project_event(
                        connection,
                        job["project_id"],
                        f"{job['action']}.completed",
                        {
                            "gateway_job_id": str(event.gateway_job_id),
                            # The verdict the report states. The Task event
                            # carries the verdict its evidence supports.
                            **(
                                {
                                    "reported_verdict": (
                                        event.data.get("report", {}) or {}
                                    ).get("verdict")
                                }
                                if job["action"] == "qa.review"
                                and isinstance(event.data.get("report"), dict)
                                else {}
                            ),
                        },
                    )
            elif event.event_type == "failed" and legacy_projection:
                next_state = "QA_FAILED" if job["action"] == "qa.review" else "FAILED"
                connection.execute(
                    "UPDATE projects SET state = %s, updated_at = %s WHERE id = %s",
                    (next_state, now, job["project_id"]),
                )
            # Project the same evidence onto this job's own Attempt. Keyed on the
            # Attempt, so a sibling Task in the same Project keeps its own state.
            ledger_job = connection.execute(
                "SELECT * FROM jobs WHERE id = %s", (event.gateway_job_id,)
            ).fetchone()
            tasks.project_worker_event(
                connection,
                job=ledger_job,  # noqa: E501 - same row, now carrying the worker id
                event_type=event.event_type,
                data=event.data,
                occurred_at=event.occurred_at,
            )
            connection.commit()
        except UniqueViolation:
            connection.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="event conflicts with an existing sequence",
            )

    return {"accepted": True, "event_id": event.event_id, "duplicate": False}


def _callback_binding_error(job: dict[str, Any], event: WorkerEvent) -> str | None:
    """Reject an event that does not match the recorded delivery."""
    recorded_dispatch = job.get("dispatch_id")
    if recorded_dispatch and event.dispatch_id != recorded_dispatch:
        return "event does not belong to this job's dispatch"
    recorded_worker_job = job.get("worker_job_id")
    if recorded_worker_job and event.worker_job_id != recorded_worker_job:
        return "event does not belong to this job's worker execution"
    return None


def _quarantine_callback(
    connection: Any, job: dict[str, Any], event: WorkerEvent, reason: str
) -> None:
    """Keep the rejected callback as audit evidence, changing no business state."""
    tasks.record_event(
        connection,
        aggregate_type="job",
        aggregate_id=str(job["id"]),
        type="job.callback_quarantined",
        actor="worker",
        payload={
            "reason": reason,
            "event_id": event.event_id,
            "claimed_dispatch_id": event.dispatch_id,
            "claimed_worker_job_id": event.worker_job_id,
            "recorded_dispatch_id": job.get("dispatch_id"),
            "recorded_worker_job_id": job.get("worker_job_id"),
        },
        causation_id=str(job["id"]),
    )


def _is_legacy_job(connection: Any, job: dict[str, Any]) -> bool:
    """Whether this job's progress still drives the old Project fields."""
    if not job.get("task_id"):
        return True
    row = connection.execute(
        "SELECT orchestration_mode FROM tasks WHERE id = %s", (job["task_id"],)
    ).fetchone()
    return row is None or row["orchestration_mode"] == "legacy"


def _parse_job_id(job_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(job_id)
    except ValueError as error:
        raise ValueError("job_id must be a valid UUID") from error


@mcp.tool(
    name="submit_job",
    description=(
        "Submit one bounded research, analysis, build, fix, test, or video job. "
        "For video.generate use parameters {prompt: string, seed?: integer}; "
        "fixed fasth3-5s-v1 workflow, 896x512, 124 frames at 24 fps. "
        "Poll get_job for result.artifact (authenticated download) and result.review (Tailnet). "
        "Only research and preview environments are accepted. Reuse the same "
        "idempotency_key when retrying the same request."
    ),
    structured_output=True,
)
def mcp_submit_job(
    action: MCPAction,
    project_id: str,
    environment: MCPEnvironment,
    parameters: dict[str, Any] | None = None,
    limits: dict[str, int] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    key = idempotency_key or f"grok-{uuid.uuid4()}"
    if not 8 <= len(key) <= 200:
        raise ValueError("idempotency_key must contain between 8 and 200 characters")

    request = JobCreate(
        action=action,
        project_id=project_id,
        environment=environment,
        parameters=parameters or {},
        limits=limits or {},
    )
    row, created = _persist_job(
        request, key, actor=MCP_PRINCIPAL, source="grok"
    )
    response = _public_job(_load_job(row["id"]) or row)
    response["idempotency_key"] = key
    response["idempotent_replay"] = not created
    return response


@mcp.tool(
    name="get_job",
    description="Get the current state and result of one Gateway job.",
    structured_output=True,
)
def mcp_get_job(job_id: str) -> dict[str, Any]:
    row = _load_job(_parse_job_id(job_id))
    if row is None:
        raise ValueError("job not found")
    return _public_job(row)


@mcp.tool(
    name="wait_for_job",
    description=(
        "Wait briefly for a Gateway job to reach a terminal state, then return "
        "its current state and result. The maximum timeout is 120 seconds."
    ),
    structured_output=True,
)
def mcp_wait_for_job(
    job_id: str, timeout_seconds: int = 60, poll_interval_seconds: int = 2
) -> dict[str, Any]:
    if not 1 <= timeout_seconds <= 120:
        raise ValueError("timeout_seconds must be between 1 and 120")
    if not 1 <= poll_interval_seconds <= 10:
        raise ValueError("poll_interval_seconds must be between 1 and 10")

    parsed_job_id = _parse_job_id(job_id)
    deadline = time.monotonic() + timeout_seconds
    terminal_states = {"SUCCEEDED", "FAILED_FINAL", "CANCELLED", "NEEDS_REVIEW"}

    while True:
        row = _load_job(parsed_job_id)
        if row is None:
            raise ValueError("job not found")
        response = _public_job(row)
        if response["state"] in terminal_states or time.monotonic() >= deadline:
            return response
        time.sleep(min(poll_interval_seconds, max(0, deadline - time.monotonic())))


@mcp.tool(
    name="get_review_url",
    description=(
        "Return the Tailnet-only review URL from a completed job when one "
        "is available. The URL is intended for the human operator."
    ),
    structured_output=True,
)
def mcp_get_review_url(job_id: str) -> dict[str, Any]:
    row = _load_job(_parse_job_id(job_id))
    if row is None:
        raise ValueError("job not found")
    result = row.get("result") or {}
    review = result.get("review") if isinstance(result, dict) else None
    return {
        "job_id": str(row["id"]),
        "state": row["state"],
        "available": isinstance(review, dict) and bool(review.get("url")),
        "review": review if isinstance(review, dict) else None,
    }


@mcp.tool(
    name="submit_business_idea",
    description=(
        "Register one human-supplied business idea as a durable project. "
        "This does not approve or deploy anything."
    ),
    structured_output=True,
)
def mcp_submit_business_idea(project_id: str, title: str, idea: str) -> dict[str, Any]:
    request = ProjectCreate(project_id=project_id, title=title, idea=idea)
    return create_project(request, None)


@mcp.tool(
    name="get_project",
    description="Get the durable business lifecycle state for one project.",
    structured_output=True,
)
def mcp_get_project(project_id: str) -> dict[str, Any]:
    return get_project(project_id, None)


@mcp.tool(
    name="request_production_approval",
    description=(
        "Request expiring human approval for the registered immutable release candidate after QA. "
        "This tool cannot grant the approval."
    ),
    structured_output=True,
)
def mcp_request_production_approval(
    project_id: str, ttl_seconds: int = 3600
) -> dict[str, Any]:
    return request_production_approval(
        project_id, ApprovalRequest(ttl_seconds=ttl_seconds), None
    )


@mcp.tool(
    name="get_production_approval",
    description="Read an approval's immutable target, authenticated approver and effective expiry state. Cannot grant approval.",
    structured_output=True,
)
def mcp_get_production_approval(approval_id: str) -> dict[str, Any]:
    return get_approval(uuid.UUID(approval_id), None)


def _build_job_request(
    *,
    action: str,
    project_id: str,
    environment: str,
    parameters: dict[str, Any],
    limits: dict[str, int],
) -> JobCreate:
    return JobCreate(
        action=action,
        project_id=project_id,
        environment=environment,
        parameters=parameters,
        limits=limits,
    )


def _create_attempt_job(
    *,
    connection: Any,
    action: str,
    project_id: str,
    environment: str,
    parameters: dict[str, Any],
    limits: dict[str, int],
    idempotency_key: str,
) -> dict[str, Any]:
    """Insert the Job for one Workflow Attempt inside the caller's transaction."""
    request = JobCreate(
        action=action,
        project_id=project_id,
        environment=environment,
        parameters=parameters,
        limits=limits,
    )
    row, created = insert_job(
        connection, request, idempotency_key, orchestration_mode="workflow-v1"
    )
    if not created:
        # The same step and cycle was already dispatched; the ledger's unique
        # step/cycle constraint means this is a duplicate proposal.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this attempt was already created",
        )
    return row


TASK_JOB_BRIDGE = task_api.JobBridge(
    build_request=_build_job_request,
    insert=insert_job,
    actions=tuple(sorted({*WORKER_ENDPOINTS, "video.generate"})),
)

def _human_principal(token: str | None) -> str:
    """Verify the separate human credential and return that person's identity."""
    require_human_approval_token(token)
    if not HUMAN_APPROVAL_ACTOR or len(HUMAN_APPROVAL_ACTOR) > 200:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="human approval actor is not configured",
        )
    return f"human:{HUMAN_APPROVAL_ACTOR}"


def _config_principal(token: str | None) -> str:
    expected = os.environ.get("CONFIG_ADMIN_TOKEN", "")
    actor = os.environ.get("CONFIG_ADMIN_ACTOR", "").strip()
    if len(expected) < 32 or not actor or len(actor) > 200:
        raise HTTPException(status_code=503, detail="configuration editing is not configured")
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="configuration editor credential required")
    return f"config-editor:{actor}"


app.include_router(configuration.build_router(pool=pool, auth=require_gateway_token, config_principal=_config_principal))
app.include_router(config_releases.build_router(pool=pool, auth=require_gateway_token, config_principal=_config_principal))


@app.get("/v1/config/contract", dependencies=[Depends(require_gateway_token)])
def configuration_contract():
    return {"api_version": "1.0", "schema_url": "/v1/config/openapi.json",
            "capabilities": ["drafts", "immutable_releases", "ci_evidence", "human_promotion", "worker_intake"],
            "compatibility": "Additive fields and new states may be introduced in v1. Clients must preserve unknown states and use available_commands."}


@app.get("/v1/config/openapi.json", dependencies=[Depends(require_gateway_token)])
def configuration_openapi():
    from fastapi.openapi.utils import get_openapi
    schema = get_openapi(title="AI Gateway Management API", version="1.0", routes=app.routes)
    schema["paths"] = {path: value for path, value in schema["paths"].items()
                       if path.startswith("/v1/config/")}
    return schema

app.include_router(
    task_api.build_router(
        pool=pool,
        auth=require_gateway_token,
        jobs=TASK_JOB_BRIDGE,
        actor_resolver=lambda: (REST_PRINCIPAL, "api"),
        human_principal=_human_principal,
        config_principal=_config_principal,
    )
)

app.include_router(
    internal_api.build_router(
        pool=pool,
        auth=require_controller_token,
        job_factory=_create_attempt_job,
        actor=CONTROLLER_PRINCIPAL,
    )
)

task_api.register_mcp(
    mcp,
    pool=pool,
    jobs=TASK_JOB_BRIDGE,
    actor=MCP_PRINCIPAL,
    source="grok",
)

# Keep the MCP mount last so the explicit REST and health routes above retain
# precedence. The mounted SDK app serves Streamable HTTP at /mcp.
app.mount("/", mcp_http_app)
