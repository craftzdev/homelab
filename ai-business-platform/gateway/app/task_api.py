"""REST and MCP surface for the Task ledger.

Kept apart from the Job/approval module so the single-module Gateway can grow
the Task contract without the existing execution paths moving underneath it.
The router is built with the caller's authentication dependency and a narrow
bridge into Job submission, so this module never reaches into Job internals.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import runs, tasks, workflows


# A criterion is carried whole into every step that is judged against it, and
# the action schemas cap one at 500 characters. Accepting a longer one would only
# fail later, at dispatch, where the requester cannot act on it.
MAX_CRITERION_CHARS = 500


# A reference is a pointer — a URL, an id, a path — and it is carried whole.
MAX_REFERENCE_CHARS = 500


def check_references(items: list[str]) -> list[str]:
    too_long = [item for item in items if len(item) > MAX_REFERENCE_CHARS]
    if too_long:
        raise ValueError(
            f"a reference of {len(too_long[0])} characters exceeds the "
            f"{MAX_REFERENCE_CHARS} a step can carry"
        )
    return items


def check_criteria(items: list[str]) -> list[str]:
    too_long = [item for item in items if len(item) > MAX_CRITERION_CHARS]
    if too_long:
        raise ValueError(
            f"an acceptance criterion of {len(too_long[0])} characters exceeds the "
            f"{MAX_CRITERION_CHARS} a step can carry"
        )
    return items


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9._-]+$")
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=8000)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=20)
    context_refs: list[str] = Field(default_factory=list, max_length=20)
    workflow_id: str = Field(default=workflows.SINGLE_ACTION_V1.id, max_length=100)
    environment: Literal["research", "preview"] = "preview"
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    action: str | None = Field(default=None, max_length=100)
    parameters: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, int] = Field(default_factory=dict)
    start: bool = False

    @model_validator(mode="after")
    def criteria_must_be_carryable(self):
        check_criteria(self.acceptance_criteria)
        check_references(self.context_refs)
        if self.context_refs and self.workflow_id == workflows.SINGLE_ACTION_V1.id:
            # A single action is executed from its parameters; a reference nothing
            # reads would be recorded and then ignored.
            raise ValueError(
                "single-action-v1 executes its parameters; put the references the "
                "action needs in parameters"
            )
        if self.parameters and self.workflow_id != workflows.SINGLE_ACTION_V1.id:
            # The mirror of the rule above: a workflow's steps are built from the
            # objective, the conditions and what people say about it. Parameters
            # nothing reads would be recorded and then ignored — and an instruction
            # written there would reach no step at all.
            raise ValueError(
                f"{self.workflow_id} builds its steps from the objective and the "
                "acceptance criteria; put what the work must do there"
            )
        return self

    @model_validator(mode="after")
    def limits_must_be_enforced(self):
        unknown = sorted(set(self.limits) - set(tasks.EXECUTION_LIMITS))
        if unknown:
            raise ValueError(
                f"these limits are not enforced by any execution: {', '.join(unknown)}; "
                f"only {', '.join(tasks.EXECUTION_LIMITS)} apply"
            )
        return self

    @model_validator(mode="after")
    def single_action_needs_an_action(self):
        if self.workflow_id == workflows.SINGLE_ACTION_V1.id and self.action is None:
            raise ValueError("single-action-v1 requires the action to run")
        if self.workflow_id != workflows.SINGLE_ACTION_V1.id and self.action is not None:
            raise ValueError("action is only accepted for single-action-v1")
        return self


class TaskMetadataUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=200)
    priority: Literal["low", "normal", "high", "urgent"] | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class TaskCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal[
        "start",
        "pause",
        "resume",
        "cancel",
        "retry",
        "request_changes",
        "accept_deliverable",
        "revise_input",
    ]
    expected_revision: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=2000)
    # Decisions name the deliverable they are about; the Gateway recomputes it.
    target_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # What the Task asks for, changed. Only `revise_input` carries these.
    objective: str | None = Field(default=None, min_length=1, max_length=8000)
    acceptance_criteria: list[str] | None = Field(default=None, max_length=20)
    context_refs: list[str] | None = Field(default=None, max_length=20)
    # What a single action executes. Its objective describes the request; these
    # are what actually runs.
    parameters: dict[str, Any] | None = None
    limits: dict[str, int] | None = None

    @model_validator(mode="after")
    def input_fields_belong_to_a_revision(self):
        given = {
            name
            for name in (
                "objective",
                "acceptance_criteria",
                "context_refs",
                "parameters",
                "limits",
            )
            if getattr(self, name) is not None
        }
        if self.type != "revise_input":
            if given:
                raise ValueError(
                    f"{', '.join(sorted(given))} can only be changed with revise_input"
                )
            return self
        if not given:
            raise ValueError(
                "revise_input needs the objective, the acceptance criteria or the "
                "context references it changes"
            )
        if self.acceptance_criteria is not None:
            check_criteria(self.acceptance_criteria)
        if self.context_refs is not None:
            check_references(self.context_refs)
        if self.limits is not None:
            unknown = sorted(set(self.limits) - set(tasks.EXECUTION_LIMITS))
            if unknown:
                raise ValueError(
                    f"these limits are not enforced by any execution: {', '.join(unknown)}"
                )
        return self

    @model_validator(mode="after")
    def decisions_need_their_target(self):
        if self.type in {"request_changes", "accept_deliverable"} and not self.target_digest:
            raise ValueError("a decision must name the deliverable digest it is about")
        if self.type == "request_changes" and not (self.reason or "").strip():
            raise ValueError("asking for changes requires the reason")
        return self


class WorkerCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Draining and resuming intake. Changing a Worker's capabilities or image is
    # a configuration change, not a runtime command.
    type: Literal["drain", "resume"]
    reason: str | None = Field(default=None, max_length=500)


# An answer is carried whole into the step that asked for it, so it has to be
# something a step can carry. Accepting more and shortening it later would answer
# the question with half a sentence.
MAX_ANSWER_CHARS = 4_000
MAX_ANSWERS_CHARS = 12_000


class InputAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: dict[str, Any] = Field(min_length=1)
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def answers_must_be_carryable(self):
        rendered = {key: str(value) for key, value in self.answers.items()}
        too_long = sorted(
            key for key, value in rendered.items() if len(value) > MAX_ANSWER_CHARS
        )
        if too_long:
            raise ValueError(
                f"these answers are longer than the {MAX_ANSWER_CHARS} characters a "
                f"step can carry: {', '.join(too_long)}"
            )
        total = sum(len(value) for value in rendered.values())
        if total > MAX_ANSWERS_CHARS:
            raise ValueError(
                f"the answers total {total} characters, more than the "
                f"{MAX_ANSWERS_CHARS} one step can be given at once"
            )
        return self


class TaskMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=8000)
    kind: Literal["comment", "instruction"] = "comment"
    applies_to: Literal["note_only", "next_attempt", "restart_required"] = "note_only"


@dataclass(frozen=True)
class JobBridge:
    """The only coupling to Job execution: build, insert, dispatch."""

    build_request: Callable[..., Any]
    # Registering a Job also queues its delivery, so there is no separate
    # dispatch step that could be lost between the two.
    insert: Callable[..., tuple[dict[str, Any], bool]]
    actions: tuple[str, ...]


def error(
    code: str,
    message: str,
    *,
    http_status: int = status.HTTP_409_CONFLICT,
    field_errors: dict[str, str] | None = None,
    current_revision: int | None = None,
    retryable: bool = False,
    correlation_id: str | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=http_status,
        detail={
            "code": code,
            "message": message,
            "field_errors": field_errors or {},
            "current_revision": current_revision,
            "retryable": retryable,
            "correlation_id": correlation_id,
        },
    )


# Deciding about a deliverable is a human act. These commands need the separate
# human credential, so the shared service token a bot uses cannot accept work.
HUMAN_ONLY_COMMANDS = ("accept_deliverable", "request_changes")


def build_router(
    *,
    pool: Any,
    auth: Any,
    jobs: JobBridge,
    actor_resolver: Callable[[], tuple[str, str]],
    human_principal: Callable[[str | None], str],
) -> APIRouter:
    """Create the /v1 Task router.

    `actor_resolver` returns the (principal, source) pair the surface itself
    proves. A client-declared source is never trusted: a bot cannot label its
    own request as human operation.
    """
    router = APIRouter(prefix="/v1", dependencies=[Depends(auth)])

    @router.post("/tasks", status_code=status.HTTP_201_CREATED)
    def create_task(
        request: TaskCreate,
        idempotency_key: str = Header(min_length=8, max_length=200),
    ) -> dict[str, Any]:
        actor, source = actor_resolver()
        return _create_task(
            pool,
            jobs,
            request,
            actor=actor,
            source=source,
            idempotency_key=idempotency_key,
        )

    @router.get("/tasks")
    def list_tasks(
        project_id: str | None = None,
        status_filter: list[str] | None = Query(default=None, alias="status"),
        stage_key: str | None = None,
        attention_only: bool = False,
        include_terminal: bool = True,
        q: str | None = Query(default=None, max_length=200),
        cursor: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        with pool.connection() as connection:
            try:
                return tasks.list_tasks(
                    connection,
                    project_id=project_id,
                    statuses=status_filter,
                    stage_key=stage_key,
                    attention_only=attention_only,
                    include_terminal=include_terminal,
                    query=q,
                    cursor=cursor,
                    limit=limit,
                )
            except ValueError as invalid:
                raise error(
                    "SNAPSHOT_REQUIRED",
                    str(invalid),
                    http_status=status.HTTP_400_BAD_REQUEST,
                ) from invalid

    @router.get("/tasks/{task_id}")
    def get_task(task_id: uuid.UUID) -> dict[str, Any]:
        with pool.connection() as connection:
            task = runs.task_detail(connection, task_id)
        if task is None:
            raise error(
                "NOT_FOUND", "task not found", http_status=status.HTTP_404_NOT_FOUND
            )
        return task

    @router.post("/input-requests/{request_id}/answers")
    def answer_input(request_id: uuid.UUID, request: InputAnswers) -> dict[str, Any]:
        actor, _ = actor_resolver()
        with pool.connection() as connection:
            try:
                answered = tasks.answer_input_request(
                    connection,
                    request_id=request_id,
                    answers=request.answers,
                    actor=actor,
                    expected_revision=request.expected_revision,
                )
            except LookupError as missing:
                raise error(
                    "NOT_FOUND",
                    "input request not found",
                    http_status=status.HTTP_404_NOT_FOUND,
                ) from missing
            except tasks.InvalidState as invalid:
                raise error("INPUT_REQUIRED", str(invalid)) from invalid
            except tasks.RevisionConflict as conflict:
                raise error(
                    "REVISION_CONFLICT",
                    "task was updated by another operation",
                    current_revision=conflict.current_revision,
                ) from conflict
            connection.commit()
        return {
            "input_request_id": str(answered["id"]),
            "task_id": str(answered["task_id"]),
            "state": answered["state"],
            "resume_step": answered["resume_step"],
            "answer_revision": answered["answer_revision"],
        }

    @router.patch("/tasks/{task_id}")
    def patch_task(
        task_id: uuid.UUID,
        request: TaskMetadataUpdate,
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> dict[str, Any]:
        actor, _ = actor_resolver()
        expected = _expected_revision(request.expected_revision, if_match)
        with pool.connection() as connection:
            try:
                row = tasks.update_metadata(
                    connection,
                    task_id=task_id,
                    actor=actor,
                    title=request.title,
                    priority=request.priority,
                    expected_revision=expected,
                )
            except LookupError as missing:
                raise error(
                    "NOT_FOUND", "task not found", http_status=status.HTTP_404_NOT_FOUND
                ) from missing
            except tasks.RevisionConflict as conflict:
                raise error(
                    "REVISION_CONFLICT",
                    "task was updated by another operation",
                    current_revision=conflict.current_revision,
                ) from conflict
            connection.commit()
        return tasks.public_task(row)

    @router.post("/tasks/{task_id}/commands", status_code=status.HTTP_202_ACCEPTED)
    def run_command(
        task_id: uuid.UUID,
        request: TaskCommand,
        idempotency_key: str = Header(min_length=8, max_length=200),
        human_approval_token: str | None = Header(
            default=None, alias="X-Human-Approval-Token"
        ),
    ) -> dict[str, Any]:
        actor, _ = actor_resolver()
        if request.type in HUMAN_ONLY_COMMANDS:
            # Accepting or rejecting a deliverable is recorded against the human
            # who holds that credential, not against the calling service.
            actor = human_principal(human_approval_token)
        return _run_command(
            pool, jobs, task_id, request, actor=actor, idempotency_key=idempotency_key
        )

    @router.get("/commands/{command_id}")
    def get_command(command_id: uuid.UUID) -> dict[str, Any]:
        with pool.connection() as connection:
            command = tasks.get_command(connection, command_id)
        if command is None:
            raise error(
                "NOT_FOUND", "command not found", http_status=status.HTTP_404_NOT_FOUND
            )
        return command

    @router.post("/tasks/{task_id}/messages", status_code=status.HTTP_201_CREATED)
    def add_message(task_id: uuid.UUID, request: TaskMessageCreate) -> dict[str, Any]:
        actor, _ = actor_resolver()
        try:
            return add_task_message(
                pool,
                task_id,
                kind=request.kind,
                body=request.body,
                applies_to=request.applies_to,
                actor=actor,
            )
        except LookupError as missing:
            raise error(
                "NOT_FOUND", "task not found", http_status=status.HTTP_404_NOT_FOUND
            ) from missing
        except tasks.InvalidState as invalid:
            raise error("INVALID_STATE", str(invalid)) from invalid

    @router.get("/tasks/{task_id}/runs")
    def get_runs(task_id: uuid.UUID) -> dict[str, Any]:
        with pool.connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM tasks WHERE id = %s", (task_id,)
            ).fetchone()
            if exists is None:
                raise error(
                    "NOT_FOUND", "task not found", http_status=status.HTTP_404_NOT_FOUND
                )
            return {"task_id": str(task_id), "runs": tasks.get_runs(connection, task_id)}

    @router.get("/artifacts/{artifact_id}")
    def get_artifact(
        artifact_id: uuid.UUID, project_id: str | None = None
    ) -> dict[str, Any]:
        return _artifact(pool, artifact_id, project_id=project_id, include_content=False)

    @router.get("/artifacts/{artifact_id}/content")
    def get_artifact_content(
        artifact_id: uuid.UUID, project_id: str | None = None
    ) -> dict[str, Any]:
        artifact = _artifact(
            pool, artifact_id, project_id=project_id, include_content=True
        )
        if artifact["media_type"] != "application/json":
            raise error(
                "ARTIFACT_CONTENT_UNAVAILABLE",
                "this artifact is retrieved through its own download route",
                current_revision=None,
            )
        return artifact

    @router.get("/workers")
    def get_workers() -> dict[str, Any]:
        """What the Workers last reported, and whether anything can run now."""
        with pool.connection() as connection:
            return tasks.worker_inventory(connection)

    @router.post("/workers/{worker_id}/commands", status_code=status.HTTP_202_ACCEPTED)
    def run_worker_command(
        worker_id: str, request: WorkerCommand
    ) -> dict[str, Any]:
        """Ask a Worker to stop or resume taking new jobs.

        The desired state is recorded here and pushed by the scheduler; the
        Worker reports which revision it applied, so "asked" and "applied" stay
        distinguishable. Draining never expires on its own.
        """
        actor, _ = actor_resolver()
        with pool.connection() as connection:
            row = tasks.set_worker_intake(
                connection,
                worker_id=worker_id,
                accepting_jobs=request.type == "resume",
                actor=actor,
                reason=request.reason,
            )
            connection.commit()
        return {
            "worker_id": worker_id,
            "desired_accepting_jobs": row["accepting_jobs"],
            "desired_revision": row["revision"],
            "applied": False,
            "status": "ACCEPTED",
        }

    @router.get("/catalog")
    def get_catalog() -> dict[str, Any]:
        return {
            # The publishable set is the intersection of what the Gateway permits,
            # the Agent Release binds and a compatible Worker executor reports.
            # Only the Gateway side is known here, so nothing is claimed ready.
            "actions": [
                {
                    "action": action,
                    "gateway_permitted": True,
                    "availability": "unverified",
                    "reason": "AGENT_WORKER_COMPATIBILITY_NOT_REPORTED",
                    "stage_key": workflows.action_stage(action),
                }
                for action in jobs.actions
            ],
            "workflows": workflows.catalog(),
            "stage_keys": list(workflows.STAGE_KEYS),
            "catalog_completeness": "gateway_only",
        }

    @router.get("/events")
    def get_events(
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, Any]:
        with pool.connection() as connection:
            try:
                return tasks.events_after(connection, after, limit)
            except tasks.SnapshotRequired as required:
                raise error(
                    "SNAPSHOT_REQUIRED",
                    str(required),
                    http_status=status.HTTP_400_BAD_REQUEST,
                ) from required

    @router.get("/snapshot")
    def get_snapshot(limit: int = Query(default=200, ge=1, le=500)) -> dict[str, Any]:
        with pool.connection() as connection:
            # Repeatable read keeps the returned cursor consistent with the rows.
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            return tasks.snapshot(connection, limit=limit)

    return router


def _expected_revision(from_body: int | None, if_match: str | None) -> int | None:
    parsed = None
    if if_match:
        try:
            parsed = int(if_match.strip('"'))
        except ValueError as invalid:
            raise error(
                "REVISION_CONFLICT",
                "If-Match must carry a task revision",
                http_status=status.HTTP_400_BAD_REQUEST,
            ) from invalid
    if from_body is not None and parsed is not None and from_body != parsed:
        raise error(
            "REVISION_CONFLICT",
            "If-Match and expected_revision disagree",
            http_status=status.HTTP_400_BAD_REQUEST,
        )
    return from_body if from_body is not None else parsed


def _artifact(
    pool: Any, artifact_id: uuid.UUID, *, project_id: str | None, include_content: bool
) -> dict[str, Any]:
    with pool.connection() as connection:
        artifact = tasks.get_artifact(
            connection, artifact_id, include_content=include_content
        )
    if artifact is None:
        raise error(
            "NOT_FOUND", "artifact not found", http_status=status.HTTP_404_NOT_FOUND
        )
    if project_id is not None and artifact["project_id"] != project_id:
        # A consistency check for a caller that states the project it means. The
        # Gateway credential is still global, so this is not yet per-principal
        # authorization; that arrives with separated principals.
        raise error(
            "ARTIFACT_MISMATCH",
            "artifact belongs to another project",
            http_status=status.HTTP_404_NOT_FOUND,
        )
    return artifact


def add_task_message(
    pool: Any,
    task_id: uuid.UUID,
    *,
    kind: str,
    body: str,
    applies_to: str,
    actor: str,
) -> dict[str, Any]:
    """Record one comment or instruction, with what it implies for the request.

    An instruction that says the work must be redone changes what the Task asks
    for, so it records a new input version and takes any deliverable back out of
    review. Every surface goes through here: recording the message without that
    would leave the previous result acceptable.
    """
    with pool.connection() as connection:
        task = tasks.locked_task(connection, task_id)
        workflow = workflows.get(task["workflow_id"])
        if applies_to != "note_only" and workflow is not None and workflow.driver == "gateway":
            # Nothing reads instructions on this workflow: its execution is built
            # from the parameters of its input version.
            raise tasks.InvalidState(
                f"{task['workflow_id']} は parameters で実行内容を決めるため、"
                "追加指示は反映されません。revise_input で parameters を変更してください"
            )
        row = tasks.add_message(
            connection,
            task_id=task_id,
            kind=kind,
            body=body,
            author=actor,
            applies_to=applies_to,
            input_revision=task["input_revision"],
        )
        revised = None
        if applies_to == "restart_required":
            revised = runs.revise_input(
                connection,
                task=task,
                actor=actor,
                reason=body,
                origin="task_message",
            )
        connection.commit()
    return {
        "message_id": str(row["id"]),
        "task_id": str(task_id),
        "kind": row["kind"],
        "applies_to": row["applies_to"],
        "input_revision": (revised or {}).get("input_revision") or row["input_revision"],
        "restarted": revised is not None,
        "created_at": row["created_at"].isoformat(),
    }


def _run_command(
    pool: Any,
    jobs: JobBridge,
    task_id: uuid.UUID,
    request: TaskCommand,
    *,
    actor: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Accept one operation on a Task: verified, recorded, then executed once."""
    request_hash = tasks.canonical_digest(
        {"task_id": str(task_id), **request.model_dump(mode="json")}
    )[0]
    job_request = None
    with pool.connection() as connection:
        # One lock order everywhere: project, then task, then capacity and rows.
        owner = connection.execute(
            "SELECT project_id FROM tasks WHERE id = %s", (task_id,)
        ).fetchone()
        if owner is None:
            raise error(
                "NOT_FOUND", "task not found", http_status=status.HTTP_404_NOT_FOUND
            )
        connection.execute(
            "SELECT id FROM projects WHERE id = %s FOR UPDATE", (owner["project_id"],)
        ).fetchone()
        try:
            task = tasks.locked_task(connection, task_id)
        except LookupError as missing:
            raise error(
                "NOT_FOUND", "task not found", http_status=status.HTTP_404_NOT_FOUND
            ) from missing
        try:
            command, created = tasks.open_command(
                connection,
                principal=actor,
                target_type="task",
                target_id=str(task_id),
                command_type=request.type,
                request_hash=request_hash,
                idempotency_key=idempotency_key,
            )
        except tasks.CommandConflict as conflict:
            raise error("REVISION_CONFLICT", str(conflict)) from conflict
        if not created:
            # A replay returns the first outcome instead of acting again.
            return {**tasks.public_command(command), "idempotent_replay": True}

        if request.expected_revision is not None and task["revision"] != request.expected_revision:
            raise error(
                "REVISION_CONFLICT",
                "task was updated by another operation",
                current_revision=task["revision"],
            )

        frozen = tasks.frozen_request(connection, task)
        commands = runs.available_commands(connection, task)
        permitted = {command["type"] for command in commands["available"]}
        if request.type not in permitted:
            reason = next(
                (item for item in commands["unavailable"] if item["type"] == request.type),
                {"reason": "INVALID_STATE", "detail": f"status={task['status']}"},
            )
            raise error(reason["reason"], reason.get("detail", ""), current_revision=task["revision"])

        workflow = workflows.get(task["workflow_id"])
        result: dict[str, Any]
        if request.type in {"start", "retry"}:
            if workflow is not None and workflow.driver == "controller":
                # The Controller proposes the first step; starting only fixes the
                # Workflow, the input revision and the configuration.
                started = tasks.start_workflow_run(
                    connection, task=task, workflow=workflow, actor=actor
                )
                result = {
                    "task_id": str(task_id),
                    "run_id": str(started["run"]["id"]),
                    "task_revision": started["task"]["revision"],
                    "driver": "controller",
                }
            else:
                action = frozen["action"]
                if action not in jobs.actions:
                    raise error(
                        "CAPABILITY_UNAVAILABLE",
                        f"action {action} has no enabled execution route",
                    )
                job_request = jobs.build_request(
                    action=action,
                    project_id=task["project_id"],
                    environment=task["environment"],
                    parameters=tasks.parameters_with_task_criteria(
                        action,
                        frozen.get("parameters") or {},
                        frozen.get("acceptance_criteria"),
                    ),
                    limits=frozen.get("limits") or {},
                )
                job_row, _ = jobs.insert(
                    connection,
                    job_request,
                    f"command-{command['id']}",
                    orchestration_mode=task["orchestration_mode"],
                )
                started = tasks.start_single_action_run(
                    connection,
                    task=task,
                    action=action,
                    job_id=job_row["id"],
                    parameters=frozen.get("parameters") or {},
                    limits=frozen.get("limits") or {},
                    actor=actor,
                )
                result = {
                    "task_id": str(task_id),
                    "task_revision": started["task"]["revision"],
                    "run_id": str(started["run"]["id"]),
                    "job_id": str(job_row["id"]),
                    "attempt_id": str(started["attempt_id"]),
                }
        elif request.type == "pause":
            result = {"task_id": str(task_id), **runs.pause(connection, task=task, actor=actor)}
        elif request.type == "resume":
            result = {"task_id": str(task_id), **runs.resume(connection, task=task, actor=actor)}
        elif request.type == "cancel":
            result = {
                "task_id": str(task_id),
                **runs.cancel(connection, task=task, actor=actor, reason=request.reason),
            }
        elif request.type == "revise_input":
            try:
                result = {
                    "task_id": str(task_id),
                    **runs.revise_input(
                        connection,
                        task=task,
                        actor=actor,
                        objective=request.objective,
                        acceptance_criteria=request.acceptance_criteria,
                        context_refs=request.context_refs,
                        parameters=request.parameters,
                        limits=request.limits,
                        reason=request.reason or "",
                    ),
                }
            except tasks.InvalidState as invalid:
                raise error("INVALID_STATE", str(invalid)) from invalid
        else:
            try:
                result = {
                    "task_id": str(task_id),
                    **runs.decide(
                        connection,
                        task=task,
                        kind=request.type,
                        target_digest=request.target_digest or "",
                        reason=request.reason,
                        actor=actor,
                    ),
                }
            except tasks.ArtifactMismatch as mismatch:
                raise error(
                    "ARTIFACT_MISMATCH",
                    "the deliverable changed after this decision was formed",
                    field_errors={"target_digest": mismatch.current_digest},
                ) from mismatch
            except tasks.InvalidState as invalid:
                raise error("INVALID_STATE", str(invalid)) from invalid

        command = tasks.finish_command(
            connection,
            command_id=command["id"],
            status="SUCCEEDED" if not result.get("stopping") else "APPLYING",
            result=result,
        )
        connection.commit()

    return {**tasks.public_command(command), "idempotent_replay": False}


