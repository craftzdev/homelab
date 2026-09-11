from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Literal

import jwt
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from jwt import PyJWKClient
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from starlette.requests import Request
from starlette.responses import JSONResponse


API_SURFACE = os.environ.get("API_SURFACE", "public")
DATABASE_URL = os.environ["DATABASE_URL"]
GATEWAY_API_TOKEN = os.environ["GATEWAY_API_TOKEN"]
WORKER_CALLBACK_TOKEN = os.environ["WORKER_CALLBACK_TOKEN"]
WORKER_BASE_URL = os.environ.get("WORKER_BASE_URL", "").rstrip("/")
WORKER_API_TOKEN = os.environ.get("WORKER_API_TOKEN", "")
HUMAN_APPROVAL_TOKEN = os.environ.get("HUMAN_APPROVAL_TOKEN", "")
CLOUDFLARE_ACCESS_REQUIRED = os.environ.get(
    "CLOUDFLARE_ACCESS_REQUIRED", "false"
).lower() in {"1", "true", "yes"}
CLOUDFLARE_ACCESS_TEAM_DOMAIN = os.environ.get(
    "CLOUDFLARE_ACCESS_TEAM_DOMAIN", ""
).removeprefix("https://").rstrip("/")
CLOUDFLARE_ACCESS_AUD = os.environ.get("CLOUDFLARE_ACCESS_AUD", "")

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
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    pool.open()
    pool.wait()
    with pool.connection() as connection:
        connection.execute(SCHEMA_SQL)
        connection.commit()
    try:
        async with mcp_http_app.router.lifespan_context(mcp_http_app):
            yield
    finally:
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
            headers = {"WWW-Authenticate": "Bearer"} if error.status_code == 401 else None
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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    supplied = authorization.removeprefix("Bearer ")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

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


