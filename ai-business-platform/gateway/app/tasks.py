"""Task ledger: Task, Workflow Run, Step, Attempt, Artifact and the event feed.

The Gateway database is the single source of truth for what a request is, who
is working on it and how far it got, so a UI card and a Grok tool call read the
same state instead of each keeping their own. Every function here takes an open
connection and never commits: the caller decides the transaction boundary, and
business state, its event and the cursor that publishes it always land in one
commit.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from app import workflows

TASK_TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")
# A Run that has ended, however it ended. SUPERSEDED means the request it was
# running was replaced before it delivered anything.
RUN_TERMINAL = ("COMPLETED", "FAILED", "CANCELLED", "SUPERSEDED")
STEP_TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "SKIPPED")
ATTEMPT_TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")

# Task statuses that need a human before anything else can move.
ATTENTION_STATUSES = ("WAITING_INPUT", "WAITING_REVIEW", "BLOCKED", "FAILED")

PRIORITIES = ("low", "normal", "high", "urgent")

ARTIFACT_KINDS = {
    "product.plan": "prd",
    "qa.review": "qa-report",
    "test.run": "test-report",
    "code.build": "code-change",
    "code.fix": "code-change",
    "growth.plan": "growth-plan",
    "content.draft": "content-draft",
    "analytics.read": "data-extract",
    "stripe.read": "data-extract",
    "browser.research": "research-notes",
    "video.generate": "video",
}

EVENT_STREAM = "platform"

SCHEMA_SQL = """
SELECT pg_advisory_xact_lock(734859204);