def _create_task(
    pool: Any,
    jobs: JobBridge,
    request: TaskCreate,
    *,
    actor: str,
    source: str,
    idempotency_key: str,
) -> dict[str, Any]:
    workflow = workflows.get(request.workflow_id)
    if workflow is None:
        raise error(
            "CAPABILITY_UNAVAILABLE",
            f"unknown workflow {request.workflow_id}",
            http_status=status.HTTP_400_BAD_REQUEST,
            field_errors={"workflow_id": f"利用可能: {', '.join(workflows.WORKFLOWS)}"},
        )
    if request.start and not workflow.startable:
        raise error(
            "CAPABILITY_UNAVAILABLE",
            f"{workflow.id} cannot be started yet",
            field_errors={
                "workflow_id": f"開始可能: {', '.join(workflows.startable_ids())}"
            },
        )

    job_request = None
    if request.action is not None:
        if request.action not in jobs.actions:
            raise error(
                "CAPABILITY_UNAVAILABLE",
                f"action {request.action} has no enabled execution route",
                http_status=status.HTTP_400_BAD_REQUEST,
                field_errors={"action": f"利用可能: {', '.join(jobs.actions)}"},
            )
        job_request = jobs.build_request(
            action=request.action,
            project_id=request.project_id,
            environment=request.environment,
            # The conditions the Task states are part of what this action is asked
            # for, not a separate note beside it.
            parameters=tasks.parameters_with_task_criteria(
                request.action, request.parameters, request.acceptance_criteria
            ),
            limits=request.limits,
        )

    request_hash = tasks.canonical_digest(request.model_dump(mode="json"))[0]
    with pool.connection() as connection:
        # Lock order across every writer: project, then task, then run/step/job.
        connection.execute(
            "SELECT id FROM projects WHERE id = %s FOR UPDATE", (request.project_id,)
        ).fetchone()
        try:
            command, created = tasks.open_command(
                connection,
                principal=actor,
                target_type="project",
                target_id=request.project_id,
                command_type="create_task",
                request_hash=request_hash,
                idempotency_key=idempotency_key,
            )
        except tasks.CommandConflict as conflict:
            raise error("REVISION_CONFLICT", str(conflict)) from conflict
        if not created:
            # A retried creation returns the original Task and execution instead
            # of registering the same request twice.
            stored = command["result"] or {}
            existing = runs.task_detail(connection, uuid.UUID(stored["task_id"]))
            return {
                **(existing or {}),
                "job_id": stored.get("job_id"),
                "attempt_id": stored.get("attempt_id"),
                "idempotent_replay": True,
            }
        # Execution capacity is admitted before any event cursor is allocated, so
        # every path takes project, capacity, then counter in the same order.
        job_row = None
        if request.start and workflow.driver == "gateway" and job_request is not None:
            job_row, _ = jobs.insert(
                connection,
                job_request,
                f"task-{command['id']}",
                orchestration_mode="workflow-v1",
            )
        task = tasks.create_task(
            connection,
            project_id=request.project_id,
            title=request.title,
            objective=request.objective,
            workflow_id=workflow.id,
            environment=request.environment,
            actor=actor,
            source=source,
            acceptance_criteria=request.acceptance_criteria,
            context_refs=request.context_refs,
            priority=request.priority,
            # Frozen with the input revision so a later start runs this request,
            # not whatever the defaults happen to be then.
            request=(
                {
                    "action": request.action,
                    "parameters": request.parameters,
                    "limits": request.limits,
                }
                if request.action
                # A Workflow request has no action of its own, but the limits the
                # requester set still bind every step it runs.
                else ({"limits": request.limits} if request.limits else {})
            ),
        )
        result: dict[str, Any] = {"job_id": None, "attempt_id": None}
        if request.start:
            # The same conditions the start command applies, checked before the
            # request is registered: a refused start registers nothing.
            commands = runs.available_commands(connection, task)
            if "start" not in {item["type"] for item in commands["available"]}:
                reason = next(
                    (item for item in commands["unavailable"] if item["type"] == "start"),
                    {"reason": "INVALID_STATE", "detail": ""},
                )
                raise error(reason["reason"], reason.get("detail", ""))
        if request.start and workflow.driver == "controller":
            started = tasks.start_workflow_run(
                connection, task=task, workflow=workflow, actor=actor
            )
            task = started["task"]
            result = {
                "job_id": None,
                "attempt_id": None,
                "run_id": str(started["run"]["id"]),
            }
        elif job_row is not None:
            started = tasks.start_single_action_run(
                connection,
                task=task,
                action=request.action or "",
                job_id=job_row["id"],
                parameters=request.parameters,
                limits=request.limits,
                actor=actor,
            )
            task = started["task"]
            result = {
                "job_id": str(job_row["id"]),
                "attempt_id": str(started["attempt_id"]),
            }
        tasks.finish_command(
            connection,
            command_id=command["id"],
            status="SUCCEEDED",
            result={"task_id": str(task["id"]), **result},
        )
        connection.commit()

    with pool.connection() as connection:
        detail = runs.task_detail(connection, task["id"])
    return {**(detail or tasks.public_task(task)), **result, "idempotent_replay": False}