def require_human_approval_token(
    human_approval_token: str | None = Header(default=None, alias="X-Human-Approval-Token"),
) -> None:
    if not HUMAN_APPROVAL_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="human approval is not configured",
        )
    if not human_approval_token or not hmac.compare_digest(
        human_approval_token, HUMAN_APPROVAL_TOKEN
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


class ProjectCreate(BaseModel):
    project_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(min_length=1, max_length=200)
    idea: str = Field(min_length=20, max_length=8000)


class ProjectRepositoryUpdate(BaseModel):
    repository_url: str = Field(min_length=10, max_length=2000, pattern=r"^https://github\.com/")


class ProjectProductionUpdate(BaseModel):
    production_url: str = Field(min_length=10, max_length=2000, pattern=r"^https://")


class ProjectAnalyticsUpdate(BaseModel):
    analytics: dict[str, Any] = Field(min_length=1, max_length=50)


class JobCreate(BaseModel):
    action: Literal[
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


MCPAction = Literal[
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


def dispatch_job(job_id: uuid.UUID, request: JobCreate) -> None:
    """Dispatch a persisted Gateway job to one typed Worker endpoint."""
    endpoint = WORKER_ENDPOINTS.get(request.action)
    if endpoint is None:
        with pool.connection() as connection:
            connection.execute(
                "UPDATE jobs SET state = 'FAILED_FINAL', result = %s, updated_at = %s "
                "WHERE id = %s AND state = 'QUEUED'",
                (
                    json.dumps({"error": "action has no enabled worker executor"}),
                    utcnow(),
                    job_id,
                ),
            )
            connection.commit()
        return

    if not WORKER_BASE_URL or not WORKER_API_TOKEN:
        error = "worker dispatch is not configured"
    else:
        dispatch_id = f"gateway:{job_id}:1"
        body = json.dumps(
            {
                "gateway_job_id": str(job_id),
                "dispatch_id": dispatch_id,
                "action": request.action,
                "project_id": request.project_id,
                "parameters": request.parameters,
                "limits": request.limits,
            }
        ).encode()
        worker_request = urllib.request.Request(
            f"{WORKER_BASE_URL}/v1/jobs/{endpoint}",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {WORKER_API_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(worker_request, timeout=20) as response:
                if response.status != status.HTTP_202_ACCEPTED:
                    raise RuntimeError(f"worker returned HTTP {response.status}")
                worker_response = json.load(response)
            worker_job_id = worker_response["worker_job_id"]
            with pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE jobs
                       SET state = CASE WHEN state = 'QUEUED' THEN 'DISPATCHED' ELSE state END,
                           worker_job_id = %s,
                           updated_at = %s
                     WHERE id = %s
                    """,
                    (worker_job_id, utcnow(), job_id),
                )
                connection.commit()
            return
        except (urllib.error.URLError, TimeoutError, RuntimeError, KeyError, ValueError):
            error = "worker dispatch failed"

    with pool.connection() as connection:
        connection.execute(
            "UPDATE jobs SET state = 'FAILED_FINAL', result = %s, updated_at = %s "
            "WHERE id = %s AND state = 'QUEUED'",
            (json.dumps({"error": error}), utcnow(), job_id),
        )
        connection.commit()


def _public_job(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": str(row["id"]),
        "project_id": row["project_id"],
        "action": row["action"],
        "environment": row["environment"],
        "state": row["state"],
        "worker_job_id": row["worker_job_id"],
        "result": row["result"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _persist_job(
    request: JobCreate, idempotency_key: str
) -> tuple[dict[str, Any], bool]:
    """Persist a job once and return the existing row on an idempotent replay."""
    now = utcnow()
    job_id = uuid.uuid7() if hasattr(uuid, "uuid7") else uuid.uuid4()
    payload = request.model_dump(mode="json")
    payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    with pool.connection() as connection:
        try:
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
            connection.commit()
            return row, True
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
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _project_event(
    connection: Any, project_id: str, event_type: str, payload: dict[str, Any]
) -> None:
    connection.execute(
        "INSERT INTO project_events (id, project_id, event_type, payload, created_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (uuid.uuid4(), project_id, event_type, json.dumps(payload), utcnow()),
    )


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
            _project_event(connection, request.project_id, "idea.submitted", request.model_dump())
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
        row = connection.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return _public_project(row)


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
        _project_event(connection, project_id, "repository.registered", request.model_dump())
        connection.commit()
    return _public_project(row)


@app.post("/v1/projects/{project_id}/approval", status_code=status.HTTP_201_CREATED)
def request_production_approval(
    project_id: str, _: None = Depends(require_gateway_token)
) -> dict[str, Any]:
    now = utcnow()
    approval_id = uuid.uuid4()
    with pool.connection() as connection:
        project = connection.execute(
            "SELECT state FROM projects WHERE id = %s FOR UPDATE", (project_id,)
        ).fetchone()
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
        if project["state"] != "QA_PASSED":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="project must pass QA before production approval",
            )
        connection.execute(
            "INSERT INTO approvals (id, project_id, kind, state, requested_at) "
            "VALUES (%s, %s, 'production.deploy', 'PENDING', %s)",
            (approval_id, project_id, now),
        )
        connection.execute(
            "UPDATE projects SET state = 'AWAITING_APPROVAL', updated_at = %s WHERE id = %s",
            (now, project_id),
        )
        _project_event(connection, project_id, "approval.requested", {"approval_id": str(approval_id)})
        connection.commit()
    return {"approval_id": str(approval_id), "project_id": project_id, "state": "PENDING"}


@app.post("/v1/approvals/{approval_id}/approve")
def approve_production(
    approval_id: uuid.UUID,
    _: None = Depends(require_gateway_token),
    __: None = Depends(require_human_approval_token),
) -> dict[str, Any]:
    now = utcnow()
    with pool.connection() as connection:
        approval = connection.execute(
            "UPDATE approvals SET state = 'APPROVED', resolved_at = %s "
            "WHERE id = %s AND state = 'PENDING' RETURNING project_id",
            (now, approval_id),
        ).fetchone()
        if approval is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="approval is not pending"
            )
        connection.execute(
            "UPDATE projects SET state = 'DEPLOY_APPROVED', updated_at = %s WHERE id = %s",
            (now, approval["project_id"]),
        )
        _project_event(
            connection,
            approval["project_id"],
            "approval.approved",
            {"approval_id": str(approval_id)},
        )
        connection.commit()
    return {"approval_id": str(approval_id), "project_id": approval["project_id"], "state": "APPROVED"}


@app.put("/v1/projects/{project_id}/production")
def record_production_release(
    project_id: str,
    request: ProjectProductionUpdate,
    _: None = Depends(require_gateway_token),
    __: None = Depends(require_human_approval_token),
) -> dict[str, Any]:
    with pool.connection() as connection:
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
        _project_event(connection, project_id, "production.released", request.model_dump())
        connection.commit()
    return _public_project(row)


@app.put("/v1/projects/{project_id}/analytics")
def record_project_analytics(
    project_id: str,
    request: ProjectAnalyticsUpdate,
    _: None = Depends(require_gateway_token),
) -> dict[str, Any]:
    with pool.connection() as connection:
        row = connection.execute(
            "UPDATE projects SET analytics = %s, state = 'MEASURING', updated_at = %s "
            "WHERE id = %s AND production_url IS NOT NULL RETURNING *",
            (json.dumps(request.analytics), utcnow(), project_id),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="analytics require a recorded production release",
            )
        _project_event(connection, project_id, "analytics.recorded", request.model_dump())
        connection.commit()
    return _public_project(row)


@app.post("/v1/jobs", status_code=status.HTTP_202_ACCEPTED)
def create_job(
    request: JobCreate,
    background: BackgroundTasks,
    _: None = Depends(require_gateway_token),
    idempotency_key: str = Header(min_length=8, max_length=200),
) -> dict[str, Any]:
    try:
        row, created = _persist_job(request, idempotency_key)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error

    if created:
        background.add_task(dispatch_job, row["id"], request)
    return _public_job(row)


@app.get("/v1/jobs/{job_id}")
def get_job(
    job_id: uuid.UUID, _: None = Depends(require_gateway_token)
) -> dict[str, Any]:
    row = _load_job(job_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return _public_job(row)


@app.post("/v1/worker-events", status_code=status.HTTP_202_ACCEPTED)
def receive_worker_event(
    event: WorkerEvent, _: None = Depends(require_callback_token)
) -> dict[str, Any]:
    state_map = {
        "accepted": "ACCEPTED",
        "started": "RUNNING",
        "progress": "RUNNING",
        "completed": "SUCCEEDED",
        "failed": "FAILED_FINAL",
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

        job = connection.execute(
            "SELECT last_event_sequence, action, project_id FROM jobs WHERE id = %s FOR UPDATE",
            (event.gateway_job_id,),
        ).fetchone()
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")

        if event.sequence <= job["last_event_sequence"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="event sequence must advance monotonically",
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
            if event.event_type == "completed":
                transition = {
                    "product.plan": ("PRD_READY", "prd", event.data.get("report")),
                    "code.build": ("PREVIEW_READY", "build_job_id", event.gateway_job_id),
                    "code.fix": ("PREVIEW_READY", "build_job_id", event.gateway_job_id),
                    "qa.review": ("QA_PASSED", "qa_job_id", event.gateway_job_id),
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
                        {"gateway_job_id": str(event.gateway_job_id)},
                    )
            elif event.event_type == "failed":
                next_state = "QA_FAILED" if job["action"] == "qa.review" else "FAILED"
                connection.execute(
                    "UPDATE projects SET state = %s, updated_at = %s WHERE id = %s",
                    (next_state, now, job["project_id"]),
                )
            connection.commit()
        except UniqueViolation:
            connection.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="event conflicts with an existing sequence",
            )

    return {"accepted": True, "event_id": event.event_id, "duplicate": False}


def _parse_job_id(job_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(job_id)
    except ValueError as error:
        raise ValueError("job_id must be a valid UUID") from error


@mcp.tool(
    name="submit_job",
    description=(
        "Submit one bounded research, analysis, build, fix, or test job. "
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
    row, created = _persist_job(request, key)
    if created:
        dispatch_job(row["id"], request)
        row = _load_job(row["id"]) or row
    response = _public_job(row)
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
        "Return the signed Tailnet-only review URL from a completed job when one "
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
        "Request human production approval after QA has passed. "
        "This tool cannot grant the approval."
    ),
    structured_output=True,
)
def mcp_request_production_approval(project_id: str) -> dict[str, Any]:
    return request_production_approval(project_id, None)


# Keep the MCP mount last so the explicit REST and health routes above retain
# precedence. The mounted SDK app serves Streamable HTTP at /mcp.
app.mount("/", mcp_http_app)