CREATE TABLE IF NOT EXISTS tasks (
    id UUID PRIMARY KEY,
    display_number BIGSERIAL NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    stage_key TEXT NOT NULL,
    control_state TEXT NOT NULL DEFAULT 'ACTIVE',
    workflow_id TEXT NOT NULL,
    orchestration_mode TEXT NOT NULL,
    environment TEXT NOT NULL,
    active_run_id UUID,
    input_revision INTEGER NOT NULL DEFAULT 1,
    priority TEXT NOT NULL DEFAULT 'normal',
    sort_rank DOUBLE PRECISION NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS tasks_project_status ON tasks(project_id, status);
CREATE INDEX IF NOT EXISTS tasks_stage ON tasks(stage_key);
CREATE INDEX IF NOT EXISTS tasks_listing ON tasks(created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS task_input_revisions (
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    objective TEXT NOT NULL,
    acceptance_criteria JSONB NOT NULL DEFAULT '[]'::jsonb,
    context_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (task_id, revision)
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    workflow_id TEXT NOT NULL,
    workflow_version TEXT NOT NULL,
    config_release_id TEXT,
    input_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    orchestration_mode TEXT NOT NULL,
    revision_cycles INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    fencing_token BIGINT NOT NULL DEFAULT 0,
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ
);

-- One Task holds at most one Run that has not ended. A superseded Run has ended:
-- the request it was running was replaced before it delivered anything.
-- The predicate is part of the index name, because `IF NOT EXISTS` keeps an index
-- that already exists under the same name with an older predicate.
DROP INDEX IF EXISTS workflow_runs_one_active_per_task;
DROP INDEX IF EXISTS workflow_runs_one_open_per_task;
CREATE UNIQUE INDEX IF NOT EXISTS workflow_runs_one_open_per_task_v2
    ON workflow_runs(task_id)
    WHERE status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED', 'SUPERSEDED');

CREATE TABLE IF NOT EXISTS workflow_steps (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    logical_key TEXT NOT NULL,
    cycle INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 1,
    stage_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    action TEXT,
    agent_binding TEXT,
    output_contract TEXT,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (run_id, logical_key, cycle)
);

CREATE TABLE IF NOT EXISTS step_attempts (
    id UUID PRIMARY KEY,
    step_id UUID NOT NULL REFERENCES workflow_steps(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    job_id UUID UNIQUE,
    status TEXT NOT NULL,
    failure_class TEXT,
    input_manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    execution_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_summary JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    UNIQUE (step_id, attempt_number)
);

-- One Step holds at most one non-terminal Attempt.
CREATE UNIQUE INDEX IF NOT EXISTS step_attempts_one_active_per_step
    ON step_attempts(step_id)
    WHERE status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED');

CREATE TABLE IF NOT EXISTS artifacts (
    id UUID PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
    run_id UUID REFERENCES workflow_runs(id) ON DELETE SET NULL,
    producer_attempt_id UUID REFERENCES step_attempts(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size BIGINT,
    digest TEXT NOT NULL,
    input_revision INTEGER,
    storage_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
    manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS artifacts_task ON artifacts(task_id, created_at DESC);
-- One Attempt produces at most one artifact of a kind, so a redelivered
-- completion cannot duplicate evidence. NULL producers stay distinct.
CREATE UNIQUE INDEX IF NOT EXISTS artifacts_one_per_attempt_kind
    ON artifacts(producer_attempt_id, kind);

CREATE TABLE IF NOT EXISTS task_messages (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    author TEXT NOT NULL,
    applies_to TEXT NOT NULL DEFAULT 'note_only',
    input_revision INTEGER,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS task_messages_task ON task_messages(task_id, created_at);

-- An instruction is outstanding until an execution has been given it, or until the
-- requester folds it into the request itself. Until then it must reach the next
-- step whole rather than competing for room with reference material.
ALTER TABLE task_messages
    ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ;

ALTER TABLE task_messages
    ADD COLUMN IF NOT EXISTS consumed_by TEXT;

CREATE TABLE IF NOT EXISTS platform_events (
    cursor BIGINT PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_revision INTEGER,
    type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    actor TEXT NOT NULL,
    correlation_id TEXT,
    causation_id TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1,
    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS platform_events_aggregate
    ON platform_events(aggregate_type, aggregate_id, cursor);

CREATE TABLE IF NOT EXISTS event_counter (
    stream_id TEXT PRIMARY KEY,
    last_cursor BIGINT NOT NULL DEFAULT 0
);

INSERT INTO event_counter (stream_id, last_cursor) VALUES ('platform', 0)
    ON CONFLICT (stream_id) DO NOTHING;

-- What was asked for, frozen with the input revision: a Task registered now
-- and started later must run exactly the request it was accepted with.
ALTER TABLE task_input_revisions
    ADD COLUMN IF NOT EXISTS reason TEXT NOT NULL DEFAULT '';

ALTER TABLE task_input_revisions
    ADD COLUMN IF NOT EXISTS request JSONB NOT NULL DEFAULT '{}'::jsonb;

-- The last version that changed what is being asked for. A revision that only
-- changes a limit or adds a reference does not supersede anything a person said, so
-- work, specifications, answers and corrections from before it still stand. This
-- carries that forward, so "is it still current?" is one comparison.
ALTER TABLE task_input_revisions
    ADD COLUMN IF NOT EXISTS requirements_revision INTEGER;

CREATE TABLE IF NOT EXISTS commands (
    id UUID PRIMARY KEY,
    principal_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    command_type TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL,
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (principal_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS commands_target ON commands(target_type, target_id, created_at DESC);

-- Deliveries the Gateway still owes a Worker. A Job is registered and its
-- delivery is queued in one commit, so losing the API process cannot lose the
-- execution, and every retry reuses the same dispatch_id.
CREATE TABLE IF NOT EXISTS job_dispatches (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL UNIQUE,
    dispatch_id TEXT NOT NULL UNIQUE,
    endpoint TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    selected_worker TEXT,
    last_error TEXT,
    retry_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS job_dispatches_due
    ON job_dispatches(state, retry_at);

-- Runtime requests the Gateway owes a Worker, such as stopping an execution. A
-- requested stop must survive the request that asked for it, or a card would say
-- "stopping" while nothing ever asked the Worker to stop.
CREATE TABLE IF NOT EXISTS worker_commands (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL,
    worker_job_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    requested_by TEXT NOT NULL,
    retry_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (job_id, kind)
);

CREATE INDEX IF NOT EXISTS worker_commands_due ON worker_commands(state, retry_at);

-- What the Workers last reported about themselves. A fresh heartbeat and the
-- ability to run an action are separate facts, so both are stored.
CREATE TABLE IF NOT EXISTS workers (
    logical_id TEXT PRIMARY KEY,
    pool TEXT,
    instance_id TEXT,
    instance_started_at TIMESTAMPTZ,
    accepting_jobs BOOLEAN,
    intake_revision INTEGER,
    max_concurrency INTEGER,
    running INTEGER,
    queued INTEGER,
    callback_backlog INTEGER,
    actions JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL,
    detail TEXT,
    observed_at TIMESTAMPTZ NOT NULL,
    reported JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Executions the Worker could not confirm it stopped. They hold a slot, so the
-- capacity a Worker reports is not the capacity it has.
ALTER TABLE workers
    ADD COLUMN IF NOT EXISTS not_stopped INTEGER NOT NULL DEFAULT 0;

-- The intake state the Gateway wants each Worker to be in. The Gateway holds the
-- desired value; the Worker reports what it actually applied, so the two can be
-- compared instead of trusting a value kept only in a Pod.
CREATE TABLE IF NOT EXISTS worker_overrides (
    worker_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 1,
    accepting_jobs BOOLEAN NOT NULL,
    reason TEXT,
    actor TEXT NOT NULL,
    -- Draining has no default expiry: intake must not resume by itself.
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

-- Which Controllers have been evaluating Runs, so the Gateway can say whether a
-- Controller-driven Workflow can actually be started right now.
CREATE TABLE IF NOT EXISTS controllers (
    owner TEXT PRIMARY KEY,
    last_seen_at TIMESTAMPTZ NOT NULL,
    leases BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS input_requests (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id UUID REFERENCES workflow_runs(id) ON DELETE CASCADE,
    step_id UUID REFERENCES workflow_steps(id) ON DELETE CASCADE,
    questions JSONB NOT NULL,
    resume_step TEXT,
    input_revision INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'OPEN',
    answers JSONB,
    answered_by TEXT,
    answer_revision INTEGER,
    created_at TIMESTAMPTZ NOT NULL,
    answered_at TIMESTAMPTZ
);

-- One open question set per Task: an answer must be unambiguous about what it
-- answers and where the Run resumes.
CREATE UNIQUE INDEX IF NOT EXISTS input_requests_one_open_per_task
    ON input_requests(task_id) WHERE state = 'OPEN';

CREATE TABLE IF NOT EXISTS decisions (
    id UUID PRIMARY KEY,
    kind TEXT NOT NULL,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id UUID NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    target_digest TEXT NOT NULL,
    target JSONB NOT NULL,
    input_revision INTEGER NOT NULL,
    state TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS decisions_task ON decisions(task_id, created_at DESC);

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS task_id UUID;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS attempt_id UUID;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS config_release_id TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS actor_id TEXT;
-- Recorded when the job is handed to a Worker, so a callback can be checked
-- against the delivery it claims to belong to.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS dispatch_id TEXT;
CREATE INDEX IF NOT EXISTS jobs_task ON jobs(task_id);

-- Nullable for pre-ledger rows, but a present link must be real and exclusive
-- in both directions.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'jobs_task_fk') THEN
        ALTER TABLE jobs ADD CONSTRAINT jobs_task_fk
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'jobs_attempt_fk') THEN
        ALTER TABLE jobs ADD CONSTRAINT jobs_attempt_fk
            FOREIGN KEY (attempt_id) REFERENCES step_attempts(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_per_attempt
    ON jobs(attempt_id) WHERE attempt_id IS NOT NULL;
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_schema(connection: Any) -> None:
    connection.execute(SCHEMA_SQL)


def _new_id() -> uuid.UUID:
    return uuid.uuid7() if hasattr(uuid, "uuid7") else uuid.uuid4()


def canonical_digest(payload: Any) -> tuple[str, int]:
    """SHA-256 over a canonical encoding, so the same content is the same id."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


# --------------------------------------------------------------------------- #
# Event feed
# --------------------------------------------------------------------------- #


def record_event(
    connection: Any,
    *,
    aggregate_type: str,
    aggregate_id: str,
    type: str,
    actor: str,
    payload: dict[str, Any] | None = None,
    aggregate_revision: int | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    occurred_at: datetime | None = None,
) -> int:
    """Append one event and allocate its cursor inside the caller's transaction.

    The counter row is locked until commit, so a reader that sees cursor N can
    rely on every cursor up to N already being committed. A plain sequence would
    let a later cursor become visible first and silently skip an event.
    """
    cursor = connection.execute(
        "UPDATE event_counter SET last_cursor = last_cursor + 1 "
        "WHERE stream_id = %s RETURNING last_cursor",
        (EVENT_STREAM,),
    ).fetchone()["last_cursor"]
    connection.execute(
        """
        INSERT INTO platform_events (
            cursor, event_id, aggregate_type, aggregate_id, aggregate_revision,
            type, payload, actor, correlation_id, causation_id, occurred_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            cursor,
            _new_id(),
            aggregate_type,
            aggregate_id,
            aggregate_revision,
            type,
            json.dumps(payload or {}, default=str),
            actor,
            correlation_id,
            causation_id,
            occurred_at or utcnow(),
        ),
    )
    return cursor


class SnapshotRequired(RuntimeError):
    """The requested cursor cannot be continued from the retained feed."""


def events_after(connection: Any, after: int, limit: int = 200) -> dict[str, Any]:
    bounds = connection.execute(
        "SELECT (SELECT last_cursor FROM event_counter WHERE stream_id = %s) AS latest, "
        "(SELECT min(cursor) FROM platform_events) AS oldest",
        (EVENT_STREAM,),
    ).fetchone()
    if after > (bounds["latest"] or 0):
        # A cursor from a different or reset ledger cannot be resumed.
        raise SnapshotRequired("cursor is ahead of this event stream")
    latest = bounds["latest"] or 0
    if bounds["oldest"] is None:
        if after < latest:
            # Everything up to `latest` was pruned, so the gap cannot be read.
            raise SnapshotRequired("events after this cursor are no longer retained")
    elif after + 1 < bounds["oldest"]:
        # Events between the cursor and the retained feed are gone; continuing
        # would silently skip them.
        raise SnapshotRequired("events after this cursor are no longer retained")
    rows = connection.execute(
        "SELECT * FROM platform_events WHERE cursor > %s ORDER BY cursor LIMIT %s",
        (after, limit),
    ).fetchall()
    last = connection.execute(
        "SELECT last_cursor FROM event_counter WHERE stream_id = %s", (EVENT_STREAM,)
    ).fetchone()["last_cursor"]
    return {
        "events": [_public_event(row) for row in rows],
        "next_cursor": rows[-1]["cursor"] if rows else after,
        "latest_cursor": last,
        "has_more": bool(rows) and rows[-1]["cursor"] < last,
    }


def _public_event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "cursor": row["cursor"],
        "event_id": str(row["event_id"]),
        "aggregate_type": row["aggregate_type"],
        "aggregate_id": row["aggregate_id"],
        "aggregate_revision": row["aggregate_revision"],
        "type": row["type"],
        "payload": row["payload"],
        "actor": row["actor"],
        "correlation_id": row["correlation_id"],
        "causation_id": row["causation_id"],
        "schema_version": row["schema_version"],
        "occurred_at": row["occurred_at"].isoformat(),
    }


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


def create_task(
    connection: Any,
    *,
    project_id: str,
    title: str,
    objective: str,
    workflow_id: str,
    environment: str,
    actor: str,
    source: str,
    acceptance_criteria: Sequence[str] = (),
    context_refs: Sequence[str] = (),
    priority: str = "normal",
    request: dict[str, Any] | None = None,
    orchestration_mode: str = "workflow-v1",
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Register one request as a Task with its first immutable input revision."""
    now = utcnow()
    task_id = _new_id()
    row = connection.execute(
        """
        INSERT INTO tasks (
            id, project_id, title, objective, status, stage_key, control_state,
            workflow_id, orchestration_mode, environment, input_revision, priority,
            sort_rank, revision, created_by, source, created_at, updated_at
        ) VALUES (
            %s, %s, %s, %s, 'READY', 'intake', 'ACTIVE',
            %s, %s, %s, 1, %s,
            %s, 1, %s, %s, %s, %s
        ) RETURNING *
        """,
        (
            task_id,
            project_id,
            title,
            objective,
            workflow_id,
            orchestration_mode,
            environment,
            priority,
            now.timestamp(),
            actor,
            source,
            now,
            now,
        ),
    ).fetchone()
    connection.execute(
        """
        INSERT INTO task_input_revisions (
            task_id, revision, objective, acceptance_criteria, context_refs,
            request, requirements_revision, created_by, created_at
        ) VALUES (%s, 1, %s, %s, %s, %s, 1, %s, %s)
        """,
        (
            task_id,
            objective,
            json.dumps(list(acceptance_criteria)),
            json.dumps(list(context_refs)),
            json.dumps(request or {}, default=str),
            actor,
            now,
        ),
    )
    record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task_id),
        aggregate_revision=1,
        type="task.created",
        actor=actor,
        payload={
            "project_id": project_id,
            "title": title,
            "workflow_id": workflow_id,
            "orchestration_mode": orchestration_mode,
            "source": source,
            "priority": priority,
        },
        correlation_id=correlation_id or str(task_id),
        occurred_at=now,
    )
    return row


def _single_action_target(
    connection: Any,
    *,
    task: dict[str, Any],
    action: str,
    parameters: dict[str, Any],
) -> dict[str, Any] | None:
    """What a single-action verification is a verification of.

    A workflow verification is bound to the artifact it is about, and that binding
    is what its evidence is checked against. A single action asks for the same thing
    with a `source_worker_job_id`, so it is bound the same way: without this, a pass
    would be checked against nothing. When this Gateway holds no recorded change for
    that execution there is nothing to bind, and the pass is not treated as
    verification of one.
    """
    if action not in QA_ACTIONS:
        return None
    source = parameters.get("source_worker_job_id")
    if not isinstance(source, str) or not source:
        return None
    change = connection.execute(
        """
        SELECT a.id, a.digest
          FROM artifacts a
          JOIN step_attempts att ON att.id = a.producer_attempt_id
          JOIN jobs j ON j.id = att.job_id
         WHERE j.worker_job_id = %s AND a.project_id = %s AND a.kind = 'code-change'
         ORDER BY a.created_at DESC LIMIT 1
        """,
        (source, task["project_id"]),
    ).fetchone()
    if change is None:
        return None
    return {
        "artifact_id": str(change["id"]),
        "digest": change["digest"],
        "source_worker_job_id": source,
    }


def start_single_action_run(
    connection: Any,
    *,
    task: dict[str, Any],
    action: str,
    job_id: uuid.UUID,
    parameters: dict[str, Any] | None = None,
    limits: dict[str, int] | None = None,
    agent_binding: str | None = None,
    actor: str,
) -> dict[str, Any]:
    """Open a single-action Run: one Step, one Attempt, one Job.

    Used both for an explicit single-action Task and for a legacy submit_job
    call, so every Job the Gateway accepts is traceable through the ledger.
    """
    workflow = workflows.SINGLE_ACTION_V1
    now = utcnow()
    stage = workflows.action_stage(action)
    run_id = _new_id()
    step_id = _new_id()
    attempt_id = _new_id()

    run = connection.execute(
        """
        INSERT INTO workflow_runs (
            id, task_id, workflow_id, workflow_version, input_revision, status,
            orchestration_mode, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, 'ACTIVE', %s, %s, %s) RETURNING *
        """,
        (
            run_id,
            task["id"],
            workflow.id,
            workflow.version,
            task["input_revision"],
            task["orchestration_mode"],
            now,
            now,
        ),
    ).fetchone()
    connection.execute(
        """
        INSERT INTO workflow_steps (
            id, run_id, logical_key, cycle, position, stage_key, kind, action,
            agent_binding, output_contract, status, created_at, updated_at
        ) VALUES (%s, %s, 'execute', 1, 1, %s, 'action', %s, %s, %s, 'RUNNING', %s, %s)
        """,
        (
            step_id,
            run_id,
            stage,
            action,
            agent_binding,
            workflow.step("execute").output_contract,
            now,
            now,
        ),
    )
    input_digest, _ = canonical_digest({"action": action, "parameters": parameters or {}})
    try:
        task_criteria = _listed(current_input(connection, task)["acceptance_criteria"])
    except LookupError:
        # A legacy submit_job Task has no recorded input version of its own.
        task_criteria = _listed(task.get("acceptance_criteria"))
    verification_target = _single_action_target(
        connection, task=task, action=action, parameters=parameters or {}
    )
    connection.execute(
        """
        INSERT INTO step_attempts (
            id, step_id, attempt_number, job_id, status, input_manifest,
            execution_snapshot, created_at, started_at
        ) VALUES (%s, %s, 1, %s, 'RUNNING', %s, %s, %s, %s)
        """,
        (
            attempt_id,
            step_id,
            job_id,
            json.dumps(
                {
                    "action": action,
                    "parameters": parameters or {},
                    # What the Task asks to be judged by, whether or not the action
                    # has a field for it: a verification of this Attempt is required
                    # to cover these as well.
                    "acceptance_criteria": _listed(task_criteria),
                    **(
                        {"verification_target": verification_target}
                        if verification_target
                        else {}
                    ),
                    "input_revision": task["input_revision"],
                    "digest": input_digest,
                },
                default=str,
            ),
            json.dumps(
                {
                    "workflow_id": workflow.id,
                    "workflow_version": workflow.version,
                    "limits": limits or {},
                    "environment": task["environment"],
                    "agent_binding": agent_binding,
                },
                default=str,
            ),
            now,
            now,
        ),
    )
    connection.execute(
        "UPDATE jobs SET task_id = %s, attempt_id = %s, actor_id = %s WHERE id = %s",
        (task["id"], attempt_id, actor, job_id),
    )
    task = _touch_task(
        connection,
        task_id=task["id"],
        status="ACTIVE",
        stage_key=stage,
        active_run_id=run_id,
    )
    record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=task["revision"],
        type="task.run_started",
        actor=actor,
        payload={
            "run_id": str(run_id),
            "workflow_id": workflow.id,
            "action": action,
            "job_id": str(job_id),
            "attempt_id": str(attempt_id),
            "stage_key": stage,
        },
        correlation_id=str(task["id"]),
        occurred_at=now,
    )
    return {"task": task, "run": run, "step_id": step_id, "attempt_id": attempt_id}


CONTROLLER_FRESH_SECONDS = 90

# Communication retries are counted and bounded separately from work retries, and
# are never charged to the Workflow's revision budget.
DISPATCH_BACKOFF_SECONDS = (1, 2, 4, 8, 16, 30)
DISPATCH_RECONCILE_SECONDS = 60


def enqueue_dispatch(
    connection: Any,
    *,
    job_id: uuid.UUID,
    dispatch_id: str,
    endpoint: str,
    payload: dict[str, Any],
    request_hash: str,
) -> dict[str, Any]:
    """Queue one delivery in the same transaction that registered the Job."""
    now = utcnow()
    return connection.execute(
        """
        INSERT INTO job_dispatches (
            id, job_id, dispatch_id, endpoint, request_hash, payload, state,
            retry_at, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, 'PENDING', %s, %s, %s)
        ON CONFLICT (job_id) DO NOTHING
        RETURNING *
        """,
        (
            _new_id(),
            job_id,
            dispatch_id,
            endpoint,
            request_hash,
            json.dumps(payload, default=str),
            now,
            now,
            now,
        ),
    ).fetchone()


def enqueue_worker_command(
    connection: Any,
    *,
    job_id: uuid.UUID,
    worker_job_id: str,
    kind: str,
    actor: str,
) -> dict[str, Any] | None:
    """Queue a runtime request to the Worker in the caller's transaction."""
    now = utcnow()
    return connection.execute(
        """
        INSERT INTO worker_commands (
            id, job_id, worker_job_id, kind, state, requested_by, retry_at,
            created_at, updated_at
        ) VALUES (%s, %s, %s, %s, 'PENDING', %s, %s, %s, %s)
        ON CONFLICT (job_id, kind) DO NOTHING
        RETURNING *
        """,
        (_new_id(), job_id, worker_job_id, kind, actor, now, now, now),
    ).fetchone()


# A claim that never settled belongs to a scheduler that stopped. After this it
# is claimable again, because a stranded delivery is worse than a repeated one
# the Worker deduplicates anyway.
CLAIM_LEASE_SECONDS = 120


def claim_worker_commands(connection: Any, limit: int = 5) -> list[dict[str, Any]]:
    return connection.execute(
        """
        UPDATE worker_commands SET state = 'SENDING', attempts = attempts + 1,
               updated_at = %s
         WHERE id IN (
            SELECT id FROM worker_commands
             WHERE (state IN ('PENDING', 'UNKNOWN') AND retry_at <= %s)
                OR (state = 'SENDING' AND updated_at < %s)
             ORDER BY retry_at
             LIMIT %s
             FOR UPDATE SKIP LOCKED
         )
        RETURNING *
        """,
        (
            utcnow(),
            utcnow(),
            utcnow() - timedelta(seconds=CLAIM_LEASE_SECONDS),
            limit,
        ),
    ).fetchall()


def settle_worker_command(
    connection: Any, *, command: dict[str, Any], state: str, error: str | None = None
) -> dict[str, Any]:
    return connection.execute(
        "UPDATE worker_commands SET state = %s, last_error = %s, retry_at = %s, "
        "updated_at = %s WHERE id = %s RETURNING *",
        (
            state,
            error,
            dispatch_backoff(command["attempts"]) if state == "UNKNOWN" else utcnow(),
            utcnow(),
            command["id"],
        ),
    ).fetchone()


def claim_dispatches(connection: Any, limit: int = 5) -> list[dict[str, Any]]:
    """Take the deliveries that are due, without two schedulers taking one."""
    return connection.execute(
        """
        UPDATE job_dispatches SET state = 'SENDING', attempts = attempts + 1,
               updated_at = %s
         WHERE id IN (
            SELECT id FROM job_dispatches
             WHERE (state IN ('PENDING', 'UNKNOWN') AND retry_at <= %s)
                -- A claim whose scheduler stopped before settling it.
                OR (state = 'SENDING' AND updated_at < %s)
             ORDER BY retry_at
             LIMIT %s
             FOR UPDATE SKIP LOCKED
         )
        RETURNING *
        """,
        (
            utcnow(),
            utcnow(),
            utcnow() - timedelta(seconds=CLAIM_LEASE_SECONDS),
            limit,
        ),
    ).fetchall()


def dispatch_backoff(attempts: int) -> datetime:
    """When to try a delivery again: bounded backoff, then periodic checking."""
    if attempts <= len(DISPATCH_BACKOFF_SECONDS):
        seconds = DISPATCH_BACKOFF_SECONDS[attempts - 1]
    else:
        seconds = DISPATCH_RECONCILE_SECONDS
    return utcnow() + timedelta(seconds=seconds)


def settle_dispatch(
    connection: Any,
    *,
    dispatch: dict[str, Any],
    state: str,
    worker_job_id: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return connection.execute(
        "UPDATE job_dispatches SET state = %s, selected_worker = COALESCE(%s, selected_worker), "
        "last_error = %s, retry_at = %s, updated_at = %s WHERE id = %s RETURNING *",
        (
            state,
            worker_job_id,
            error,
            dispatch_backoff(dispatch["attempts"]) if state == "UNKNOWN" else utcnow(),
            utcnow(),
            dispatch["id"],
        ),
    ).fetchone()


WORKER_STALE_SECONDS = 60


def record_worker_report(
    connection: Any,
    *,
    logical_id: str,
    report: dict[str, Any] | None,
    error: str | None = None,
) -> dict[str, Any]:
    """Store what one Worker reported, or that it could not be reached."""
    report = report or {}
    now = utcnow()
    return connection.execute(
        """
        INSERT INTO workers (
            logical_id, pool, instance_id, instance_started_at, accepting_jobs,
            intake_revision, max_concurrency, running, queued, callback_backlog,
            actions, status, detail, observed_at, reported, not_stopped
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (logical_id) DO UPDATE SET
            pool = excluded.pool,
            instance_id = excluded.instance_id,
            instance_started_at = excluded.instance_started_at,
            accepting_jobs = excluded.accepting_jobs,
            intake_revision = excluded.intake_revision,
            max_concurrency = excluded.max_concurrency,
            running = excluded.running,
            queued = excluded.queued,
            callback_backlog = excluded.callback_backlog,
            actions = excluded.actions,
            status = excluded.status,
            detail = excluded.detail,
            observed_at = excluded.observed_at,
            reported = excluded.reported,
            not_stopped = excluded.not_stopped
        RETURNING *
        """,
        (
            logical_id,
            report.get("pool"),
            report.get("instance_id"),
            report.get("instance_started_at"),
            report.get("accepting_jobs"),
            report.get("intake_revision"),
            report.get("max_concurrency"),
            report.get("running"),
            report.get("queued"),
            report.get("callback_backlog"),
            json.dumps(report.get("actions") or []),
            "HEALTHY" if report else "UNREACHABLE",
            error,
            now,
            json.dumps(report, default=str),
            int(report.get("not_stopped") or 0),
        ),
    ).fetchone()


def set_worker_intake(
    connection: Any,
    *,
    worker_id: str,
    accepting_jobs: bool,
    actor: str,
    reason: str | None,
) -> dict[str, Any]:
    """Record the intake state the Gateway wants, as a new revision."""
    now = utcnow()
    row = connection.execute(
        """
        INSERT INTO worker_overrides (
            worker_id, revision, accepting_jobs, reason, actor, created_at, updated_at
        ) VALUES (%s, 1, %s, %s, %s, %s, %s)
        ON CONFLICT (worker_id) DO UPDATE SET
            revision = worker_overrides.revision + 1,
            accepting_jobs = excluded.accepting_jobs,
            reason = excluded.reason,
            actor = excluded.actor,
            updated_at = excluded.updated_at
        RETURNING *
        """,
        (worker_id, accepting_jobs, reason, actor, now, now),
    ).fetchone()
    record_event(
        connection,
        aggregate_type="worker",
        aggregate_id=worker_id,
        aggregate_revision=row["revision"],
        type="worker.intake_requested" if not accepting_jobs else "worker.intake_resumed",
        actor=actor,
        payload={
            "accepting_jobs": accepting_jobs,
            "revision": row["revision"],
            "reason": reason,
        },
        correlation_id=worker_id,
    )
    return row


def worker_override(connection: Any, worker_id: str) -> dict[str, Any] | None:
    return connection.execute(
        "SELECT * FROM worker_overrides WHERE worker_id = %s", (worker_id,)
    ).fetchone()


def worker_inventory(connection: Any) -> dict[str, Any]:
    """The Workers as last reported, with how old each report is.

    A heartbeat that has gone quiet is reported as unknown rather than healthy:
    the Gateway cannot tell the difference between a busy Worker and a gone one
    without hearing from it.
    """
    rows = connection.execute(
        """
        SELECT w.*, o.revision AS desired_revision, o.accepting_jobs AS desired_accepting,
               o.reason AS desired_reason, o.actor AS desired_actor
          FROM workers w
          LEFT JOIN worker_overrides o ON o.worker_id = w.logical_id
         ORDER BY w.logical_id
        """
    ).fetchall()
    now = utcnow()
    workers = []
    for row in rows:
        age = (now - row["observed_at"]).total_seconds()
        stale = age > WORKER_STALE_SECONDS
        workers.append(
            {
                "logical_id": row["logical_id"],
                "pool": row["pool"],
                "instance_id": row["instance_id"],
                "instance_started_at": row["instance_started_at"].isoformat()
                if row["instance_started_at"]
                else None,
                # What the Worker said, and separately whether we still believe it.
                "reported_status": row["status"],
                "connection_status": "UNKNOWN" if stale else row["status"],
                "observed_at": row["observed_at"].isoformat(),
                "observed_age_seconds": int(age),
                "accepting_jobs": None if stale else row["accepting_jobs"],
                "intake_revision": row["intake_revision"],
                "max_concurrency": row["max_concurrency"],
                "running": row["running"],
                # Executions this Worker could not confirm it stopped. They hold a
                # slot, so the capacity it has is lower than the one it reports.
                "not_stopped": row["not_stopped"],
                # What is free now, and how much of this Worker can be used at
                # all: a running job finishes, a held slot does not.
                "free_slots": (
                    None
                    if row["max_concurrency"] is None
                    else max(
                        0,
                        int(row["max_concurrency"])
                        - int(row["running"] or 0)
                        - int(row["not_stopped"] or 0),
                    )
                ),
                "usable_slots": (
                    None
                    if row["max_concurrency"] is None
                    else max(
                        0, int(row["max_concurrency"]) - int(row["not_stopped"] or 0)
                    )
                ),
                "queued": row["queued"],
                "callback_backlog": row["callback_backlog"],
                "actions": row["actions"],
                "detail": row["detail"],
                # What the Gateway asked for, and whether the Worker has applied it.
                "desired_accepting_jobs": row["desired_accepting"],
                "desired_revision": row["desired_revision"],
                "desired_reason": row["desired_reason"],
                "desired_by": row["desired_actor"],
                "intake_applied": (
                    None
                    if row["desired_revision"] is None
                    else row["intake_revision"] == row["desired_revision"]
                ),
            }
        )
    accepting = [
        worker
        for worker in workers
        if worker["connection_status"] == "HEALTHY" and worker["accepting_jobs"]
    ]
    # A Worker that is accepting work but has no free slot cannot execute anything:
    # an execution nobody could confirm stopped is still holding it.
    usable = [
        worker
        for worker in accepting
        if worker["usable_slots"] is None or worker["usable_slots"] > 0
    ]
    held = sum(int(worker["not_stopped"] or 0) for worker in workers)
    return {
        "workers": workers,
        "accepting_count": len(accepting),
        "usable_count": len(usable),
        "not_stopped_count": held,
        # Said plainly, because a board with no Worker accepting work cannot make
        # progress however healthy everything else looks.
        "can_execute": bool(usable),
        "reason": None
        if usable
        else (
            "every accepting worker's capacity is held by an execution that could "
            "not be confirmed stopped"
            if accepting
            else (
                "no worker has reported that it is accepting jobs"
                if workers
                else "no worker has reported to this Gateway yet"
            )
        ),
    }


def note_controller(connection: Any, owner: str, leased: int = 0) -> None:
    connection.execute(
        "INSERT INTO controllers (owner, last_seen_at, leases) VALUES (%s, %s, %s) "
        "ON CONFLICT (owner) DO UPDATE SET last_seen_at = excluded.last_seen_at, "
        "leases = controllers.leases + excluded.leases",
        (owner, utcnow(), leased),
    )


def controller_available(connection: Any) -> bool:
    """Whether a Workflow Controller has evaluated Runs recently enough to rely on."""
    row = connection.execute(
        "SELECT 1 FROM controllers WHERE last_seen_at > %s LIMIT 1",
        (utcnow() - timedelta(seconds=CONTROLLER_FRESH_SECONDS),),
    ).fetchone()
    return row is not None


def start_workflow_run(
    connection: Any,
    *,
    task: dict[str, Any],
    workflow: Any,
    actor: str,
) -> dict[str, Any]:
    """Open a Controller-driven Run. The first step is proposed, not assumed."""
    now = utcnow()
    run_id = _new_id()
    entry = workflow.step(workflow.entry_step)
    run = connection.execute(
        """
        INSERT INTO workflow_runs (
            id, task_id, workflow_id, workflow_version, input_revision, status,
            orchestration_mode, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, 'ACTIVE', 'workflow-v1', %s, %s) RETURNING *
        """,
        (
            run_id,
            task["id"],
            workflow.id,
            workflow.version,
            task["input_revision"],
            now,
            now,
        ),
    ).fetchone()
    updated = _touch_task(
        connection,
        task_id=task["id"],
        status="ACTIVE",
        stage_key=entry.stage,
        active_run_id=run_id,
    )
    record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.run_started",
        actor=actor,
        payload={
            "run_id": str(run_id),
            "workflow_id": workflow.id,
            "driver": workflow.driver,
            "entry_step": workflow.entry_step,
            "stage_key": entry.stage,
        },
        correlation_id=str(task["id"]),
        occurred_at=now,
    )
    return {"task": updated, "run": run}


def attach_legacy_job(
    connection: Any,
    *,
    job: dict[str, Any],
    actor: str,
    source: str,
    title: str | None = None,
) -> dict[str, Any]:
    """Wrap a directly submitted Job in its own single-action Task.

    submit_job stays a supported route, so its Jobs get a card like any other
    request rather than living outside the board.
    """
    action = job["action"]
    payload = (job["input"] or {}).get("payload") or {}
    parameters = payload.get("parameters") or {}
    task = create_task(
        connection,
        project_id=job["project_id"],
        title=title or f"{action} の実行",
        objective=f"{action} を一回実行する",
        workflow_id=workflows.SINGLE_ACTION_V1.id,
        environment=job["environment"],
        actor=actor,
        source=source,
        acceptance_criteria=["action の出力 contract に適合する"],
        request={"action": action, "parameters": parameters, "limits": payload.get("limits") or {}},
        # A directly submitted Job keeps the existing Project projection; the
        # new Controller must never pick it up as a Workflow to advance.
        orchestration_mode="legacy",
    )
    return start_single_action_run(
        connection,
        task=task,
        action=action,
        job_id=job["id"],
        parameters=parameters,
        limits=payload.get("limits") or {},
        actor=actor,
    )


def deliverable(connection: Any, run_id: uuid.UUID, input_revision: int) -> dict[str, Any]:
    """What a decision is about: this Run's artifacts at this input revision.

    The digest is recomputed from the stored artifacts every time, so a decision
    made about one set of results cannot be applied to a different one.
    """
    rows = connection.execute(
        "SELECT id, kind, digest FROM artifacts WHERE run_id = %s ORDER BY id", (run_id,)
    ).fetchall()
    target = {
        "run_id": str(run_id),
        "input_revision": input_revision,
        "artifacts": [
            {"artifact_id": str(row["id"]), "kind": row["kind"], "digest": row["digest"]}
            for row in rows
        ],
    }
    digest, _ = canonical_digest(target)
    return {"digest": digest, "target": target}


def open_input_request(
    connection: Any,
    *,
    task: dict[str, Any],
    run_id: uuid.UUID | None,
    step_id: uuid.UUID | None,
    questions: list[dict[str, Any]],
    resume_step: str | None,
    actor: str,
) -> dict[str, Any]:
    """Ask the requester for what is missing and record where work resumes."""
    now = utcnow()
    row = connection.execute(
        """
        INSERT INTO input_requests (
            id, task_id, run_id, step_id, questions, resume_step, input_revision,
            state, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'OPEN', %s)
        RETURNING *
        """,
        (
            _new_id(),
            task["id"],
            run_id,
            step_id,
            json.dumps(questions, default=str),
            resume_step,
            task["input_revision"],
            now,
        ),
    ).fetchone()
    updated = _touch_task(connection, task_id=task["id"], status="WAITING_INPUT")
    record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.input_requested",
        actor=actor,
        payload={
            "input_request_id": str(row["id"]),
            "questions": questions,
            "resume_step": resume_step,
        },
        correlation_id=str(task["id"]),
        occurred_at=now,
    )
    return row


def answer_input_request(
    connection: Any,
    *,
    request_id: uuid.UUID,
    answers: dict[str, Any],
    actor: str,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Record an answer against the exact questions and revision it answers."""
    owner = connection.execute(
        """
        SELECT i.task_id, t.project_id FROM input_requests i
          JOIN tasks t ON t.id = i.task_id
         WHERE i.id = %s
        """,
        (request_id,),
    ).fetchone()
    if owner is None:
        raise LookupError("input request not found")
    # One lock order everywhere: project, then task, then the rows beneath it.
    connection.execute(
        "SELECT id FROM projects WHERE id = %s FOR UPDATE", (owner["project_id"],)
    ).fetchone()
    task = locked_task(connection, owner["task_id"])
    row = connection.execute(
        "SELECT * FROM input_requests WHERE id = %s FOR UPDATE", (request_id,)
    ).fetchone()
    if row["state"] not in {"OPEN", "ANSWERED"}:
        raise InvalidState("this input request is no longer open")
    if row["state"] == "ANSWERED":
        # An answer may be corrected only while nothing has been done with it. Once
        # work has been built from it, that work answers the old answer: silently
        # replacing it would leave a verification about something else.
        if row["run_id"] is None:
            raise InvalidState("this answer cannot be corrected")
        run = connection.execute(
            "SELECT status FROM workflow_runs WHERE id = %s", (row["run_id"],)
        ).fetchone()
        if run["status"] in RUN_TERMINAL:
            raise InvalidState("the run this answer belongs to has ended")
        used = connection.execute(
            """
            SELECT 1
              FROM step_attempts a
              JOIN workflow_steps s ON s.id = a.step_id
             WHERE s.run_id = %s AND a.created_at >= %s
             LIMIT 1
            """,
            (row["run_id"], row["answered_at"]),
        ).fetchone()
        if used is not None:
            raise InvalidState(
                "work has already been done with this answer; change what the task "
                "asks for instead"
            )
    if task["status"] in TASK_TERMINAL:
        raise InvalidState(f"this task is already {task['status'].lower()}")
    if expected_revision is not None and task["revision"] != expected_revision:
        raise RevisionConflict(task["revision"])
    if int(row["input_revision"]) < requirements_revision_of(
        connection, task["id"], int(task["input_revision"])
    ):
        # Asked about a version whose request has since been replaced. A revision
        # that only changed a limit does not replace it, and the question is still
        # the one the step is waiting on.
        raise InvalidState("the question was asked about an earlier input revision")
    asked = {question["id"] for question in row["questions"]}
    unknown = sorted(set(answers) - asked)
    if unknown:
        raise InvalidState(f"unknown question ids: {', '.join(unknown)}")
    unanswered = sorted(
        question["id"]
        for question in row["questions"]
        if question.get("required", True) and question["id"] not in answers
    )
    if unanswered:
        raise InvalidState(f"these questions still need an answer: {', '.join(unanswered)}")
    now = utcnow()
    answered = connection.execute(
        "UPDATE input_requests SET state = 'ANSWERED', answers = %s, answered_by = %s, "
        "answer_revision = %s, answered_at = %s WHERE id = %s RETURNING *",
        (json.dumps(answers, default=str), actor, task["input_revision"], now, request_id),
    ).fetchone()
    if row["step_id"]:
        # The question is answered, so the step that asked it is done and the Run
        # continues from where it said it would resume.
        connection.execute(
            "UPDATE workflow_steps SET status = 'SUCCEEDED', updated_at = %s WHERE id = %s",
            (now, row["step_id"]),
        )
    if row["run_id"]:
        connection.execute(
            "UPDATE workflow_runs SET status = 'ACTIVE', updated_at = %s WHERE id = %s "
            "AND status NOT IN ('COMPLETED','FAILED','CANCELLED','SUPERSEDED')",
            (now, row["run_id"]),
        )
    updated = _touch_task(connection, task_id=task["id"], status="ACTIVE")
    record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.input_answered",
        actor=actor,
        payload={
            "input_request_id": str(request_id),
            "resume_step": row["resume_step"],
            "answered": sorted(answers),
        },
        correlation_id=str(task["id"]),
        occurred_at=now,
    )
    return answered


class InvalidState(RuntimeError):
    """The requested operation does not apply to the current state."""


class ArtifactMismatch(RuntimeError):
    """The decision names a deliverable that is no longer the current one."""

    def __init__(self, current_digest: str) -> None:
        super().__init__("the deliverable changed since this decision was formed")
        self.current_digest = current_digest


def active_attempt(connection: Any, run_id: uuid.UUID) -> dict[str, Any] | None:
    return connection.execute(
        """
        SELECT a.*, s.logical_key, j.worker_job_id
          FROM step_attempts a
          JOIN workflow_steps s ON s.id = a.step_id
          LEFT JOIN jobs j ON j.id = a.job_id
         WHERE s.run_id = %s AND a.status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
         ORDER BY a.created_at DESC
         LIMIT 1
        """,
        (run_id,),
    ).fetchone()


def close_open_input_requests(
    connection: Any, *, task_id: uuid.UUID, reason: str
) -> int:
    """Close questions that will never be answered, so no answer revives a Task."""
    rows = connection.execute(
        "UPDATE input_requests SET state = 'CANCELLED', answered_at = %s "
        "WHERE task_id = %s AND state = 'OPEN' RETURNING id",
        (utcnow(), task_id),
    ).fetchall()
    for row in rows:
        record_event(
            connection,
            aggregate_type="task",
            aggregate_id=str(task_id),
            type="task.input_request_closed",
            actor="gateway",
            payload={"input_request_id": str(row["id"]), "reason": reason},
            correlation_id=str(task_id),
        )
    return len(rows)


def cancel_pending_dispatch(connection: Any, *, job_id: uuid.UUID) -> bool:
    """Stop a delivery that has not been sent. Returns whether nothing was sent.

    Only a delivery still waiting in the queue can be withdrawn: once the outcome
    is unknown the Worker may already be running it, and that has to be resolved
    with the Worker instead.
    """
    row = connection.execute(
        "UPDATE job_dispatches SET state = 'CANCELLED', updated_at = %s "
        "WHERE job_id = %s AND state = 'PENDING' RETURNING id",
        (utcnow(), job_id),
    ).fetchone()
    if row is None:
        return False
    connection.execute(
        "UPDATE jobs SET state = 'CANCELLED', result = %s, updated_at = %s "
        "WHERE id = %s AND state = 'QUEUED'",
        (json.dumps({"error": "cancelled before delivery"}), utcnow(), job_id),
    )
    return True


def terminate_attempt(
    connection: Any,
    *,
    attempt_id: uuid.UUID,
    status: str,
    failure_class: str | None,
    result: dict[str, Any],
) -> None:
    """End one Attempt and its Step without inventing a Worker report."""
    now = utcnow()
    row = connection.execute(
        "UPDATE step_attempts SET status = %s, failure_class = %s, result_summary = %s, "
        "ended_at = %s WHERE id = %s AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED') "
        "RETURNING step_id",
        (status, failure_class, json.dumps(result, default=str), now, attempt_id),
    ).fetchone()
    if row is None:
        return
    connection.execute(
        "UPDATE workflow_steps SET status = %s, updated_at = %s WHERE id = %s",
        (status, now, row["step_id"]),
    )


def settle_cancel_command(connection: Any, *, task_id: uuid.UUID, outcome: str) -> None:
    """Finish a cancel command, so its status stops saying it is being applied."""
    connection.execute(
        "UPDATE commands SET status = 'SUCCEEDED', "
        "result = coalesce(result, '{}'::jsonb) || %s, updated_at = %s "
        "WHERE target_type = 'task' AND target_id = %s AND command_type = 'cancel' "
        "AND status = 'APPLYING'",
        (
            json.dumps({"stopping": False, "outcome": outcome}),
            utcnow(),
            str(task_id),
        ),
    )


def confirm_cancellation(
    connection: Any, *, task: dict[str, Any], run_id: uuid.UUID, actor: str = "gateway"
) -> dict[str, Any] | None:
    """Complete a requested stop once no execution is still running."""
    if task["control_state"] != "CANCEL_REQUESTED":
        return None
    if active_attempt(connection, run_id) is not None:
        return None
    now = utcnow()
    connection.execute(
        "UPDATE workflow_runs SET status = 'CANCELLED', result = %s, updated_at = %s, "
        "ended_at = %s WHERE id = %s "
        "AND status NOT IN ('COMPLETED','FAILED','CANCELLED','SUPERSEDED')",
        (json.dumps({"reason": "cancel confirmed"}), now, now, run_id),
    )
    updated = _touch_task(
        connection,
        task_id=task["id"],
        status="CANCELLED",
        control_state="ACTIVE",
        clear_active_run=True,
    )
    settle_cancel_command(connection, task_id=task["id"], outcome="CANCELLED")
    record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.cancelled",
        actor=actor,
        payload={"run_id": str(run_id), "confirmed": True},
        correlation_id=str(task["id"]),
    )
    return updated



def record_decision(
    connection: Any,
    *,
    task: dict[str, Any],
    run_id: uuid.UUID,
    kind: str,
    target_digest: str,
    target: dict[str, Any],
    actor: str,
    reason: str | None,
) -> dict[str, Any]:
    return connection.execute(
        """
        INSERT INTO decisions (
            id, kind, task_id, run_id, target_digest, target, input_revision,
            state, actor, reason, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'RESOLVED', %s, %s, %s)
        RETURNING *
        """,
        (
            _new_id(),
            kind,
            task["id"],
            run_id,
            target_digest,
            json.dumps(target, default=str),
            task["input_revision"],
            actor,
            reason,
            utcnow(),
        ),
    ).fetchone()


def frozen_request(connection: Any, task: dict[str, Any]) -> dict[str, Any]:
    """The request recorded with the Task's current input revision."""
    row = connection.execute(
        "SELECT request FROM task_input_revisions WHERE task_id = %s AND revision = %s",
        (task["id"], task["input_revision"]),
    ).fetchone()
    return dict(row["request"] or {}) if row else {}


class CommandConflict(RuntimeError):
    """The same idempotency key was reused for a different request."""


def open_command(
    connection: Any,
    *,
    principal: str,
    target_type: str,
    target_id: str,
    command_type: str,
    request_hash: str,
    idempotency_key: str,
) -> tuple[dict[str, Any], bool]:
    """Register a command once. A replay returns the first command and result.

    A lost response must be recoverable by resending the same key, and must
    never produce a second execution.
    """
    existing = connection.execute(
        "SELECT * FROM commands WHERE principal_id = %s AND idempotency_key = %s FOR UPDATE",
        (principal, idempotency_key),
    ).fetchone()
    if existing is not None:
        if existing["request_hash"] != request_hash:
            raise CommandConflict("idempotency key already used with a different request")
        return existing, False
    now = utcnow()
    row = connection.execute(
        """
        INSERT INTO commands (
            id, principal_id, target_type, target_id, command_type, request_hash,
            idempotency_key, status, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'ACCEPTED', %s, %s)
        RETURNING *
        """,
        (
            _new_id(),
            principal,
            target_type,
            target_id,
            command_type,
            request_hash,
            idempotency_key,
            now,
            now,
        ),
    ).fetchone()
    return row, True


def finish_command(
    connection: Any, *, command_id: uuid.UUID, status: str, result: dict[str, Any]
) -> dict[str, Any]:
    return connection.execute(
        "UPDATE commands SET status = %s, result = %s, updated_at = %s WHERE id = %s "
        "RETURNING *",
        (status, json.dumps(result, default=str), utcnow(), command_id),
    ).fetchone()


def get_command(connection: Any, command_id: uuid.UUID) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM commands WHERE id = %s", (command_id,)
    ).fetchone()
    return None if row is None else public_command(row)


def public_command(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "command_id": str(row["id"]),
        "type": row["command_type"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "status": row["status"],
        "result": row["result"],
        "status_url": f"/v1/commands/{row['id']}",
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def add_message(
    connection: Any,
    *,
    task_id: uuid.UUID,
    kind: str,
    body: str,
    author: str,
    applies_to: str,
    input_revision: int | None,
) -> dict[str, Any]:
    now = utcnow()
    row = connection.execute(
        """
        INSERT INTO task_messages (
            id, task_id, kind, body, author, applies_to, input_revision, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (_new_id(), task_id, kind, body, author, applies_to, input_revision, now),
    ).fetchone()
    if applies_to != "note_only":
        # An instruction for the next attempt changes what the Task asks for, so
        # a proposal built before it cannot be applied afterwards unnoticed.
        _touch_task(connection, task_id=task_id)
    record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task_id),
        type="task.message_added",
        actor=author,
        payload={"message_id": str(row["id"]), "kind": kind, "applies_to": applies_to},
        correlation_id=str(task_id),
        occurred_at=now,
    )
    return row


def current_input(connection: Any, task: dict[str, Any]) -> dict[str, Any]:
    """The input revision the Task is working from."""
    row = connection.execute(
        "SELECT * FROM task_input_revisions WHERE task_id = %s AND revision = %s",
        (task["id"], task["input_revision"]),
    ).fetchone()
    if row is None:
        raise LookupError("the task has no recorded input")
    return row


def requirements_revision_of(connection: Any, task_id: Any, input_revision: int) -> int:
    """The last version at or before this one that changed what is asked for.

    A revision that only changed a limit or added a reference asks for the same work,
    so work, specifications and instructions from before it still answer the request.
    Everything that decides "does this still answer what is being asked?" compares
    against this rather than against the revision number.
    """
    row = connection.execute(
        "SELECT requirements_revision FROM task_input_revisions "
        " WHERE task_id = %s AND revision = %s",
        (task_id, input_revision),
    ).fetchone()
    return int((row or {}).get("requirements_revision") or input_revision)


def consolidates_instructions(
    previous: dict[str, Any],
    *,
    objective: str | None,
    acceptance_criteria: list[str] | None,
) -> bool:
    """Whether this revision takes what was asked in instructions into the request.

    Rewriting the objective or the conditions does that: what an instruction asked
    for is now part of what the Task asks for, so it is no longer outstanding.
    Re-sending either of them unchanged — while changing a limit, say — does not:
    treating that as consolidation would mark an instruction as acted on when
    nothing has acted on it, and no step would ever be told.
    """
    if objective is not None and objective.strip() != (previous["objective"] or "").strip():
        return True
    return acceptance_criteria is not None and list(acceptance_criteria) != list(
        previous["acceptance_criteria"] or []
    )


def add_input_revision(
    connection: Any,
    *,
    task: dict[str, Any],
    actor: str,
    objective: str | None = None,
    acceptance_criteria: list[str] | None = None,
    context_refs: list[str] | None = None,
    parameters: dict[str, Any] | None = None,
    limits: dict[str, int] | None = None,
    origin: str,
    reason: str = "",
) -> dict[str, Any]:
    """Record a new, immutable version of what the Task asks for.

    Everything not given is carried over, so the new version is complete on its own.
    The version also records when what is asked for last changed
    (`requirements_revision`): verification recorded before that stays in the ledger
    but is no longer evidence about what is being asked for now, while a version that
    only changed a limit leaves earlier work and verification standing.
    """
    previous = current_input(connection, task)
    now = utcnow()
    revision = int(task["input_revision"]) + 1
    # Whether this version changes what is asked for, or only how it is run. Only the
    # former supersedes what a person said about the previous version. An instruction
    # that says the work must be redone is itself a new requirement — its text is the
    # change — so it counts whatever else it leaves alone.
    changes_requirements = (
        origin != "operator"
        or (objective is not None and objective.strip() != (previous["objective"] or "").strip())
        or (
            acceptance_criteria is not None
            and list(acceptance_criteria) != list(previous["acceptance_criteria"] or [])
        )
        or (
            parameters is not None
            and parameters != (previous["request"] or {}).get("parameters")
        )
    )
    carried = ""
    consolidating = consolidates_instructions(
        previous, objective=objective, acceptance_criteria=acceptance_criteria
    )
    # What the previous version asked for beyond its objective — a restart
    # instruction, the reason a change was requested — is carried into this one so it
    # is not left behind in the message history where the next step may not see it.
    if (previous["reason"] or "").strip() and (
        # A version that asks for the same work carries it whether or not something
        # has already answered it: otherwise extending a limit would drop a
        # requirement the next step, or the verification, still has to be told about.
        not changes_requirements
        # A version that rewrites the objective or the conditions has taken that
        # instruction into the request itself, so it is not still pending; and an
        # instruction something has already acted on is part of that work now.
        or (
            not consolidating
            and not _work_done_for(
                connection, task_id=task["id"], input_revision=int(task["input_revision"])
            )
        )
    ):
        carried = previous["reason"].strip()
    reason = "\n\n".join(part for part in (reason.strip(), carried) if part)
    new_objective = previous["objective"] if objective is None else objective
    criteria = (
        previous["acceptance_criteria"]
        if acceptance_criteria is None
        else list(acceptance_criteria)
    )
    refs = previous["context_refs"] if context_refs is None else list(context_refs)
    request = dict(previous["request"] or {})
    if parameters is not None:
        # What is actually executed for this version. Copying the previous
        # parameters would run the old request under a new revision number.
        request["parameters"] = parameters
    if limits is not None:
        request["limits"] = limits
    requirements_revision = (
        revision
        if changes_requirements
        else int(previous["requirements_revision"] or previous["revision"])
    )
    connection.execute(
        """
        INSERT INTO task_input_revisions (
            task_id, revision, objective, acceptance_criteria, context_refs,
            request, reason, requirements_revision, created_by, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            task["id"],
            revision,
            new_objective,
            json.dumps(list(criteria)),
            json.dumps(list(refs)),
            json.dumps(request, default=str),
            reason,
            requirements_revision,
            actor,
            now,
        ),
    )
    connection.execute(
        "UPDATE tasks SET input_revision = %s, objective = %s, updated_at = %s "
        "WHERE id = %s",
        (revision, new_objective, now, task["id"]),
    )
    if task["active_run_id"]:
        # The Run continues under this version, so attempts created from here record
        # it. Whether earlier attempts still answer the request is decided by
        # `requirements_revision`, not by this number.
        connection.execute(
            "UPDATE workflow_runs SET input_revision = %s, updated_at = %s WHERE id = %s",
            (revision, now, task["active_run_id"]),
        )
    updated = _touch_task(connection, task_id=task["id"])
    record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.input_revised",
        actor=actor,
        payload={
            "input_revision": revision,
            "origin": origin,
            "reason": reason,
            "objective_changed": objective is not None
            and objective != previous["objective"],
            "criteria_changed": acceptance_criteria is not None
            and list(criteria) != list(previous["acceptance_criteria"]),
            "parameters_changed": parameters is not None
            and parameters != (previous["request"] or {}).get("parameters"),
        },
        correlation_id=str(task["id"]),
        occurred_at=now,
    )
    return {"task": updated, "input_revision": revision}


def consume_instructions(connection: Any, *, task_id: uuid.UUID, by: str) -> int:
    """Mark the outstanding instructions as given to something.

    An attempt is built from them, or the requester has just put them into the
    objective or the acceptance criteria. Either way they are no longer waiting:
    keeping them outstanding forever would eventually leave no room for anything
    else in a handoff.
    """
    rows = connection.execute(
        # Unconsumed, or consumed by an attempt that ended without delivering: those
        # became outstanding again, so what is given them now is their consumer.
        "UPDATE task_messages m SET consumed_at = %s, consumed_by = %s "
        "WHERE m.task_id = %s AND m.applies_to <> 'note_only' "
        "AND (m.consumed_at IS NULL OR EXISTS ("
        "     SELECT 1 FROM step_attempts a "
        "      WHERE a.id::text = m.consumed_by AND a.status IN ('FAILED', 'CANCELLED')"
        ")) "
        "RETURNING m.id",
        (utcnow(), by, task_id),
    ).fetchall()
    return len(rows)


def _work_done_for(connection: Any, *, task_id: uuid.UUID, input_revision: int) -> bool:
    """Whether an execution has completed for this version of the request."""
    row = connection.execute(
        """
        SELECT 1
          FROM step_attempts a
          JOIN workflow_steps s ON s.id = a.step_id
          JOIN workflow_runs r ON r.id = s.run_id
         WHERE r.task_id = %s
           AND a.status = 'SUCCEEDED'
           AND s.kind = 'action'
           AND (a.input_manifest->>'input_revision')::int = %s
         LIMIT 1
        """,
        (task_id, input_revision),
    ).fetchone()
    return row is not None


def update_metadata(
    connection: Any,
    *,
    task_id: uuid.UUID,
    actor: str,
    title: str | None = None,
    priority: str | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Change permitted presentation metadata. Never a state transition."""
    task = locked_task(connection, task_id)
    if expected_revision is not None and task["revision"] != expected_revision:
        raise RevisionConflict(task["revision"])
    changed = {}
    if title is not None and title != task["title"]:
        changed["title"] = title
    if priority is not None and priority != task["priority"]:
        changed["priority"] = priority
    if not changed:
        return task
    updated = _touch_task(
        connection,
        task_id=task_id,
        title=changed.get("title"),
        priority=changed.get("priority"),
    )
    record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task_id),
        aggregate_revision=updated["revision"],
        type="task.metadata_updated",
        actor=actor,
        payload={"changed": changed},
        correlation_id=str(task_id),
    )
    return updated


class RevisionConflict(RuntimeError):
    def __init__(self, current_revision: int) -> None:
        super().__init__("task was updated by another operation")
        self.current_revision = current_revision


def locked_task(connection: Any, task_id: uuid.UUID) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM tasks WHERE id = %s FOR UPDATE", (task_id,)
    ).fetchone()
    if row is None:
        raise LookupError("task not found")
    return row


def _touch_task(
    connection: Any,
    *,
    task_id: uuid.UUID,
    status: str | None = None,
    stage_key: str | None = None,
    control_state: str | None = None,
    active_run_id: uuid.UUID | None = None,
    clear_active_run: bool = False,
    title: str | None = None,
    priority: str | None = None,
) -> dict[str, Any]:
    """Apply a state change and bump the revision used for conflict detection."""
    return connection.execute(
        """
        UPDATE tasks
           SET status = COALESCE(%s, status),
               stage_key = COALESCE(%s, stage_key),
               control_state = COALESCE(%s, control_state),
               active_run_id = CASE WHEN %s THEN NULL ELSE COALESCE(%s, active_run_id) END,
               title = COALESCE(%s, title),
               priority = COALESCE(%s, priority),
               revision = revision + 1,
               updated_at = %s
         WHERE id = %s
        RETURNING *
        """,
        (
            status,
            stage_key,
            control_state,
            clear_active_run,
            active_run_id,
            title,
            priority,
            utcnow(),
            task_id,
        ),
    ).fetchone()


# --------------------------------------------------------------------------- #
# Worker event projection
# --------------------------------------------------------------------------- #

_ATTEMPT_STATUS = {
    "accepted": "RUNNING",
    "started": "RUNNING",
    "progress": "RUNNING",
    "completed": "SUCCEEDED",
    "failed": "FAILED",
}

# The minimum output an action must return for its Step to count as delivered,
# matching what the executors actually produce. Storing a contract name proves
# nothing, so the Gateway re-checks the result it accepts.
@dataclass(frozen=True)
class OutputContract:
    # Where the output lives in the callback: a key, or the payload itself.
    key: str | None
    required: tuple[str, ...] = ()


OUTPUT_CONTRACTS: dict[str, OutputContract] = {
    "product.plan": OutputContract("report", ("title", "problem", "acceptance_criteria")),
    "qa.review": OutputContract("report", ("verdict", "summary", "acceptance_criteria")),
    "growth.plan": OutputContract(
        "report", ("summary", "observations", "recommended_actions")
    ),
    # The build executor returns its evidence at the top level, not under a report.
    "code.build": OutputContract(None, ("mode", "succeeded", "changed_files")),
    "code.fix": OutputContract(None, ("mode", "succeeded", "changed_files")),
    "video.generate": OutputContract("artifact", ("sha256", "bytes")),
}
# The limits an execution actually applies. A request may not carry any other
# limit: keeping one that nothing enforces would record a bound that does not
# exist, and the requester would believe the work was constrained by it.
EXECUTION_LIMITS = ("timeout_seconds", "max_output_bytes")

# A capability probe is the whole output of a diagnostic request.
DIAGNOSTIC_CONTRACT = OutputContract(None, ("mode", "executor", "commands"))
DEFAULT_CONTRACT = OutputContract("report")

# Verifying work may only report a pass with per-criterion verdicts and a
# reference to what was verified. Anything less stays inconclusive.
QA_ACTIONS = ("qa.review", "test.run")
CRITERION_KEYS = ("criteria", "checks", "acceptance_criteria")
TARGET_KEYS = ("target_digest", "target_artifact_id", "source_worker_job_id")

# A code change is only identified by its patch and workspace, never by a
# base commit or a free-form report.
CODE_CHANGE_DIGEST_KEYS = ("patch_digest", "workspace_digest")


def _contract_for(attempt: dict[str, Any]) -> OutputContract:
    requested = (attempt.get("input_manifest") or {}).get("parameters") or {}
    if requested.get("operation") == "self_test":
        return DIAGNOSTIC_CONTRACT
    return OUTPUT_CONTRACTS.get(attempt.get("action") or "", DEFAULT_CONTRACT)


def _accepted_output(
    attempt: dict[str, Any], data: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    contract = _contract_for(attempt)
    output = data if contract.key is None else data.get(contract.key)
    if not isinstance(output, dict) or not output:
        where = contract.key or "the result payload"
        return None, f"required output '{where}' is missing or empty"
    missing = [key for key in contract.required if key not in output]
    if missing:
        return None, f"output is missing required fields: {', '.join(missing)}"
    return output, None


def criterion_text(value: Any) -> str:
    """One criterion as its identity: the text itself, with layout normalised.

    Line breaks and runs of spaces are formatting, so they are collapsed. Case is
    not formatting — `userID` and `userid` can be two different requirements — so a
    verdict about one is not a verdict about the other.
    """
    return " ".join(str(value or "").split())


# Where each action's own input states the conditions the work is judged by. The
# planning action calls them constraints; the others carry them as acceptance
# criteria. For an action that has no such field the Task's conditions are still
# recorded and still required of a verification, but the request cannot state them.
CRITERIA_PARAMETER = {
    "code.build": "acceptance_criteria",
    "code.fix": "acceptance_criteria",
    "qa.review": "acceptance_criteria",
    "product.plan": "constraints",
}


def parameters_with_task_criteria(
    action: str, parameters: dict[str, Any] | None, criteria: list[Any] | None
) -> dict[str, Any]:
    """The action's parameters, told what the Task is judged by.

    A Task's acceptance criteria are what a person asked for. An action that can be
    given them has to be, or the work — and the verification of it — answers less
    than the request: a single action would otherwise be judged only by whatever the
    parameters happened to repeat, and a plan would be written without the
    constraints the requester set.
    """
    field = CRITERIA_PARAMETER.get(action)
    if field is None or not criteria:
        return dict(parameters or {})
    merged = list(_listed((parameters or {}).get(field)))
    seen = {criterion_text(item) for item in merged}
    for item in criteria:
        if criterion_text(item) not in seen:
            merged.append(item)
            seen.add(criterion_text(item))
    return {**(parameters or {}), field: merged}


def _required_criteria(attempt: dict[str, Any]) -> set[str]:
    """Everything this attempt was asked to judge, from every source it was given."""
    manifest = attempt.get("input_manifest") or {}
    sources = (
        _listed(manifest.get("acceptance_criteria")),
        _listed((manifest.get("parameters") or {}).get("acceptance_criteria")),
        _listed((manifest.get("verification_target") or {}).get("criteria")),
    )
    return {
        criterion_text(item)
        for source in sources
        for item in source
        if str(item).strip()
    }


def _listed(value: Any) -> list[Any]:
    """Whatever this field holds, read as a list of items or as nothing.

    A report is written by an agent. A field that should be a list and is not says
    nothing about the criteria, and must not be able to stop the projection: the
    callback would then be retried for ever.
    """
    return value if isinstance(value, list) else []


def _unresolved(data: dict[str, Any]) -> bool:
    """Whether this progress report says the execution is not resolved.

    Two different things reach here as progress: an execution whose end could not be
    established (`stopped: false`) and one that ended whose recorded outcome cannot
    be read yet (`outcome_pending: true`). Neither is work progressing, and treating
    either as progress would clear a Task that is still waiting to be resolved.
    """
    return data.get("stopped") is False or data.get("outcome_pending") is True


def _quality(
    connection: Any,
    attempt: dict[str, Any],
    job: dict[str, Any],
    output: dict[str, Any] | None,
) -> dict[str, Any]:
    """Separate what the verifier reported from what its evidence supports."""
    action = attempt.get("action")
    if action not in QA_ACTIONS:
        return {}
    report = output or {}
    reported = report.get("verdict")
    if not isinstance(reported, str):
        # Whatever this is, it is not a verdict. Comparing it against the ones that
        # exist must not be able to stop the projection: the callback would then be
        # delivered again for ever.
        reported = None
    criteria_verdicts = [
        (item or {}).get("verdict")
        for key in CRITERION_KEYS
        for item in _listed(report.get(key))
        if isinstance(item, dict)
    ]
    if "fail" in criteria_verdicts:
        # A criterion that failed is decisive, whatever the overall verdict says.
        return {
            "reported_verdict": reported,
            "quality_verdict": "fail",
            "downgraded_reason": "an acceptance criterion did not pass",
        }
    if reported is None or reported not in {"pass", "fail", "inconclusive"}:
        return {
            "reported_verdict": reported,
            "quality_verdict": "inconclusive",
            "downgraded_reason": "no recognised verdict in the report",
        }
    if reported != "pass":
        return {"reported_verdict": reported, "quality_verdict": reported}

    listed = [
        item
        for key in CRITERION_KEYS
        for item in _listed(report.get(key))
    ]
    # Every list the report used, not only the first one with entries: a second list
    # holding an unfinished judgement is part of what this report says.
    criteria = listed or None
    verdicts = [
        item.get("verdict")
        for item in _listed(criteria)
        if isinstance(item, dict)
    ]
    failed = [verdict for verdict in verdicts if verdict and verdict != "pass"]
    if failed:
        # A criterion that did not pass is decisive: the overall pass cannot
        # stand over it, whatever else the report is missing.
        return {
            "reported_verdict": "pass",
            "quality_verdict": "fail" if "fail" in failed else "inconclusive",
            "downgraded_reason": (
                f"{len(failed)} of {len(verdicts)} acceptance criteria did not pass"
            ),
        }
    missing: list[str] = []
    if not criteria or len(verdicts) != len(criteria) or not all(verdicts):
        missing.append("per-criterion verdicts")
    if not all(
        str((item or {}).get("evidence") or "").strip()
        for item in _listed(criteria)
        if isinstance(item, dict)
    ):
        missing.append("the evidence behind each verdict")
    judged = {
        criterion_text((item or {}).get("criterion"))
        for item in _listed(criteria)
        if isinstance(item, dict)
    }
    unjudged = sorted(_required_criteria(attempt) - judged)
    if unjudged:
        missing.append(f"a verdict for {', '.join(unjudged)}")
    target_error = _resolve_target(connection, attempt, job, report)
    if target_error:
        missing.append(target_error)
    verified_error = _verified_change_error(connection, attempt, report)
    if verified_error:
        missing.append(verified_error)
    elif not ((attempt.get("input_manifest") or {}).get("verification_target") or {}):
        # Nothing binds this verification to a change this Gateway recorded, so
        # there is nothing its pass can be checked against. The report stays as
        # history; it does not authorise anything.
        missing.append("a change this Gateway recorded to verify")
    if missing:
        return {
            "reported_verdict": "pass",
            # A pass nobody can check is not a pass.
            "quality_verdict": "inconclusive",
            "downgraded_reason": "a reported pass is missing " + " and ".join(missing),
        }
    return {"reported_verdict": "pass", "quality_verdict": "pass"}


def _verified_change_error(
    connection: Any, attempt: dict[str, Any], report: dict[str, Any]
) -> str | None:
    """Check what the verifier says it rebuilt against what the Gateway recorded.

    The Worker rebuilds the change from the patch and states the patch it used. The
    Gateway holds that patch's digest in the artifact it bound this verification to,
    so the two must agree: otherwise a pass could be about a local copy that was
    replaced after the change was recorded.
    """
    bound = (attempt.get("input_manifest") or {}).get("verification_target") or {}
    artifact_id = bound.get("artifact_id")
    if not artifact_id:
        return None
    row = connection.execute(
        "SELECT manifest FROM artifacts WHERE id = %s", (uuid.UUID(str(artifact_id)),)
    ).fetchone()
    recorded = ((row or {}).get("manifest") or {}).get("content") or {}
    expected = recorded.get("patch_digest")
    if not expected:
        # A deliverable with no patch cannot be rebuilt, so nothing can establish what
        # a pass about it would be about. Keeping the report as history is fine;
        # treating it as verified work is not.
        return "a change identified by the patch that produces it"
    verified = report.get("verified_change") or {}
    if not isinstance(verified, dict):
        verified = {}
    claimed = verified.get("patch_digest")
    if not isinstance(claimed, str) or not claimed:
        # The Worker rebuilds the change from its patch and says which patch that
        # was. Without it, nothing establishes that this pass is about the change the
        # Gateway recorded rather than about whatever was on a disk somewhere.
        return "a statement of the change it rebuilt and verified"
    if expected != claimed:
        return "a verification of the change this Gateway recorded"
    expected_base = recorded.get("base_commit")
    if not expected_base:
        # A change identified by a patch but recorded without the commit it applies
        # to cannot be verified: there is nothing to check the verifier's base
        # against, and the same patch on another base is different work.
        return "a recorded base for the change this verification is about"
    claimed_base = verified.get("base_commit")
    if not isinstance(claimed_base, str) or not claimed_base:
        # A patch says what changed, not what it changed, so a pass has to say which
        # base it was verified against.
        return "a statement of the base this change was verified against"
    if claimed_base != expected_base:
        return "a verification against the base this Gateway recorded"
    return None


def _resolve_target(
    connection: Any,
    attempt: dict[str, Any],
    job: dict[str, Any],
    report: dict[str, Any],
) -> str | None:
    """Check that the report names something real that was actually verified.

    Each accepted form must resolve on its own: a 64-character string that no
    artifact of this project carries is not evidence, and an execution named by
    the report must be the one this verification was given.
    """
    requested_source = (
        (attempt.get("input_manifest") or {}).get("parameters") or {}
    ).get("source_worker_job_id")

    bound = (attempt.get("input_manifest") or {}).get("verification_target") or {}
    if bound:
        # The Gateway recorded which artifact this verification is of, so the
        # report has to be about that one.
        reported_source = report.get("source_worker_job_id")
        if reported_source and reported_source != bound.get("source_worker_job_id"):
            return "the execution this verification was given"
        digest = report.get("target_digest")
        artifact_id = report.get("target_artifact_id")
        if digest and digest != bound.get("digest"):
            return "the change this verification was given"
        if artifact_id and str(artifact_id) != bound.get("artifact_id"):
            return "the change this verification was given"
        if not (reported_source or digest or artifact_id):
            # A report that names nothing could have been produced anywhere,
            # including by an earlier execution whose workspace this one copied.
            # The Worker records the execution it verified, so a report without
            # one is not evidence about this change.
            return "a reference to the execution or change it verified"
        return None

    given = {
        str(item.get("digest")): str(item.get("artifact_id"))
        for item in ((attempt.get("input_manifest") or {}).get("input_artifacts") or [])
        if isinstance(item, dict)
    }

    digest = report.get("target_digest")
    if isinstance(digest, str) and len(digest) == 64:
        if given:
            # The verification was handed specific artifacts; its target has to
            # be one of them.
            return None if digest in given else "the artifact this verification was given"
        known = connection.execute(
            "SELECT 1 FROM artifacts a JOIN workflow_steps s ON s.run_id = a.run_id "
            "JOIN step_attempts att ON att.step_id = s.id "
            "WHERE a.digest = %s AND a.project_id = %s AND att.id = %s LIMIT 1",
            (digest, job["project_id"], attempt["id"]),
        ).fetchone()
        if known:
            return None
        return "a target produced by this run"

    artifact_id = report.get("target_artifact_id")
    if artifact_id:
        try:
            parsed = uuid.UUID(str(artifact_id))
        except ValueError:
            return "a resolvable verified target"
        if given:
            return (
                None
                if str(parsed) in given.values()
                else "the artifact this verification was given"
            )
        owned = connection.execute(
            "SELECT 1 FROM artifacts WHERE id = %s AND run_id = %s",
            (parsed, attempt["run_id"]),
        ).fetchone()
        if owned:
            return None
        return "a verified target from this run"

    reported_source = report.get("source_worker_job_id")
    if isinstance(reported_source, str) and reported_source:
        if requested_source and reported_source != requested_source:
            # The report names a different execution than the one it was asked
            # to verify.
            return "the execution this verification was given"
        return _resolve_execution(connection, reported_source, job["project_id"])
    if isinstance(requested_source, str) and requested_source:
        return _resolve_execution(connection, requested_source, job["project_id"])
    return "a resolvable verified target"


def _resolve_execution(connection: Any, worker_job_id: str, project_id: str) -> str | None:
    owner = connection.execute(
        "SELECT project_id FROM jobs WHERE worker_job_id = %s "
        "ORDER BY created_at DESC LIMIT 1",
        (worker_job_id,),
    ).fetchone()
    if owner is None:
        return "a known verified execution"
    if owner["project_id"] != project_id:
        return "a verified target in this project"
    return None


def _locked_attempt(connection: Any, attempt_id: uuid.UUID) -> dict[str, Any] | None:
    """Lock the Task before its Attempt, matching the platform lock order.

    The Attempt's owning Task never changes, so reading the link without a lock
    and then locking Task before Attempt is safe, and keeps callbacks in the
    same order as the command path.
    """
    link = connection.execute(
        """
        SELECT a.id, r.task_id
          FROM step_attempts a
          JOIN workflow_steps s ON s.id = a.step_id
          JOIN workflow_runs r ON r.id = s.run_id
         WHERE a.id = %s
        """,
        (attempt_id,),
    ).fetchone()
    if link is None:
        return None
    locked_task(connection, link["task_id"])
    return connection.execute(
        """
        SELECT a.*, s.id AS step_id, s.run_id, s.logical_key, s.stage_key, s.action,
               r.task_id, r.workflow_id, r.orchestration_mode, r.input_revision
          FROM step_attempts a
          JOIN workflow_steps s ON s.id = a.step_id
          JOIN workflow_runs r ON r.id = s.run_id
         WHERE a.id = %s
         FOR UPDATE OF a
        """,
        (attempt_id,),
    ).fetchone()


def project_worker_event(
    connection: Any,
    *,
    job: dict[str, Any],
    event_type: str,
    data: dict[str, Any],
    occurred_at: datetime,
    actor: str = "worker",
) -> dict[str, Any] | None:
    """Carry one Worker event into the ledger for the Job's own Attempt.

    Keyed on the Attempt, so a second Task in the same Project cannot have its
    progress or artifacts overwritten by another Task's callback.
    """
    attempt_id = job.get("attempt_id")
    if attempt_id is None:
        return None  # A pre-ledger Job: legacy Project projection still applies.
    attempt = _locked_attempt(connection, attempt_id)
    if attempt is None:
        return None
    if attempt["status"] in ATTEMPT_TERMINAL:
        # A late callback for a finished Attempt is evidence, not a state change.
        return None

    now = utcnow()
    attempt_status = _ATTEMPT_STATUS[event_type]
    output: dict[str, Any] | None = None
    contract_error: str | None = None
    quality: dict[str, Any] = {}
    artifact = None
    if event_type == "completed":
        output, contract_error = _accepted_output(attempt, data)
        if contract_error is not None:
            # The execution ended, but its required output never arrived: the
            # Step has not delivered, and no completion may be claimed.
            attempt_status = "FAILED"
        else:
            quality = _quality(connection, attempt, job, output)
            artifact = _record_artifact(
                connection,
                attempt=attempt,
                job=job,
                output=output,
                quality=quality,
                now=now,
            )
    terminal = attempt_status in ATTEMPT_TERMINAL
    failure_class = None
    cancelled = event_type == "failed" and bool(data.get("cancelled"))
    interrupted = event_type == "failed" and bool(data.get("interrupted"))
    if cancelled:
        # Stopping on request is not the same outcome as failing.
        attempt_status = "CANCELLED"
        failure_class = "cancelled_by_request"
    elif interrupted:
        failure_class = "interrupted_before_completion"
    elif event_type == "failed":
        failure_class = "worker_reported_failure"
    elif contract_error is not None:
        failure_class = "output_contract_unsatisfied"
    terminal = attempt_status in ATTEMPT_TERMINAL

    connection.execute(
        """
        UPDATE step_attempts
           SET status = %s,
               failure_class = %s,
               result_summary = %s,
               started_at = COALESCE(started_at, %s),
               ended_at = CASE WHEN %s THEN %s ELSE ended_at END
         WHERE id = %s
        """,
        (
            attempt_status,
            failure_class,
            json.dumps(
                {
                    # What the execution did and what it delivered are reported
                    # separately, so neither can stand in for the other.
                    "job_state": {
                        "completed": "SUCCEEDED",
                        "failed": "FAILED_FINAL",
                    }.get(event_type, "RUNNING"),
                    "artifact_id": str(artifact["id"]) if artifact else None,
                    "contract_error": contract_error,
                    "error": data.get("error")
                    if event_type == "failed" or _unresolved(data)
                    else None,
                    # An execution whose end could not be established: the Attempt
                    # stays open and says so.
                    "stopped": False if data.get("stopped") is False else None,
                    # An execution that ended and whose outcome the Worker cannot
                    # read yet. Nothing about this Attempt is settled either.
                    "outcome_pending": True
                    if data.get("outcome_pending") is True
                    else None,
                    "cancelled": cancelled or None,
                    "interrupted": interrupted or None,
                    **quality,
                },
                default=str,
            ),
            occurred_at,
            terminal,
            now,
            attempt["id"],
        ),
    )
    connection.execute(
        "UPDATE workflow_steps SET status = %s, updated_at = %s WHERE id = %s",
        (attempt_status if terminal else "RUNNING", now, attempt["step_id"]),
    )

    task = connection.execute(
        "SELECT * FROM tasks WHERE id = %s", (attempt["task_id"],)
    ).fetchone()
    task_status = task["status"]
    stage_key = attempt["stage_key"]
    run_status = None
    paused_before_completion = (
        terminal
        and attempt_status == "SUCCEEDED"
        and task["control_state"] == "PAUSE_REQUESTED"
    )
    if (
        terminal
        and attempt["workflow_id"] == workflows.SINGLE_ACTION_V1.id
        and not paused_before_completion
    ):
        # A single action completes its Run: its output contract is the whole
        # completion condition, and nothing else is waiting behind it.
        run_status = {
            "SUCCEEDED": "COMPLETED",
            "CANCELLED": "CANCELLED",
            "FAILED": "FAILED",
        }[attempt_status]
        task_status = run_status
        stage_key = "done" if attempt_status == "SUCCEEDED" else attempt["stage_key"]
    if run_status:
        connection.execute(
            "UPDATE workflow_runs SET status = %s, result = %s, updated_at = %s, ended_at = %s "
            "WHERE id = %s",
            (
                run_status,
                json.dumps(
                    {
                        "artifact_id": str(artifact["id"]) if artifact else None,
                        "contract_error": contract_error,
                        **quality,
                    },
                    default=str,
                ),
                now,
                now,
                attempt["run_id"],
            ),
        )
    # The Worker reports what it could not resolve as progress: `stopped: false` when
    # it could not establish that an execution stopped, `outcome_pending: true` when
    # the execution ended and what it produced cannot be read yet. Those are the
    # non-terminal events that change what may be concluded about the Attempt.
    unstopped = not terminal and _unresolved(data)
    if unstopped:
        # Nothing may be concluded about this Attempt until the Worker resolves it or
        # someone looks, so the Task reports that it is being checked rather than
        # that work is progressing.
        task_status = "BLOCKED"
    elif not terminal and task_status == "BLOCKED":
        # The Worker is reporting progress, so the outcome is no longer unknown.
        task_status = "ACTIVE"
    control_state = None
    if terminal and task["control_state"] == "PAUSE_REQUESTED" and not run_status:
        # The work that was already running has ended, so the pause is complete.
        # Completion of a paused Run is decided on resume, not here.
        control_state = "PAUSED"
    updated = _touch_task(
        connection,
        task_id=task["id"],
        status=task_status,
        stage_key=stage_key,
        control_state=control_state,
        clear_active_run=bool(run_status),
    )
    record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type=f"task.attempt_{event_type}",
        actor=actor,
        payload={
            "run_id": str(attempt["run_id"]),
            "step_key": attempt["logical_key"],
            "attempt_id": str(attempt["id"]),
            "job_id": str(job["id"]),
            "attempt_status": attempt_status,
            "task_status": updated["status"],
            "artifact_id": str(artifact["id"]) if artifact else None,
            "contract_error": contract_error,
            **quality,
        },
        correlation_id=str(task["id"]),
        causation_id=str(job["id"]),
        occurred_at=occurred_at,
    )
    if terminal and not run_status:
        # A stop that was requested becomes a stop that happened only here, once
        # nothing is running any more.
        confirmed = confirm_cancellation(
            connection, task=updated, run_id=attempt["run_id"]
        )
        updated = confirmed or updated
    elif terminal and run_status and task["control_state"] == "CANCEL_REQUESTED":
        # The Run ended on its own while a stop was pending: record how it
        # actually ended rather than leaving the Task stuck as "stopping".
        updated = _touch_task(connection, task_id=task["id"], control_state="ACTIVE")
        settle_cancel_command(
            connection, task_id=task["id"], outcome=updated["status"]
        )
    elif terminal and run_status and task["control_state"] == "PAUSE_REQUESTED":
        # The Run ended before the pause could take effect. Nothing is being
        # held, so the Task does not keep reporting a pause that has nothing to
        # pause — otherwise it would offer both resume and retry.
        updated = _touch_task(connection, task_id=task["id"], control_state="ACTIVE")
        record_event(
            connection,
            aggregate_type="task",
            aggregate_id=str(task["id"]),
            aggregate_revision=updated["revision"],
            type="task.pause_settled",
            actor=actor,
            payload={
                "run_id": str(attempt["run_id"]),
                "outcome": updated["status"],
                "reason": "the run ended before the pause took effect",
            },
            correlation_id=str(task["id"]),
            causation_id=str(job["id"]),
        )
    return {
        "task": updated,
        "artifact": artifact,
        "contract_error": contract_error,
        **quality,
    }


def _dispatch_refused_after_acceptance(
    connection: Any, *, job: dict[str, Any], reason: str, actor: str
) -> dict[str, Any] | None:
    """Record a refused delivery for work the Worker has already taken.

    The execution is real: it reported for duty, or it has an id here. Ending the
    Attempt on the strength of a refused retry would report a failure that did not
    happen and settle a stop that nobody performed. The Task is marked as needing
    checking instead, and the reason is kept.
    """
    row = connection.execute(
        """
        SELECT r.task_id, a.status AS attempt_status
          FROM step_attempts a
          JOIN workflow_steps s ON s.id = a.step_id
          JOIN workflow_runs r ON r.id = s.run_id
         WHERE a.id = %s
        """,
        (job["attempt_id"],),
    ).fetchone()
    if row is None:
        return None
    task = locked_task(connection, row["task_id"])
    if task["status"] in TASK_TERMINAL or row["attempt_status"] in ATTEMPT_TERMINAL:
        # The execution this delivery was about has already ended and been recorded,
        # or the Task itself is finished. A refused retry afterwards is a delivery
        # problem in the past: it must not move a Task that is waiting for a decision
        # or getting on with the next step, and it cannot reopen a finished one — but
        # it is recorded either way, because it happened.
        record_event(
            connection,
            aggregate_type="task",
            aggregate_id=str(task["id"]),
            aggregate_revision=task["revision"],
            type="task.dispatch_refused_after_completion",
            actor=actor,
            payload={
                "reason": reason,
                "job_id": str(job["id"]),
                "worker_job_id": job.get("worker_job_id"),
                "attempt_status": row["attempt_status"],
                "task_status": task["status"],
            },
            correlation_id=str(task["id"]),
            causation_id=str(job["id"]),
        )
        return None
    updated = _touch_task(connection, task_id=task["id"], status="BLOCKED")
    record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.blocked",
        actor=actor,
        payload={
            "reason": (
                "a delivery was refused for work the worker had already accepted: "
                + reason
            ),
            "job_id": str(job["id"]),
            "worker_job_id": job.get("worker_job_id"),
            "stage": "dispatch",
            "input_revision": task["input_revision"],
        },
        correlation_id=str(task["id"]),
        causation_id=str(job["id"]),
    )
    return updated


def project_dispatch_outcome(
    connection: Any,
    *,
    job: dict[str, Any],
    definitive: bool,
    reason: str,
    actor: str = "gateway",
) -> dict[str, Any] | None:
    """Report a Gateway-side delivery result on the Task the Job belongs to.

    A refusal the Gateway made itself ends the Attempt. A delivery whose outcome
    is unknown must not: the work may already be running, so the card says so
    instead of showing a failure nobody verified.

    A refusal of a *later* delivery says nothing about an execution that was already
    accepted — a lost response and then a rotated credential is enough to produce
    one — so where there is evidence of an accepted execution, the delivery problem
    is recorded and the Attempt is left alone.
    """
    if job.get("attempt_id") is None:
        return None
    if definitive and (job.get("worker_job_id") or job.get("state") == "RUNNING"):
        return _dispatch_refused_after_acceptance(
            connection, job=job, reason=reason, actor=actor
        )
    if not definitive:
        return project_blocked(
            connection,
            job=job,
            reason=reason,
            detail={"stage": "dispatch"},
            actor=actor,
        )
    attempt = _locked_attempt(connection, job["attempt_id"])
    if attempt is None or attempt["status"] in ATTEMPT_TERMINAL:
        return None
    now = utcnow()
    connection.execute(
        "UPDATE step_attempts SET status = 'FAILED', failure_class = 'dispatch_rejected', "
        "result_summary = %s, ended_at = %s WHERE id = %s",
        (
            json.dumps({"job_state": "FAILED_FINAL", "error": reason}),
            now,
            attempt["id"],
        ),
    )
    connection.execute(
        "UPDATE workflow_steps SET status = 'FAILED', updated_at = %s WHERE id = %s",
        (now, attempt["step_id"]),
    )
    connection.execute(
        "UPDATE workflow_runs SET status = 'FAILED', result = %s, updated_at = %s, "
        "ended_at = %s WHERE id = %s "
        "AND status NOT IN ('COMPLETED','FAILED','CANCELLED','SUPERSEDED')",
        (json.dumps({"error": reason}), now, now, attempt["run_id"]),
    )
    task = locked_task(connection, attempt["task_id"])
    if task["status"] in TASK_TERMINAL:
        return None
    stopping = task["control_state"] == "CANCEL_REQUESTED"
    pausing = task["control_state"] in {"PAUSE_REQUESTED", "PAUSED"}
    updated = _touch_task(
        connection,
        task_id=task["id"],
        status="FAILED",
        # Neither a pending stop nor a pending pause survives the end of the work
        # they were about: a finished Task that still says "pausing" offers no
        # coherent operation at all.
        control_state="ACTIVE" if stopping or pausing else None,
        clear_active_run=True,
    )
    if stopping:
        # The stop was pending and the work ended anyway: record how it actually
        # ended instead of leaving the Task and its command as "stopping".
        settle_cancel_command(connection, task_id=task["id"], outcome=updated["status"])
    if pausing:
        record_event(
            connection,
            aggregate_type="task",
            aggregate_id=str(task["id"]),
            aggregate_revision=updated["revision"],
            type="task.pause_settled",
            actor=actor,
            payload={
                "job_id": str(job["id"]),
                "outcome": updated["status"],
                "reason": "the delivery was refused before the pause took effect",
            },
            correlation_id=str(task["id"]),
            causation_id=str(job["id"]),
        )
    record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.dispatch_failed",
        actor=actor,
        payload={"job_id": str(job["id"]), "reason": reason},
        correlation_id=str(task["id"]),
        causation_id=str(job["id"]),
    )
    return updated


def project_blocked(
    connection: Any,
    *,
    job: dict[str, Any],
    reason: str,
    detail: dict[str, Any] | None = None,
    actor: str = "gateway",
) -> dict[str, Any] | None:
    """Mark a Task blocked while its execution outcome is unknown.

    The Attempt stays non-terminal on purpose: an unresolved submission is not
    evidence that the work stopped, so the card reports "確認中" instead of a
    result nobody verified.
    """
    attempt_id = job.get("attempt_id")
    if attempt_id is None:
        return None
    row = connection.execute(
        """
        SELECT r.task_id, a.status AS attempt_status
          FROM step_attempts a
          JOIN workflow_steps s ON s.id = a.step_id
          JOIN workflow_runs r ON r.id = s.run_id
         WHERE a.id = %s
        """,
        (attempt_id,),
    ).fetchone()
    if row is None:
        return None
    task = locked_task(connection, row["task_id"])
    if task["status"] in TASK_TERMINAL or row["attempt_status"] in ATTEMPT_TERMINAL:
        # This execution has already ended and been recorded, or the Task is finished;
        # a delivery question about it afterwards is history, not something the Task
        # is waiting on — but it is kept, because someone reading the feed should see
        # it happened.
        record_event(
            connection,
            aggregate_type="task",
            aggregate_id=str(task["id"]),
            aggregate_revision=task["revision"],
            type="task.dispatch_refused_after_completion",
            actor=actor,
            payload={
                "reason": reason,
                "job_id": str(job["id"]),
                "worker_job_id": job.get("worker_job_id"),
                "attempt_status": row["attempt_status"],
                "task_status": task["status"],
            },
            correlation_id=str(task["id"]),
            causation_id=str(job["id"]),
        )
        return None
    updated = _touch_task(connection, task_id=task["id"], status="BLOCKED")
    record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.blocked",
        actor=actor,
        payload={"reason": reason, "job_id": str(job["id"]), **(detail or {})},
        correlation_id=str(task["id"]),
        causation_id=str(job["id"]),
    )
    return updated


def _artifact_kind(action: str | None, output: dict[str, Any]) -> str:
    kind = ARTIFACT_KINDS.get(action or "", "action-output")
    if kind == "code-change" and not all(
        output.get(key) for key in CODE_CHANGE_DIGEST_KEYS
    ):
        # Without patch and workspace digests this is a report about a change,
        # not a manifest that identifies one.
        return "code-change-report"
    return kind


def _record_artifact(
    connection: Any,
    *,
    attempt: dict[str, Any],
    job: dict[str, Any],
    output: dict[str, Any],
    quality: dict[str, Any],
    now: datetime,
) -> dict[str, Any] | None:
    storage_ref: dict[str, Any] = {
        "worker_job_id": job.get("worker_job_id"),
        "gateway_job_id": str(job["id"]),
    }
    kind = _artifact_kind(attempt["action"], output)
    media_type = "application/json"
    expires_at = None
    # Prefer the digest and size the producer verified over a hash of its
    # description, so the recorded identity is the artifact's own.
    digest = output.get("sha256") if isinstance(output.get("sha256"), str) else None
    size = output.get("bytes") if isinstance(output.get("bytes"), int) else None
    if attempt["action"] == "video.generate":
        media_type = output.get("media_type") or "video/mp4"
        storage_ref["path"] = output.get("path") or output.get("url")
        expires_at = now + timedelta(days=int(output.get("retention_days") or 7))
    if digest is None:
        digest, size = canonical_digest(output)
        media_type = "application/json"
    return connection.execute(
        """
        INSERT INTO artifacts (
            id, project_id, task_id, run_id, producer_attempt_id, kind, media_type,
            size, digest, input_revision, storage_ref, manifest, created_at, expires_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (producer_attempt_id, kind) DO NOTHING
        RETURNING *
        """,
        (
            _new_id(),
            job["project_id"],
            attempt["task_id"],
            attempt["run_id"],
            attempt["id"],
            kind,
            media_type,
            size,
            digest,
            attempt["input_revision"],
            json.dumps(storage_ref, default=str),
            json.dumps(
                {
                    "action": attempt["action"],
                    "produced_by_step": attempt["logical_key"],
                    "content": output,
                    **quality,
                },
                default=str,
            ),
            now,
            expires_at,
        ),
    ).fetchone()

def public_task(row: dict[str, Any], *, attention: bool | None = None) -> dict[str, Any]:
    return {
        "task_id": str(row["id"]),
        "display_number": row["display_number"],
        "project_id": row["project_id"],
        "title": row["title"],
        "objective": row["objective"],
        "status": row["status"],
        "stage_key": row["stage_key"],
        "control_state": row["control_state"],
        "workflow_id": row["workflow_id"],
        "orchestration_mode": row["orchestration_mode"],
        "environment": row["environment"],
        "active_run_id": str(row["active_run_id"]) if row["active_run_id"] else None,
        "input_revision": row["input_revision"],
        "priority": row["priority"],
        "revision": row["revision"],
        "created_by": row["created_by"],
        "source": row["source"],
        "needs_attention": row["status"] in ATTENTION_STATUSES if attention is None else attention,
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def list_tasks(
    connection: Any,
    *,
    project_id: str | None = None,
    statuses: Iterable[str] | None = None,
    stage_key: str | None = None,
    attention_only: bool = False,
    query: str | None = None,
    include_terminal: bool = True,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    clauses: list[str] = []
    values: list[Any] = []
    if project_id:
        clauses.append("project_id = %s")
        values.append(project_id)
    status_list = list(statuses or [])
    if status_list:
        clauses.append("status = ANY(%s)")
        values.append(status_list)
    if stage_key:
        clauses.append("stage_key = %s")
        values.append(stage_key)
    if attention_only:
        clauses.append("status = ANY(%s)")
        values.append(list(ATTENTION_STATUSES))
    if not include_terminal:
        clauses.append("status <> ALL(%s)")
        values.append(list(TASK_TERMINAL))
    if query:
        clauses.append("(title ILIKE %s OR objective ILIKE %s)")
        values.extend([f"%{query}%"] * 2)
    if cursor:
        created_at, cursor_id = _decode_cursor(cursor)
        clauses.append("(created_at, id) < (%s, %s)")
        values.extend([created_at, cursor_id])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = connection.execute(
        f"SELECT * FROM tasks {where} ORDER BY created_at DESC, id DESC LIMIT %s",
        (*values, limit + 1),
    ).fetchall()
    has_more = len(rows) > limit
    page = rows[:limit]
    stage_counts = {
        row["stage_key"]: row["n"]
        for row in connection.execute(
            "SELECT stage_key, count(*) AS n FROM tasks GROUP BY stage_key"
        ).fetchall()
    }
    return {
        "tasks": [public_task(row) for row in page],
        "next_cursor": _encode_cursor(page[-1]) if has_more and page else None,
        "has_more": has_more,
        "stage_counts": stage_counts,
    }


def _encode_cursor(row: dict[str, Any]) -> str:
    return f"{row['created_at'].isoformat()}|{row['id']}"


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        created_at, cursor_id = cursor.split("|", 1)
        return datetime.fromisoformat(created_at), uuid.UUID(cursor_id)
    except ValueError as error:
        raise ValueError("cursor is not a valid listing position") from error


def get_task(connection: Any, task_id: uuid.UUID) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM tasks WHERE id = %s", (task_id,)).fetchone()
    if row is None:
        return None
    return {
        **public_task(row),
        "input_revisions": [
            {
                "revision": item["revision"],
                "objective": item["objective"],
                "acceptance_criteria": item["acceptance_criteria"],
                "context_refs": item["context_refs"],
                # What this version executes, and why it exists: an operator
                # changing the request needs to see both.
                "action": (item["request"] or {}).get("action"),
                "parameters": (item["request"] or {}).get("parameters") or {},
                "limits": (item["request"] or {}).get("limits") or {},
                "reason": item["reason"],
                "created_by": item["created_by"],
                "created_at": item["created_at"].isoformat(),
            }
            for item in connection.execute(
                "SELECT * FROM task_input_revisions WHERE task_id = %s ORDER BY revision",
                (task_id,),
            ).fetchall()
        ],
        "runs": _runs(connection, task_id),
        "input_requests": [
            {
                "input_request_id": str(item["id"]),
                "state": item["state"],
                "questions": item["questions"],
                "resume_step": item["resume_step"],
                "input_revision": item["input_revision"],
                "answers": item["answers"],
                "answered_by": item["answered_by"],
                "created_at": item["created_at"].isoformat(),
                "answered_at": item["answered_at"].isoformat()
                if item["answered_at"]
                else None,
            }
            for item in connection.execute(
                "SELECT * FROM input_requests WHERE task_id = %s ORDER BY created_at",
                (task_id,),
            ).fetchall()
        ],
        "decisions": [
            {
                "decision_id": str(item["id"]),
                "kind": item["kind"],
                "target_digest": item["target_digest"],
                "actor": item["actor"],
                "reason": item["reason"],
                "input_revision": item["input_revision"],
                "created_at": item["created_at"].isoformat(),
            }
            for item in connection.execute(
                "SELECT * FROM decisions WHERE task_id = %s ORDER BY created_at",
                (task_id,),
            ).fetchall()
        ],
        "artifacts": [_public_artifact(item, include_content=False) for item in connection.execute(
            "SELECT * FROM artifacts WHERE task_id = %s ORDER BY created_at DESC", (task_id,)
        ).fetchall()],
        "messages": [
            {
                "message_id": str(item["id"]),
                "kind": item["kind"],
                "body": item["body"],
                "author": item["author"],
                "applies_to": item["applies_to"],
                "input_revision": item["input_revision"],
                "created_at": item["created_at"].isoformat(),
            }
            for item in connection.execute(
                "SELECT * FROM task_messages WHERE task_id = %s ORDER BY created_at", (task_id,)
            ).fetchall()
        ],
    }


def _runs(connection: Any, task_id: uuid.UUID) -> list[dict[str, Any]]:
    runs = connection.execute(
        "SELECT * FROM workflow_runs WHERE task_id = %s ORDER BY created_at", (task_id,)
    ).fetchall()
    if not runs:
        return []
    steps = connection.execute(
        "SELECT * FROM workflow_steps WHERE run_id = ANY(%s) ORDER BY position, cycle",
        ([run["id"] for run in runs],),
    ).fetchall()
    attempts = connection.execute(
        """
        SELECT a.*, j.state AS job_state
          FROM step_attempts a
          LEFT JOIN jobs j ON j.id = a.job_id
         WHERE a.step_id = ANY(%s)
         ORDER BY a.attempt_number
        """,
        ([step["id"] for step in steps] or [None],),
    ).fetchall()
    by_step: dict[Any, list[dict[str, Any]]] = {}
    for attempt in attempts:
        by_step.setdefault(attempt["step_id"], []).append(
            {
                "attempt_id": str(attempt["id"]),
                "attempt_number": attempt["attempt_number"],
                "status": attempt["status"],
                "job_id": str(attempt["job_id"]) if attempt["job_id"] else None,
                "job_state": attempt["job_state"],
                "failure_class": attempt["failure_class"],
                "input_manifest": attempt["input_manifest"],
                "execution_snapshot": attempt["execution_snapshot"],
                "result_summary": attempt["result_summary"],
                "created_at": attempt["created_at"].isoformat(),
                "started_at": attempt["started_at"].isoformat() if attempt["started_at"] else None,
                "ended_at": attempt["ended_at"].isoformat() if attempt["ended_at"] else None,
            }
        )
    by_run: dict[Any, list[dict[str, Any]]] = {}
    for step in steps:
        by_run.setdefault(step["run_id"], []).append(
            {
                "step_id": str(step["id"]),
                "logical_key": step["logical_key"],
                "cycle": step["cycle"],
                "stage_key": step["stage_key"],
                "kind": step["kind"],
                "action": step["action"],
                "agent_binding": step["agent_binding"],
                "output_contract": step["output_contract"],
                "status": step["status"],
                "attempts": by_step.get(step["id"], []),
            }
        )
    return [
        {
            "run_id": str(run["id"]),
            "workflow_id": run["workflow_id"],
            "workflow_version": run["workflow_version"],
            "input_revision": run["input_revision"],
            "status": run["status"],
            "orchestration_mode": run["orchestration_mode"],
            "revision_cycles": run["revision_cycles"],
            "result": run["result"],
            "created_at": run["created_at"].isoformat(),
            "ended_at": run["ended_at"].isoformat() if run["ended_at"] else None,
            "steps": by_run.get(run["id"], []),
        }
        for run in runs
    ]


def get_runs(connection: Any, task_id: uuid.UUID) -> list[dict[str, Any]]:
    return _runs(connection, task_id)


def _public_artifact(row: dict[str, Any], *, include_content: bool) -> dict[str, Any]:
    manifest = dict(row["manifest"] or {})
    content = manifest.pop("content", None)
    artifact = {
        "artifact_id": str(row["id"]),
        "project_id": row["project_id"],
        "task_id": str(row["task_id"]) if row["task_id"] else None,
        "run_id": str(row["run_id"]) if row["run_id"] else None,
        "producer_attempt_id": str(row["producer_attempt_id"])
        if row["producer_attempt_id"]
        else None,
        "kind": row["kind"],
        "media_type": row["media_type"],
        "size": row["size"],
        "digest": row["digest"],
        "input_revision": row["input_revision"],
        "storage_ref": row["storage_ref"],
        "manifest": manifest,
        "created_at": row["created_at"].isoformat(),
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
    }
    if include_content:
        artifact["content"] = content
    return artifact


def get_artifact(
    connection: Any, artifact_id: uuid.UUID, *, include_content: bool = False
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM artifacts WHERE id = %s", (artifact_id,)
    ).fetchone()
    if row is None:
        return None
    return _public_artifact(row, include_content=include_content)


def project_summary(connection: Any, project_id: str) -> dict[str, Any]:
    """Aggregate a Project's Tasks instead of letting the newest callback win."""
    counts = connection.execute(
        """
        SELECT
            count(*) FILTER (WHERE status <> ALL(%s)) AS active_task_count,
            count(*) FILTER (WHERE status = ANY(%s)) AS attention_count,
            count(*) AS task_count
          FROM tasks WHERE project_id = %s
        """,
        (list(TASK_TERMINAL), list(ATTENTION_STATUSES), project_id),
    ).fetchone()
    return {
        "task_count": counts["task_count"],
        "active_task_count": counts["active_task_count"],
        "attention_count": counts["attention_count"],
    }


def snapshot(connection: Any, *, limit: int = 200) -> dict[str, Any]:
    """A consistent read plus the cursor to resume the feed from.

    Read inside one repeatable-read transaction so the cursor cannot be newer
    than the rows returned with it.
    """
    listing = list_tasks(connection, limit=limit)
    last = connection.execute(
        "SELECT last_cursor FROM event_counter WHERE stream_id = %s", (EVENT_STREAM,)
    ).fetchone()["last_cursor"]
    return {
        "cursor": last,
        "tasks": listing["tasks"],
        # A snapshot larger than the page must be continued with next_cursor
        # before the consumer follows the feed from `cursor`.
        "next_cursor": listing["next_cursor"],
        "has_more": listing["has_more"],
        "stage_counts": listing["stage_counts"],
        "projects": project_views(connection),
        "generated_at": utcnow().isoformat(),
    }


def record_project_event(
    connection: Any,
    *,
    project_id: str,
    event_type: str,
    payload: dict[str, Any],
    actor: str = "gateway",
) -> int:
    """Publish a Project change on the shared feed.

    A consumer that rebuilds from snapshot plus feed must learn about projects,
    candidates and approvals too, not only Tasks.
    """
    return record_event(
        connection,
        aggregate_type="project",
        aggregate_id=project_id,
        type=f"project.{event_type}" if not event_type.startswith("project.") else event_type,
        actor=actor,
        payload=payload,
        correlation_id=project_id,
    )


def project_views(connection: Any) -> list[dict[str, Any]]:
    """Every Project with its Task aggregate, including ones with no Task yet."""
    rows = connection.execute(
        """
        SELECT p.id AS project_id, p.title, p.state, p.repository_url, p.production_url,
               (p.release_candidate IS NOT NULL) AS has_release_candidate,
               p.updated_at,
               count(t.id) AS task_count,
               count(t.id) FILTER (WHERE t.status <> ALL(%s)) AS active_task_count,
               count(t.id) FILTER (WHERE t.status = ANY(%s)) AS attention_count
          FROM projects p
          LEFT JOIN tasks t ON t.project_id = p.id
         GROUP BY p.id
         ORDER BY p.id
        """,
        (list(TASK_TERMINAL), list(ATTENTION_STATUSES)),
    ).fetchall()
    views = [
        {**dict(row), "updated_at": row["updated_at"].isoformat()} for row in rows
    ]
    # Tasks may reference a project the Gateway does not own a row for; report
    # them rather than dropping them from the projection.
    orphans = connection.execute(
        """
        SELECT t.project_id,
               count(*) AS task_count,
               count(*) FILTER (WHERE t.status <> ALL(%s)) AS active_task_count,
               count(*) FILTER (WHERE t.status = ANY(%s)) AS attention_count
          FROM tasks t
          LEFT JOIN projects p ON p.id = t.project_id
         WHERE p.id IS NULL
         GROUP BY t.project_id
         ORDER BY t.project_id
        """,
        (list(TASK_TERMINAL), list(ATTENTION_STATUSES)),
    ).fetchall()
    views.extend(
        {
            "project_id": row["project_id"],
            "title": None,
            "state": "NOT_REGISTERED",
            "repository_url": None,
            "production_url": None,
            "has_release_candidate": False,
            "updated_at": None,
            "task_count": row["task_count"],
            "active_task_count": row["active_task_count"],
            "attention_count": row["attention_count"],
        }
        for row in orphans
    )
    return views
