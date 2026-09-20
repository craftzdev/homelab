"""Internal API for the Agent Workflow Controller.

Separate from the public surface and behind its own credential: the Controller
may advance work, which Grok and the browser may not. The Controller proposes;
this module lets the Gateway verify the lease, the expected revision and the
transition before anything is applied.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app import runs, tasks
from app.task_api import error


class LeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = Field(min_length=1, max_length=100)
    limit: int = Field(default=5, ge=1, le=20)
    lease_seconds: int = Field(default=runs.LEASE_SECONDS, ge=5, le=runs.MAX_LEASE_SECONDS)


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = Field(min_length=1, max_length=100)
    token: int = Field(ge=1)
    lease_seconds: int = Field(default=runs.LEASE_SECONDS, ge=5, le=runs.MAX_LEASE_SECONDS)


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: int = Field(ge=1)
    type: Literal[
        "create_attempt", "request_review", "request_input", "complete_run", "fail_run"
    ]
    step_key: str | None = Field(default=None, max_length=100)
    cycle: int | None = Field(default=None, ge=1)
    # Required: a proposal is built from a Run state, and applying it to a
    # different one would act on work the Controller has not seen.
    expected_task_revision: int = Field(ge=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, int] = Field(default_factory=dict)
    input_artifact_ids: list[str] = Field(default_factory=list, max_length=20)
    questions: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    feedback: dict[str, Any] | None = None


class BlockedReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)
    detail: dict[str, Any] = Field(default_factory=dict)


def build_router(
    *,
    pool: Any,
    auth: Any,
    job_factory: Callable[..., dict[str, Any]],
    actor: str,
) -> APIRouter:
    router = APIRouter(prefix="/internal/v1", dependencies=[Depends(auth)])

    @router.post("/runs/lease")
    def lease(request: LeaseRequest) -> dict[str, Any]:
        with pool.connection() as connection:
            leased = runs.lease_runs(
                connection,
                owner=request.owner,
                limit=request.limit,
                lease_seconds=request.lease_seconds,
            )
            # Recorded so the Gateway can say whether a Controller is actually
            # evaluating Runs before it lets one be started.
            tasks.note_controller(connection, request.owner, len(leased))
            connection.commit()
        return {"runs": leased, "leased": len(leased)}

    @router.get("/runs/{run_id}")
    def get_run(run_id: uuid.UUID) -> dict[str, Any]:
        with pool.connection() as connection:
            try:
                return runs.run_state(connection, run_id)
            except LookupError as missing:
                raise error(
                    "NOT_FOUND", "run not found", http_status=status.HTTP_404_NOT_FOUND
                ) from missing

    @router.get("/artifacts/{artifact_id}")
    def get_artifact(artifact_id: uuid.UUID) -> dict[str, Any]:
        """Read one artifact with its content, so a handoff can quote it."""
        with pool.connection() as connection:
            artifact = tasks.get_artifact(connection, artifact_id, include_content=True)
        if artifact is None:
            raise error(
                "NOT_FOUND", "artifact not found", http_status=status.HTTP_404_NOT_FOUND
            )
        return artifact

    @router.post("/runs/{run_id}/heartbeat")
    def heartbeat(run_id: uuid.UUID, request: HeartbeatRequest) -> dict[str, Any]:
        with pool.connection() as connection:
            try:
                renewed = runs.renew_lease(
                    connection,
                    run_id=run_id,
                    token=request.token,
                    lease_seconds=request.lease_seconds,
                )
            except LookupError as missing:
                raise error(
                    "NOT_FOUND", "run not found", http_status=status.HTTP_404_NOT_FOUND
                ) from missing
            except runs.LeaseRejected as rejected:
                raise error("LEASE_LOST", str(rejected)) from rejected
            tasks.note_controller(connection, request.owner)
            connection.commit()
        return renewed

    @router.post("/runs/{run_id}/blocked", status_code=status.HTTP_202_ACCEPTED)
    def blocked(run_id: uuid.UUID, request: BlockedReport) -> dict[str, Any]:
        """Record that this Run cannot be advanced with what it contains.

        The Controller cannot invent the input a step needs, and a Task that is
        waiting for a person must say so instead of looking active.
        """
        with pool.connection() as connection:
            try:
                recorded = runs.report_blocked(
                    connection,
                    run_id=run_id,
                    token=request.token,
                    reason=request.reason,
                    detail=request.detail,
                    actor=actor,
                )
            except LookupError as missing:
                raise error(
                    "NOT_FOUND", "run not found", http_status=status.HTTP_404_NOT_FOUND
                ) from missing
            except runs.LeaseRejected as rejected:
                raise error("LEASE_LOST", str(rejected)) from rejected
            connection.commit()
        return recorded

    @router.post("/runs/{run_id}/proposals", status_code=status.HTTP_202_ACCEPTED)
    def propose(run_id: uuid.UUID, request: Proposal) -> dict[str, Any]:
        applied: dict[str, Any]
        with pool.connection() as connection:
            # Same lock order as every other writer: project, then task, then rows.
            owner = connection.execute(
                """
                SELECT t.project_id FROM workflow_runs r JOIN tasks t ON t.id = r.task_id
                 WHERE r.id = %s
                """,
                (run_id,),
            ).fetchone()
            if owner is None:
                raise error(
                    "NOT_FOUND", "run not found", http_status=status.HTTP_404_NOT_FOUND
                )
            connection.execute(
                "SELECT id FROM projects WHERE id = %s FOR UPDATE", (owner["project_id"],)
            ).fetchone()
            try:
                applied = runs.apply_proposal(
                    connection,
                    run_id=run_id,
                    token=request.token,
                    proposal=request.model_dump(mode="json"),
                    actor=actor,
                    job_factory=job_factory,
                )
            except runs.LeaseRejected as rejected:
                raise error("LEASE_LOST", str(rejected)) from rejected
            except runs.ProposalRejected as rejected:
                raise error("TRANSITION_NOT_ALLOWED", str(rejected)) from rejected
            except tasks.InvalidState as invalid:
                raise error("INVALID_STATE", str(invalid)) from invalid
            except HTTPException:
                raise
            connection.commit()
        # The attempt's Job was registered and queued for delivery in the same
        # commit, so there is no separate dispatch step to lose here.
        return applied

    return router
