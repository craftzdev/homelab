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
        "Submit bounded jobs to the AI Business Worker and inspect their results. "
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


class JobCreate(BaseModel):
    action: Literal[
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
            "SELECT last_event_sequence FROM jobs WHERE id = %s FOR UPDATE",
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


# Keep the MCP mount last so the explicit REST and health routes above retain
# precedence. The mounted SDK app serves Streamable HTTP at /mcp.
app.mount("/", mcp_http_app)