def register_mcp(mcp: Any, *, pool: Any, jobs: JobBridge, actor: str, source: str) -> None:
    """Expose the read and request tools a bot may use.

    Configuration editing, Harness permission changes and human-only decisions
    stay off this surface.
    """

    @mcp.tool(
        name="list_capabilities",
        description=(
            "List the work the Gateway can accept, with each action's stage and "
            "whether an Agent and Worker have confirmed they can run it."
        ),
        structured_output=True,
    )
    def mcp_list_capabilities() -> dict[str, Any]:
        return {
            "actions": [
                {
                    "action": action,
                    "stage_key": workflows.action_stage(action),
                    "availability": "unverified",
                    "reason": "AGENT_WORKER_COMPATIBILITY_NOT_REPORTED",
                }
                for action in jobs.actions
            ],
            "catalog_completeness": "gateway_only",
        }

    @mcp.tool(
        name="list_workflows",
        description="List the defined request routes and which of them can be started now.",
        structured_output=True,
    )
    def mcp_list_workflows() -> dict[str, Any]:
        return {
            "workflows": workflows.catalog(),
            "startable": list(workflows.startable_ids()),
        }

    @mcp.tool(
        name="create_task",
        description=(
            "Register one request as a Task with its objective and acceptance "
            "criteria, and optionally start it. Returns the task_id to follow. "
            "Retrying an identical request returns the same Task; pass a distinct "
            "idempotency_key to register a deliberately repeated request."
        ),
        structured_output=True,
    )
    def mcp_create_task(
        project_id: str,
        title: str,
        objective: str,
        action: str | None = None,
        acceptance_criteria: list[str] | None = None,
        workflow_id: str = workflows.SINGLE_ACTION_V1.id,
        environment: Literal["research", "preview"] = "preview",
        priority: Literal["low", "normal", "high", "urgent"] = "normal",
        parameters: dict[str, Any] | None = None,
        limits: dict[str, int] | None = None,
        start: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        request = TaskCreate(
            project_id=project_id,
            title=title,
            objective=objective,
            acceptance_criteria=acceptance_criteria or [],
            workflow_id=workflow_id,
            environment=environment,
            priority=priority,
            action=action,
            parameters=parameters or {},
            limits=limits or {},
            start=start,
        )
        # Without an explicit key, the request's own content is the key, so a bot
        # that retries after a lost response gets the first Task back instead of
        # registering the same request again.
        key = idempotency_key or (
            "content-" + tasks.canonical_digest(request.model_dump(mode="json"))[0][:40]
        )
        if not 8 <= len(key) <= 200:
            raise ValueError("idempotency_key must contain between 8 and 200 characters")
        return _create_task(
            pool, jobs, request, actor=actor, source=source, idempotency_key=key
        )

    @mcp.tool(
        name="get_task",
        description="Get one Task with its steps, attempts, artifacts and required input.",
        structured_output=True,
    )
    def mcp_get_task(task_id: str) -> dict[str, Any]:
        with pool.connection() as connection:
            task = runs.task_detail(connection, _parse_uuid(task_id, "task_id"))
        if task is None:
            raise ValueError("task not found")
        return task

    @mcp.tool(
        name="list_tasks",
        description="List Tasks, newest first, optionally narrowed to one project or to what needs attention.",
        structured_output=True,
    )
    def mcp_list_tasks(
        project_id: str | None = None,
        attention_only: bool = False,
        include_terminal: bool = True,
        limit: int = 25,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with pool.connection() as connection:
            return tasks.list_tasks(
                connection,
                project_id=project_id,
                attention_only=attention_only,
                include_terminal=include_terminal,
                limit=limit,
            )

    @mcp.tool(
        name="request_task_action",
        description=(
            "Request an operation on a Task: start, pause, resume, cancel or "
            "retry. Accepting or rejecting a deliverable is a human decision and "
            "is not available here. Reuse the same idempotency_key when retrying."
        ),
        structured_output=True,
    )
    def mcp_request_task_action(
        task_id: str,
        type: Literal["start", "pause", "resume", "cancel", "retry"],
        idempotency_key: str | None = None,
        expected_revision: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        key = idempotency_key or f"grok-{uuid.uuid4()}"
        if not 8 <= len(key) <= 200:
            raise ValueError("idempotency_key must contain between 8 and 200 characters")
        return _run_command(
            pool,
            jobs,
            _parse_uuid(task_id, "task_id"),
            TaskCommand(type=type, expected_revision=expected_revision, reason=reason),
            actor=actor,
            idempotency_key=key,
        )

    @mcp.tool(
        name="add_task_instruction",
        description=(
            "Add a comment or an additional instruction to a Task and say when it "
            "should take effect. A note or an instruction for the next attempt "
            "changes no state; `restart_required` does — it records a new version of "
            "what the Task asks for, takes any deliverable back out of review and "
            "sends the work back to be redone."
        ),
        structured_output=True,
    )
    def mcp_add_task_instruction(
        task_id: str,
        body: str,
        kind: Literal["comment", "instruction"] = "comment",
        applies_to: Literal["note_only", "next_attempt", "restart_required"] = "note_only",
    ) -> dict[str, Any]:
        parsed = _parse_uuid(task_id, "task_id")
        try:
            return add_task_message(
                pool, parsed, kind=kind, body=body, applies_to=applies_to, actor=actor
            )
        except LookupError as missing:
            raise ValueError("task not found") from missing
        except tasks.InvalidState as invalid:
            raise ValueError(str(invalid)) from invalid


def _parse_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as invalid:
        raise ValueError(f"{field} must be a valid UUID") from invalid
