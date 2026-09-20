"""Workflow Run progression.

The Controller decides *when* a Run is looked at; the Gateway decides *what may
happen next*. Both read the same typed definition, and this module is the
authority: a proposal that is not in the set of allowed next actions is refused,
so a restarted or duplicated Controller cannot invent a transition or double a
Job. Every function takes an open connection and never commits.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any, Callable

from app import tasks, workflows

# Lease defaults from the design: long enough to evaluate a Run, short enough
# that a dead Controller does not hold it.
LEASE_SECONDS = 30
MAX_LEASE_SECONDS = 300


class LeaseRejected(RuntimeError):
    """The lease is expired, unknown, or held by another Controller."""


class ProposalRejected(RuntimeError):
    """The proposed action is not allowed for this Run's current state."""


def lease_runs(
    connection: Any, *, owner: str, limit: int = 5, lease_seconds: int = LEASE_SECONDS
) -> list[dict[str, Any]]:
    """Claim Runs that need evaluating, fencing out any previous holder.

    The fencing token increases on every claim, so a proposal built under an
    expired lease is refused instead of applied late.
    """
    seconds = max(5, min(lease_seconds, MAX_LEASE_SECONDS))
    now = tasks.utcnow()
    rows = connection.execute(
        """
        SELECT r.id
          FROM workflow_runs r
          JOIN tasks t ON t.id = r.task_id
         WHERE r.status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED', 'SUPERSEDED')
           AND r.orchestration_mode = 'workflow-v1'
           -- Free, expired, or already held by this Controller: another one may
           -- only take over after the current lease has run out.
           AND (r.lease_until IS NULL OR r.lease_until < %s OR r.lease_owner = %s)
         ORDER BY r.updated_at
         LIMIT %s
         FOR UPDATE OF r SKIP LOCKED
        """,
        (now, owner, limit),
    ).fetchall()
    leased: list[dict[str, Any]] = []
    for row in rows:
        updated = connection.execute(
            "UPDATE workflow_runs SET lease_owner = %s, lease_until = %s, "
            "fencing_token = fencing_token + 1, updated_at = %s WHERE id = %s "
            "RETURNING fencing_token, lease_until",
            (owner, now + timedelta(seconds=seconds), now, row["id"]),
        ).fetchone()
        state = run_state(connection, row["id"])
        state["lease"] = {
            "owner": owner,
            "token": updated["fencing_token"],
            "expires_at": updated["lease_until"].isoformat(),
        }
        leased.append(state)
    return leased


def renew_lease(
    connection: Any, *, run_id: uuid.UUID, token: int, lease_seconds: int = LEASE_SECONDS
) -> dict[str, Any]:
    run = _locked_run(connection, run_id)
    _check_lease(run, token)
    seconds = max(5, min(lease_seconds, MAX_LEASE_SECONDS))
    updated = connection.execute(
        "UPDATE workflow_runs SET lease_until = %s, updated_at = %s WHERE id = %s "
        "RETURNING fencing_token, lease_until",
        (tasks.utcnow() + timedelta(seconds=seconds), tasks.utcnow(), run_id),
    ).fetchone()
    return {
        "run_id": str(run_id),
        "token": updated["fencing_token"],
        "expires_at": updated["lease_until"].isoformat(),
    }


def _locked_run_and_task(
    connection: Any, run_id: uuid.UUID
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Lock what a Run's progression touches, in the one order everything uses.

    Project, then Task, then Run, then the job row. A human command comes in
    through the Project and reaches the Task and its Run from there; a proposal
    starts from the Run and inserts a Job, which locks the Project. Taking the
    Project and the Task first here is what keeps the two from meeting head-on
    and leaving PostgreSQL to abort one of them.
    """
    owner = connection.execute(
        """
        SELECT r.task_id, t.project_id
          FROM workflow_runs r JOIN tasks t ON t.id = r.task_id
         WHERE r.id = %s
        """,
        (run_id,),
    ).fetchone()
    if owner is None:
        raise LookupError("run not found")
    connection.execute(
        "SELECT id FROM projects WHERE id = %s FOR UPDATE", (owner["project_id"],)
    ).fetchone()
    task = tasks.locked_task(connection, owner["task_id"])
    return _locked_run(connection, run_id), task


def _locked_run(connection: Any, run_id: uuid.UUID) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,)
    ).fetchone()
    if row is None:
        raise LookupError("run not found")
    return row


def _check_lease(run: dict[str, Any], token: int) -> None:
    if run["fencing_token"] != token:
        raise LeaseRejected("this lease has been superseded")
    if run["lease_until"] is None or run["lease_until"] < tasks.utcnow():
        raise LeaseRejected("this lease has expired")


def run_state(connection: Any, run_id: uuid.UUID) -> dict[str, Any]:
    run = connection.execute(
        "SELECT * FROM workflow_runs WHERE id = %s", (run_id,)
    ).fetchone()
    if run is None:
        raise LookupError("run not found")
    task = connection.execute(
        "SELECT * FROM tasks WHERE id = %s", (run["task_id"],)
    ).fetchone()
    steps = _steps(connection, run_id)
    workflow = workflows.get(run["workflow_id"])
    revisions = connection.execute(
        "SELECT * FROM task_input_revisions WHERE task_id = %s AND revision = %s",
        (task["id"], run["input_revision"]),
    ).fetchone()
    artifacts = [
        {
            "artifact_id": str(row["id"]),
            "kind": row["kind"],
            "digest": row["digest"],
            "producer_step": row["produced_by_step"],
            # The execution that produced it. A verification has to name that one, not
            # whichever step happens to have succeeded most recently.
            "producer_worker_job_id": row["producer_worker_job_id"],
            "input_revision": row["input_revision"],
        }
        for row in connection.execute(
            "SELECT a.*, a.manifest->>'produced_by_step' AS produced_by_step, "
            "       j.worker_job_id AS producer_worker_job_id "
            "  FROM artifacts a "
            "  LEFT JOIN step_attempts att ON att.id = a.producer_attempt_id "
            "  LEFT JOIN jobs j ON j.id = att.job_id "
            " WHERE a.run_id = %s ORDER BY a.created_at",
            (run_id,),
        ).fetchall()
    ]
    open_input = connection.execute(
        "SELECT * FROM input_requests WHERE task_id = %s AND state = 'OPEN'",
        (task["id"],),
    ).fetchone()
    # What people have already told this Run: answers, decision reasons and
    # instructions meant for the next attempt. A handoff that ignored these would
    # repeat work the requester has already corrected.
    answered = [
        {
            "input_request_id": str(row["id"]),
            "questions": row["questions"],
            "answers": row["answers"],
            "resume_step": row["resume_step"],
            # Which version of the request this answer was about. An answer given
            # before the request changed may have been superseded by that change, so
            # a step is told which it is rather than being left to assume.
            "input_revision": row["answer_revision"] or row["input_revision"],
            "answered_at": row["answered_at"].isoformat() if row["answered_at"] else None,
        }
        for row in connection.execute(
            # This Run's own questions: an answer given to an earlier Run was about
            # work that is finished, and carrying it here would crowd out what this
            # step actually needs.
            "SELECT * FROM input_requests WHERE run_id = %s AND state = 'ANSWERED' "
            "ORDER BY answered_at",
            (run_id,),
        ).fetchall()
    ]
    decisions = [
        {
            "kind": row["kind"],
            "reason": row["reason"],
            "target_digest": row["target_digest"],
            "actor": row["actor"],
            # The version of the request this decision was about, for the same
            # reason as an answer.
            "input_revision": row["input_revision"],
            "created_at": row["created_at"].isoformat(),
        }
        for row in connection.execute(
            "SELECT * FROM decisions WHERE run_id = %s ORDER BY created_at", (run_id,)
        ).fetchall()
    ]
    instructions = [
        {
            "kind": row["kind"],
            "body": row["body"],
            "applies_to": row["applies_to"],
            "author": row["author"],
            "created_at": row["created_at"].isoformat(),
        }
        for row in connection.execute(
            # Outstanding only: an instruction an attempt has already been given is
            # part of that work now — unless that attempt ended without delivering,
            # in which case nothing was done with it and the retry must be told too.
            "SELECT m.* FROM task_messages m "
            "LEFT JOIN step_attempts a ON a.id::text = m.consumed_by "
            "WHERE m.task_id = %s AND m.applies_to <> 'note_only' "
            "AND (m.consumed_at IS NULL "
            "     OR (a.id IS NOT NULL AND a.status IN ('FAILED', 'CANCELLED'))) "
            "ORDER BY created_at",
            (task["id"],),
        ).fetchall()
    ]
    state = {
        "run_id": str(run["id"]),
        "task_id": str(task["id"]),
        "project_id": task["project_id"],
        "environment": task["environment"],
        "workflow_id": run["workflow_id"],
        "workflow_version": run["workflow_version"],
        "status": run["status"],
        "input_revision": run["input_revision"],
        # The last version that changed what is asked for. Anything produced or said
        # at or after it still stands: a revision that only changed a limit or added
        # a reference supersedes nothing a person said.
        "requirements_revision": int(
            (revisions or {}).get("requirements_revision") or run["input_revision"]
        ),
        "revision_cycles": run["revision_cycles"],
        "task_status": task["status"],
        "task_revision": task["revision"],
        "control_state": task["control_state"],
        "objective": task["objective"],
        "acceptance_criteria": (revisions or {}).get("acceptance_criteria") or [],
        # What the requester attached to this version, so a step is built with the
        # references it was given rather than without them.
        "context_refs": (revisions or {}).get("context_refs") or [],
        "request": (revisions or {}).get("request") or {},
        # Why this version of the request exists, so the next step is built from
        # the change rather than from the version it replaced.
        "input_revision_reason": (revisions or {}).get("reason") or "",
        "steps": steps,
        "artifacts": artifacts,
        "open_input_request": None
        if open_input is None
        else {
            "input_request_id": str(open_input["id"]),
            "questions": open_input["questions"],
            "resume_step": open_input["resume_step"],
        },
        "answered_inputs": answered,
        "decisions": decisions,
        "instructions": instructions,
        "limits": {
            "max_revision_cycles": workflow.max_revision_cycles if workflow else 0,
            "max_job_attempts_per_step": workflow.max_job_attempts_per_step if workflow else 0,
        },
    }
    state["next_actions"] = allowed_next(connection, state)
    return state


def _steps(connection: Any, run_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM workflow_steps WHERE run_id = %s ORDER BY position, cycle", (run_id,)
    ).fetchall()
    attempts = connection.execute(
        """
        SELECT a.*, j.worker_job_id, j.state AS job_state
          FROM step_attempts a
          LEFT JOIN jobs j ON j.id = a.job_id
         WHERE a.step_id = ANY(%s)
         ORDER BY a.attempt_number
        """,
        ([row["id"] for row in rows] or [None],),
    ).fetchall()
    by_step: dict[Any, list[dict[str, Any]]] = {}
    for attempt in attempts:
        summary = attempt["result_summary"] or {}
        by_step.setdefault(attempt["step_id"], []).append(
            {
                "attempt_id": str(attempt["id"]),
                "attempt_number": attempt["attempt_number"],
                "status": attempt["status"],
                "job_id": str(attempt["job_id"]) if attempt["job_id"] else None,
                # The Worker execution, which is how a later step refers to the
                # workspace this attempt produced.
                "worker_job_id": attempt["worker_job_id"],
                "job_state": attempt["job_state"],
                "failure_class": attempt["failure_class"],
                "input_revision": (attempt["input_manifest"] or {}).get(
                    "input_revision"
                ),
                "quality_verdict": summary.get("quality_verdict"),
                "reported_verdict": summary.get("reported_verdict"),
                "contract_error": summary.get("contract_error"),
                "artifact_id": summary.get("artifact_id"),
            }
        )
    return [
        {
            "step_id": str(row["id"]),
            "logical_key": row["logical_key"],
            "cycle": row["cycle"],
            "position": row["position"],
            "stage_key": row["stage_key"],
            "kind": row["kind"],
            "action": row["action"],
            "agent_binding": row["agent_binding"],
            "status": row["status"],
            "created_at": row["created_at"].isoformat(),
            "attempts": by_step.get(row["id"], []),
        }
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# What may happen next
# --------------------------------------------------------------------------- #


def allowed_next(connection: Any, state: dict[str, Any]) -> list[dict[str, Any]]:
    """The only transitions the Gateway will accept for this Run right now."""
    workflow = workflows.get(state["workflow_id"])
    if workflow is None or state["status"] in tasks.RUN_TERMINAL:
        return []
    if state["control_state"] != "ACTIVE":
        return []  # Pausing and cancelling stop new work from starting.
    if state["open_input_request"]:
        return []  # The answer decides what happens next.

    steps = state["steps"]
    if not steps:
        entry = workflow.step(workflow.entry_step)
        return [_attempt_action(entry, cycle=1)]

    last = steps[-1]
    definition = workflow.steps.get(last["logical_key"])
    if definition is None:
        return []
    attempts = last["attempts"]
    active = [item for item in attempts if item["status"] not in tasks.ATTEMPT_TERMINAL]
    if active:
        return []  # Work is in flight; nothing else may start on this Run.

    if _nothing_ran_for_this_request(state):
        # Every attempt on this Run answered a version of the request that has since
        # been replaced — whatever step the Run is standing on, and whether that step
        # succeeded, failed or asked a question. Continuing from it would verify,
        # retry or resume work for a request nobody is making any more.
        return _after_request_changed(connection, workflow, state, last)

    if definition.kind == "human_review":
        if last["status"] == "SUPERSEDED":
            # The review was withdrawn because what the Task asks for changed, so
            # the work is done again under the current version rather than waiting
            # for a decision about the previous one.
            return _after_request_changed(connection, workflow, state, last)
        decision = connection.execute(
            "SELECT * FROM decisions WHERE run_id = %s AND created_at >= %s "
            "ORDER BY created_at DESC LIMIT 1",
            (state["run_id"], last["created_at"]),
        ).fetchone()
        if decision is None:
            return []  # Waiting for a human decision about this deliverable.
        if decision["kind"] == "accept_deliverable":
            current = tasks.deliverable(
                connection, uuid.UUID(state["run_id"]), state["input_revision"]
            )["digest"]
            if decision["target_digest"] != current:
                # The results changed after the acceptance, so it no longer applies.
                return []
            return [{"type": "complete_run", "reason": "accepted_deliverable"}]
        if decision["kind"] == "request_changes" and definition.on_request_changes:
            return [
                _next_for(
                    connection, workflow, state, definition.on_request_changes, last
                )
            ]
        return []

    if definition.kind == "input_request" and last["status"] == "SUPERSEDED":
        # The question was overtaken by a change to the request itself. Resuming
        # where an answer would have led would verify what was built for the
        # previous version, so the work is done again instead.
        return _after_request_changed(connection, workflow, state, last)

    if definition.kind == "input_request" and last["status"] != "SUCCEEDED":
        # An open question is handled above; anything else here means the Run is
        # waiting rather than ready.
        return []

    if last["status"] == "SUCCEEDED":
        verdict = attempts[-1]["quality_verdict"] if attempts else None
        target_key = definition.on_success
        if definition.verification and verdict == "fail":
            target_key = definition.on_fail
        elif definition.verification and verdict == "inconclusive":
            target_key = definition.on_inconclusive
        if target_key is None:
            return [{"type": "complete_run", "reason": "output_contract"}] if (
                workflow.completion == "output_contract"
            ) else []
        return [_next_for(connection, workflow, state, target_key, last)]

    if last["status"] == "FAILED":
        if len(attempts) < workflow.max_job_attempts_per_step:
            return [
                _attempt_action(
                    definition, cycle=last["cycle"], attempt_number=len(attempts) + 1
                )
            ]
        return [
            {
                "type": "fail_run",
                "failure_class": "attempt_limit_reached",
                "reason": f"{last['logical_key']} failed {len(attempts)} times",
            }
        ]
    return []


def _next_for(
    connection: Any,
    workflow: workflows.Workflow,
    state: dict[str, Any],
    target_key: str,
    last: dict[str, Any],
) -> dict[str, Any]:
    target = workflow.step(target_key)
    cycle = _cycle_for(state, target_key)
    if target.verification:
        newest = latest_change(connection, uuid.UUID(state["run_id"]))
        if newest is not None and newest["kind"] != "code-change":
            # A report about a change cannot be rebuilt, so it cannot be verified.
            # The work is done again rather than offered for a verification that
            # would be refused. `_next_for` answers with one action, so the redo is
            # unwrapped here.
            redo = _after_request_changed(connection, workflow, state, last)
            if redo:
                return redo[0]
            return {
                "type": "fail_run",
                "failure_class": "unverifiable_result",
                "reason": (
                    "the run produced a report about a change rather than a change "
                    "that can be verified, and there is nowhere to do the work again"
                ),
            }
    if target.kind == "human_review" and "qa_pass" in workflow.completion_requires:
        unverified = _unverified_change(connection, state)
        if unverified:
            return {
                "type": "fail_run",
                "failure_class": "unverified_deliverable",
                "reason": unverified,
            }
    if target.kind == "human_review":
        # A deliverable can come back for review after changes, so this is a new
        # cycle of the same step rather than a conflict with the first one.
        return {"type": "request_review", "step_key": target.key, "cycle": cycle}
    if target.revision_entry or target.kind == "input_request":
        # Fixes and repeated questions are revisions of the same work and share
        # one budget for the Run, so neither can go around the other.
        used = state["revision_cycles"]
        if used >= workflow.max_revision_cycles:
            return {
                "type": "fail_run",
                "failure_class": "revision_limit_reached",
                "reason": (
                    f"{target_key} would start revision {used + 1}, above the limit of "
                    f"{workflow.max_revision_cycles}"
                ),
            }
    if target.kind == "input_request":
        return {
            "type": "request_input",
            "step_key": target.key,
            "cycle": cycle,
            "resume_step": target.on_success,
        }
    if target.revision_entry and cycle > workflow.max_revision_cycles:
        return {
            "type": "fail_run",
            "failure_class": "revision_limit_reached",
            "reason": (
                f"{target_key} would start revision cycle {cycle}, above the limit of "
                f"{workflow.max_revision_cycles}"
            ),
        }
    return _attempt_action(target, cycle=cycle)


def _nothing_ran_for_this_request(state: dict[str, Any]) -> bool:
    """Whether no attempt on this Run was made for the request as it stands now.

    "As it stands now" means since what is asked for last changed: an attempt that
    ran before a revision which only extended a limit still answers this request, so
    it is not a reason to do the work again.
    """
    since = int(state.get("requirements_revision") or state["input_revision"])
    seen = False
    for step in state["steps"]:
        for attempt in step["attempts"]:
            ran_under = attempt.get("input_revision")
            if not ran_under:
                # From before attempts recorded what they ran against: not evidence
                # that the request changed.
                return False
            seen = True
            if int(ran_under) >= since:
                return False
    return seen


def _after_request_changed(
    connection: Any,
    workflow: workflows.Workflow,
    state: dict[str, Any],
    last: dict[str, Any],
) -> list[dict[str, Any]]:
    """Where a Run goes when what it has is not what it needs.

    The work itself has to answer the current request: a verification of what was
    built for an earlier one is not evidence about this one. What it starts from
    depends on what it has — a change it can continue, a result it cannot use, or
    nothing at all.
    """
    newest = latest_change(connection, uuid.UUID(state["run_id"]))
    if newest is None:
        target = workflow.entry_step
    elif newest["kind"] == "code-change":
        target = workflow.work_revision_step
    else:
        # A report about a change is nothing to continue: the step that produces a
        # change does it again.
        target = workflow.work_producing_step or workflow.entry_step
    if target is None:
        return []
    # Redoing the work is a revision of it, whichever step it starts from, and the
    # Run's budget bounds how many times that can happen — both what this step has
    # already cost and what the Run has spent altogether. Without this, repeated
    # changes to the request could restart a Run's work without limit.
    if state["revision_cycles"] >= workflow.max_revision_cycles:
        return [
            {
                "type": "fail_run",
                "failure_class": "revision_limit_reached",
                "reason": (
                    f"the work would start again for revision "
                    f"{state['revision_cycles'] + 1}, above the limit of "
                    f"{workflow.max_revision_cycles}"
                ),
            }
        ]
    if _cycle_for(state, target) > workflow.max_revision_cycles + 1:
        return [
            {
                "type": "fail_run",
                "failure_class": "revision_limit_reached",
                "reason": (
                    f"{target} would start again for revision "
                    f"{_cycle_for(state, target)}, above the limit of "
                    f"{workflow.max_revision_cycles}"
                ),
            }
        ]
    return [_next_for(connection, workflow, state, target, last)]


def _unverified_change(connection: Any, state: dict[str, Any]) -> str | None:
    """Whether the newest change has a passing verification of itself.

    Acceptance may only be asked for when the work being accepted is the work
    that passed, so a later change without its own passing verification blocks
    the review instead of inheriting an earlier pass.
    """
    run_id = uuid.UUID(state["run_id"])
    newest = latest_change(connection, run_id)
    if newest is None:
        return "no change has been produced for this deliverable"
    if newest["kind"] != "code-change":
        return (
            "the latest result is a report about a change, not a change that can be "
            "verified"
        )
    passing = connection.execute(
        """
        SELECT 1
          FROM step_attempts a
          JOIN workflow_steps s ON s.id = a.step_id
         WHERE s.run_id = %s
           AND a.status = 'SUCCEEDED'
           AND a.result_summary->>'quality_verdict' = 'pass'
           -- The verification of this exact change, not one that merely had it
           -- among its inputs.
           AND a.input_manifest->'verification_target'->>'artifact_id' = %s
           -- And of what the Task asks for now: a pass recorded before the request
           -- last changed what it asks for does not answer the current one.
           AND (a.input_manifest->>'input_revision')::int >= %s
         LIMIT 1
        """,
        (
            run_id,
            str(newest["id"]),
            int(state.get("requirements_revision") or state["input_revision"]),
        ),
    ).fetchone()
    if passing is None:
        return (
            "the latest change has no passing verification of its own for the "
            "current version of the request"
        )
    return None


def _cycle_for(state: dict[str, Any], step_key: str) -> int:
    existing = [step["cycle"] for step in state["steps"] if step["logical_key"] == step_key]
    return max(existing) + 1 if existing else 1


def _attempt_action(
    step: workflows.Step, *, cycle: int, attempt_number: int = 1
) -> dict[str, Any]:
    return {
        "type": "create_attempt",
        "step_key": step.key,
        "cycle": cycle,
        "attempt_number": attempt_number,
        "action": step.action,
        "agent": step.agent,
        "stage_key": step.stage,
        "input_from": step.input_from,
        "acceptance_from": step.acceptance_from,
        "feedback_from": step.feedback_from,
    }


# --------------------------------------------------------------------------- #
# Applying a proposal
# --------------------------------------------------------------------------- #


def apply_proposal(
    connection: Any,
    *,
    run_id: uuid.UUID,
    token: int,
    proposal: dict[str, Any],
    actor: str,
    job_factory: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate one proposed transition and apply it in this transaction."""
    run, task = _locked_run_and_task(connection, run_id)
    _check_lease(run, token)
    state = run_state(connection, run_id)
    expected = proposal.get("expected_task_revision")
    if expected is not None and expected != task["revision"]:
        raise ProposalRejected("the task changed since this proposal was built")

    matched = _match(state["next_actions"], proposal)
    if matched is None:
        raise ProposalRejected(
            f"{proposal.get('type')} is not an allowed next action for this run"
        )

    if matched["type"] == "create_attempt":
        return _create_attempt(
            connection,
            run=run,
            task=task,
            plan=matched,
            proposal=proposal,
            actor=actor,
            job_factory=job_factory,
        )
    if matched["type"] == "request_review":
        return _request_review(connection, run=run, task=task, plan=matched, actor=actor)
    if matched["type"] == "request_input":
        return _request_input(
            connection, run=run, task=task, plan=matched, proposal=proposal, actor=actor
        )
    if matched["type"] == "complete_run":
        return _complete_run(connection, run=run, task=task, reason=matched["reason"], actor=actor)
    return _fail_run(
        connection,
        run=run,
        task=task,
        failure_class=matched["failure_class"],
        reason=matched["reason"],
        actor=actor,
    )


def report_blocked(
    connection: Any,
    *,
    run_id: uuid.UUID,
    token: int,
    reason: str,
    detail: dict[str, Any] | None = None,
    actor: str = "controller",
) -> dict[str, Any]:
    """Record that the Run cannot be advanced with what it contains.

    A Controller that cannot build a step's input must not keep that to itself:
    the Task would stay ACTIVE while nothing happens. The reason is recorded once
    per input revision, so repeated evaluation passes do not fill the feed, and
    the Task leaves this state as soon as a proposal is accepted.
    """
    run, task = _locked_run_and_task(connection, run_id)
    _check_lease(run, token)
    if task["status"] in tasks.TASK_TERMINAL:
        return {"applied": None, "reason": "the task has already finished"}
    last = connection.execute(
        "SELECT payload FROM platform_events WHERE aggregate_type = 'task' "
        "AND aggregate_id = %s AND type = 'task.blocked' ORDER BY cursor DESC LIMIT 1",
        (str(task["id"]),),
    ).fetchone()
    recorded = (last or {}).get("payload") or {}
    if (
        task["status"] == "BLOCKED"
        and recorded.get("reason") == reason
        and recorded.get("input_revision") == task["input_revision"]
    ):
        # Already reported for this input; nothing has changed since.
        return {
            "applied": None,
            "reason": "this obstruction is already recorded",
            "task_status": task["status"],
            "task_revision": task["revision"],
        }
    updated = tasks._touch_task(connection, task_id=task["id"], status="BLOCKED")
    tasks.record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.blocked",
        actor=actor,
        payload={
            "reason": reason,
            "run_id": str(run_id),
            "input_revision": task["input_revision"],
            **(detail or {}),
        },
        correlation_id=str(task["id"]),
        causation_id=str(run_id),
    )
    return {
        "applied": "report_blocked",
        "task_status": updated["status"],
        "task_revision": updated["revision"],
    }


def _match(allowed: list[dict[str, Any]], proposal: dict[str, Any]) -> dict[str, Any] | None:
    for option in allowed:
        if option["type"] != proposal.get("type"):
            continue
        if option["type"] in {"complete_run", "fail_run"}:
            return option
        if option["step_key"] != proposal.get("step_key"):
            continue
        if proposal.get("cycle") is not None and option["cycle"] != proposal["cycle"]:
            continue
        if (
            proposal.get("attempt_number") is not None
            and option.get("attempt_number") != proposal["attempt_number"]
        ):
            continue
        return option
    return None


def _create_attempt(
    connection: Any,
    *,
    run: dict[str, Any],
    task: dict[str, Any],
    plan: dict[str, Any],
    proposal: dict[str, Any],
    actor: str,
    job_factory: Callable[..., dict[str, Any]] | None,
) -> dict[str, Any]:
    if job_factory is None:
        raise ProposalRejected("this Gateway cannot create executions right now")
    workflow = workflows.get(run["workflow_id"])
    definition = workflow.step(plan["step_key"])
    parameters = dict(proposal.get("parameters") or {})
    inputs = _resolve_inputs(connection, run=run, task=task, proposal=proposal)
    verification_target = None
    if definition.verification:
        # A verification must be pointed at what this Run produced and at the
        # criteria the request was accepted with. The Controller proposes them;
        # the Gateway is what makes them binding, and records the one artifact
        # this attempt is the verification of.
        verification_target = _bind_verification(
            connection,
            run=run,
            task=task,
            definition=definition,
            parameters=parameters,
            inputs=inputs,
        )
    now = tasks.utcnow()
    attempt_number = plan.get("attempt_number", 1)

    step = connection.execute(
        """
        INSERT INTO workflow_steps (
            id, run_id, logical_key, cycle, position, stage_key, kind, action,
            agent_binding, output_contract, status, created_at, updated_at
        ) VALUES (%s, %s, %s, %s,
                  (SELECT coalesce(max(position), 0) + 1 FROM workflow_steps WHERE run_id = %s),
                  %s, 'action', %s, %s, %s, 'RUNNING', %s, %s)
        ON CONFLICT (run_id, logical_key, cycle) DO NOTHING
        RETURNING *
        """,
        (
            tasks._new_id(),
            run["id"],
            plan["step_key"],
            plan["cycle"],
            run["id"],
            definition.stage,
            definition.action,
            definition.agent,
            definition.output_contract,
            now,
            now,
        ),
    ).fetchone()
    if step is None:
        if attempt_number == 1:
            # A duplicate proposal for a step that already exists, not a retry.
            raise ProposalRejected("this step and cycle already exists for the run")
        # A retry runs the same step again: the attempt number is what must be
        # unique, so a repeated retry proposal cannot double the execution.
        step = connection.execute(
            "SELECT * FROM workflow_steps WHERE run_id = %s AND logical_key = %s "
            "AND cycle = %s FOR UPDATE",
            (run["id"], plan["step_key"], plan["cycle"]),
        ).fetchone()
        connection.execute(
            "UPDATE workflow_steps SET status = 'RUNNING', updated_at = %s WHERE id = %s",
            (now, step["id"]),
        )

    # What the requester allowed and what this step asked for, whichever is
    # tighter: a Task submitted with a one-second budget does not get the
    # executor's default because the proposal said nothing about it.
    limits = _effective_limits(
        tasks.frozen_request(connection, task).get("limits") or {},
        proposal.get("limits") or {},
    )
    job = job_factory(
        connection=connection,
        action=definition.action,
        project_id=task["project_id"],
        environment=task["environment"],
        parameters=parameters,
        limits=limits,
        idempotency_key=(
            f"run-{run['id']}-{plan['step_key']}-{plan['cycle']}-{attempt_number}"
        ),
    )
    attempt_id = tasks._new_id()
    input_digest, _ = tasks.canonical_digest(
        {"action": definition.action, "parameters": parameters, "inputs": inputs}
    )
    connection.execute(
        """
        INSERT INTO step_attempts (
            id, step_id, attempt_number, job_id, status, input_manifest,
            execution_snapshot, created_at, started_at
        ) VALUES (%s, %s, %s, %s, 'RUNNING', %s, %s, %s, %s)
        """,
        (
            attempt_id,
            step["id"],
            attempt_number,
            job["id"],
            json.dumps(
                {
                    "action": definition.action,
                    "parameters": parameters,
                    "input_revision": run["input_revision"],
                    "objective": task["objective"],
                    "acceptance_criteria": inputs["acceptance_criteria"],
                    "input_artifacts": inputs["artifacts"],
                    "verification_target": verification_target,
                    "feedback": inputs["feedback"],
                    "digest": input_digest,
                },
                default=str,
            ),
            json.dumps(
                {
                    "workflow_id": run["workflow_id"],
                    "workflow_version": run["workflow_version"],
                    "agent_binding": definition.agent,
                    "environment": task["environment"],
                    "limits": limits,
                    "requested_limits": proposal.get("limits") or {},
                },
                default=str,
            ),
            now,
            now,
        ),
    )
    connection.execute(
        "UPDATE jobs SET task_id = %s, attempt_id = %s, actor_id = %s WHERE id = %s",
        (task["id"], attempt_id, actor, job["id"]),
    )
    updated = tasks._touch_task(
        connection,
        task_id=task["id"],
        status="ACTIVE",
        stage_key=definition.stage,
        active_run_id=run["id"],
    )
    tasks.consume_instructions(connection, task_id=task["id"], by=str(attempt_id))
    starting_over = plan["cycle"] > 1 and plan["step_key"] in {
        workflow.entry_step,
        workflow.work_producing_step,
    }
    if attempt_number == 1 and (definition.revision_entry or starting_over):
        # One revision of the work, counted once for the whole Run: a step the work
        # goes back to, or the Workflow starting over from planning or from producing
        # the change again. A request that keeps sending the Run back spends the same
        # budget as one that keeps asking for fixes; re-verifying a fix is part of that
        # revision, not another one.
        connection.execute(
            "UPDATE workflow_runs SET revision_cycles = revision_cycles + 1, "
            "updated_at = %s WHERE id = %s",
            (now, run["id"]),
        )
    tasks.record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.attempt_created",
        actor=actor,
        payload={
            "run_id": str(run["id"]),
            "step_key": plan["step_key"],
            "cycle": plan["cycle"],
            "action": definition.action,
            "agent": definition.agent,
            "job_id": str(job["id"]),
            "attempt_id": str(attempt_id),
        },
        correlation_id=str(task["id"]),
        occurred_at=now,
    )
    return {
        "applied": "create_attempt",
        "run_id": str(run["id"]),
        "step_key": plan["step_key"],
        "cycle": plan["cycle"],
        "attempt_number": attempt_number,
        "attempt_id": str(attempt_id),
        "job_id": str(job["id"]),
        "task_revision": updated["revision"],
    }


def latest_change(connection: Any, run_id: uuid.UUID) -> dict[str, Any] | None:
    """The most recent code change this Run produced, and what kind of result it is.

    A `code-change-report` is included because it is what the Run produced: the
    progression has to notice it rather than look past it. Whether it can be verified
    is a separate question, answered where that matters.
    """
    return connection.execute(
        "SELECT id, digest, kind FROM artifacts WHERE run_id = %s "
        "AND kind IN ('code-change', 'code-change-report') "
        "ORDER BY created_at DESC LIMIT 1",
        (run_id,),
    ).fetchone()


def _produced_under_current_request(
    connection: Any, *, artifact_id: Any, since: int
) -> bool:
    """Whether the execution that produced this artifact answers the current request.

    It does when it ran at or after the version that last changed what is asked for.
    """
    row = connection.execute(
        """
        SELECT 1
          FROM artifacts art
          JOIN step_attempts a ON a.id = art.producer_attempt_id
         WHERE art.id = %s
           AND (a.input_manifest->>'input_revision')::int >= %s
         LIMIT 1
        """,
        (artifact_id, since),
    ).fetchone()
    return row is not None


def _bind_verification(
    connection: Any,
    *,
    run: dict[str, Any],
    task: dict[str, Any],
    definition: workflows.Step,
    parameters: dict[str, Any],
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """Refuse a verification that is not aimed at this Run's current work.

    A pass only means something about what it actually examined, so the
    verification must be given the newest change, be told which execution
    produced it, and cover every criterion the Run is judged against.
    """
    source = parameters.get("source_worker_job_id")
    if not source:
        raise ProposalRejected("a verification must name the execution it verifies")
    given = [uuid.UUID(item["artifact_id"]) for item in inputs["artifacts"]]
    newest = latest_change(connection, run["id"])
    if newest is None:
        raise ProposalRejected("there is no change for this verification to examine")
    if newest["kind"] != "code-change":
        # A report about a change is not a change: without the patch that produces it
        # there is nothing a verification could rebuild, and nothing a pass could be
        # about. The work has to be done again instead.
        raise ProposalRejected(
            "the latest result is a report about a change, not a change that can be "
            "rebuilt and verified"
        )
    if newest["id"] not in given:
        raise ProposalRejected(
            "the verification must be given the latest change this run produced"
        )
    if not _produced_under_current_request(
        connection,
        artifact_id=newest["id"],
        since=tasks.requirements_revision_of(
            connection, task["id"], int(task["input_revision"])
        ),
    ):
        # The change was produced for a version of the request that has been
        # replaced. Verifying it would judge work against conditions it was never
        # given, so the work has to be done again first.
        raise ProposalRejected(
            "the latest change was produced for an earlier version of this request"
        )
    produced = connection.execute(
        """
        SELECT j.worker_job_id
          FROM artifacts a
          JOIN step_attempts att ON att.id = a.producer_attempt_id
          JOIN jobs j ON j.id = att.job_id
         WHERE a.run_id = %s AND a.id = %s
        """,
        (run["id"], newest["id"]),
    ).fetchall()
    produced_by = {row["worker_job_id"] for row in produced if row["worker_job_id"]}
    if source not in produced_by:
        raise ProposalRejected(
            "the verification input must be an artifact produced by the execution "
            "it names"
        )
    required = {
        _normalise(item)
        for item in (inputs["acceptance_criteria"] or [])
        if str(item).strip()
    }
    required |= _acceptance_from(connection, run=run, definition=definition)
    offered = {
        _normalise(item)
        for item in (parameters.get("acceptance_criteria") or [])
        if str(item).strip()
    }
    missing = sorted(required - offered)
    if missing:
        raise ProposalRejected(
            "the verification must cover every acceptance criterion: "
            + ", ".join(missing)
        )
    # The one artifact this attempt verifies. A report about anything else is not
    # evidence about this change.
    return {
        "artifact_id": str(newest["id"]),
        "digest": newest["digest"],
        "source_worker_job_id": source,
        "criteria": sorted(required),
    }


def requirements_revision(connection: Any, run: dict[str, Any]) -> int:
    """The last version of the request that changed what is being asked for.

    A revision that only changes a limit or adds a reference supersedes nothing a
    person said: work, specifications, answers and corrections from before it still
    stand. Comparing against this rather than against the raw revision number is
    what keeps such a revision from quietly discarding them — or from sending work
    that already answers the request round again.
    """
    return tasks.requirements_revision_of(
        connection, run["task_id"], int(run["input_revision"])
    )


def _acceptance_from(
    connection: Any, *, run: dict[str, Any], definition: workflows.Step
) -> set[str]:
    """The criteria a declared `acceptance_from` reference adds to the check."""
    reference = definition.acceptance_from
    if not reference or "." not in reference:
        return set()
    producer, output = reference.split(".", 1)
    row = connection.execute(
        "SELECT manifest, input_revision FROM artifacts WHERE run_id = %s "
        "AND manifest->>'produced_by_step' = %s ORDER BY created_at DESC LIMIT 1",
        (run["id"], producer),
    ).fetchone()
    if row is None:
        raise ProposalRejected(
            f"{reference} has not been produced, so its acceptance criteria are unknown"
        )
    if row["input_revision"] is not None and int(row["input_revision"]) < (
        requirements_revision(connection, run)
    ):
        # The specification was written before the request last changed what it asks
        # for. What it asked for then is not what this Run is judged by now —
        # requiring it would make a correct answer to the current request impossible
        # to verify — so what the Task itself states is what counts.
        return set()
    content = (row["manifest"] or {}).get("content") or {}
    criteria = content.get("acceptance_criteria") or []
    return {
        _normalise(item if not isinstance(item, dict) else item.get("criterion", ""))
        for item in criteria
        if str(item).strip()
    }


def _normalise(value: Any) -> str:
    """One criterion as its identity — the same rule the whole platform uses.

    Layout is normalised and case is kept: `userID` and `userid` can be two
    different requirements, and a verdict about one is not one about the other.
    """
    return tasks.criterion_text(value)


def _resolve_inputs(
    connection: Any,
    *,
    run: dict[str, Any],
    task: dict[str, Any],
    proposal: dict[str, Any],
) -> dict[str, Any]:
    """Bind the handoff to artifacts this Run actually produced.

    An input artifact must belong to this Run and Project: naming an id from
    another Task cannot pull its content into this execution.
    """
    requested = proposal.get("input_artifact_ids") or []
    resolved: list[dict[str, Any]] = []
    for artifact_id in requested:
        try:
            parsed = uuid.UUID(str(artifact_id))
        except ValueError as invalid:
            raise ProposalRejected(f"{artifact_id} is not an artifact id") from invalid
        row = connection.execute(
            "SELECT id, kind, digest, project_id, run_id FROM artifacts WHERE id = %s",
            (parsed,),
        ).fetchone()
        if row is None or row["run_id"] != run["id"] or row["project_id"] != task["project_id"]:
            raise ProposalRejected(f"{artifact_id} is not an artifact of this run")
        resolved.append(
            {
                "artifact_id": str(row["id"]),
                "kind": row["kind"],
                "digest": row["digest"],
            }
        )
    revision = connection.execute(
        "SELECT acceptance_criteria FROM task_input_revisions WHERE task_id = %s AND revision = %s",
        (task["id"], run["input_revision"]),
    ).fetchone()
    return {
        "artifacts": resolved,
        "acceptance_criteria": (revision or {}).get("acceptance_criteria") or [],
        "feedback": proposal.get("feedback"),
    }


def _request_review(
    connection: Any, *, run: dict[str, Any], task: dict[str, Any], plan: dict[str, Any], actor: str
) -> dict[str, Any]:
    now = tasks.utcnow()
    workflow = workflows.get(run["workflow_id"])
    definition = workflow.step(plan["step_key"])
    step = connection.execute(
        """
        INSERT INTO workflow_steps (
            id, run_id, logical_key, cycle, position, stage_key, kind, status,
            created_at, updated_at
        ) VALUES (%s, %s, %s, %s,
                  (SELECT coalesce(max(position), 0) + 1 FROM workflow_steps WHERE run_id = %s),
                  %s, 'human_review', 'WAITING_REVIEW', %s, %s)
        ON CONFLICT (run_id, logical_key, cycle) DO NOTHING
        RETURNING *
        """,
        (
            tasks._new_id(),
            run["id"],
            plan["step_key"],
            plan["cycle"],
            run["id"],
            definition.stage,
            now,
            now,
        ),
    ).fetchone()
    if step is None:
        raise ProposalRejected("this review step already exists for the run")
    target = tasks.deliverable(connection, run["id"], run["input_revision"])
    updated = tasks._touch_task(
        connection, task_id=task["id"], status="WAITING_REVIEW", stage_key=definition.stage
    )
    connection.execute(
        "UPDATE workflow_runs SET status = 'WAITING_REVIEW', updated_at = %s WHERE id = %s",
        (now, run["id"]),
    )
    tasks.record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.review_requested",
        actor=actor,
        payload={
            "run_id": str(run["id"]),
            "step_key": plan["step_key"],
            "target_digest": target["digest"],
            "artifacts": target["target"]["artifacts"],
        },
        correlation_id=str(task["id"]),
        occurred_at=now,
    )
    return {
        "applied": "request_review",
        "run_id": str(run["id"]),
        "target_digest": target["digest"],
        "task_revision": updated["revision"],
    }


def _request_input(
    connection: Any,
    *,
    run: dict[str, Any],
    task: dict[str, Any],
    plan: dict[str, Any],
    proposal: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    questions = proposal.get("questions") or []
    if not questions or not all(
        isinstance(question, dict) and question.get("id") and question.get("text")
        for question in questions
    ):
        raise ProposalRejected("each question needs an id and the text to answer")
    now = tasks.utcnow()
    workflow = workflows.get(run["workflow_id"])
    definition = workflow.step(plan["step_key"])
    step = connection.execute(
        """
        INSERT INTO workflow_steps (
            id, run_id, logical_key, cycle, position, stage_key, kind, status,
            created_at, updated_at
        ) VALUES (%s, %s, %s, %s,
                  (SELECT coalesce(max(position), 0) + 1 FROM workflow_steps WHERE run_id = %s),
                  %s, 'input_request', 'WAITING_INPUT', %s, %s)
        ON CONFLICT (run_id, logical_key, cycle) DO NOTHING
        RETURNING *
        """,
        (
            tasks._new_id(),
            run["id"],
            plan["step_key"],
            plan["cycle"],
            run["id"],
            definition.stage,
            now,
            now,
        ),
    ).fetchone()
    if step is None:
        raise ProposalRejected("this input request already exists for the run")
    request = tasks.open_input_request(
        connection,
        task=task,
        run_id=run["id"],
        step_id=step["id"],
        questions=questions,
        resume_step=plan.get("resume_step"),
        actor=actor,
    )
    # Asking again is a revision of the same work.
    connection.execute(
        "UPDATE workflow_runs SET revision_cycles = revision_cycles + 1, updated_at = %s "
        "WHERE id = %s",
        (now, run["id"]),
    )
    connection.execute(
        "UPDATE workflow_runs SET status = 'WAITING_INPUT', updated_at = %s WHERE id = %s",
        (now, run["id"]),
    )
    return {
        "applied": "request_input",
        "run_id": str(run["id"]),
        "input_request_id": str(request["id"]),
    }


def _complete_run(
    connection: Any, *, run: dict[str, Any], task: dict[str, Any], reason: str, actor: str
) -> dict[str, Any]:
    now = tasks.utcnow()
    target = tasks.deliverable(connection, run["id"], run["input_revision"])
    connection.execute(
        "UPDATE workflow_runs SET status = 'COMPLETED', result = %s, updated_at = %s, "
        "ended_at = %s WHERE id = %s",
        (json.dumps({"reason": reason, "deliverable": target["digest"]}), now, now, run["id"]),
    )
    connection.execute(
        "UPDATE workflow_steps SET status = 'SUCCEEDED', updated_at = %s "
        "WHERE run_id = %s AND kind = 'human_review' AND status = 'WAITING_REVIEW'",
        (now, run["id"]),
    )
    pausing = task["control_state"] in {"PAUSE_REQUESTED", "PAUSED"}
    updated = tasks._touch_task(
        connection,
        task_id=task["id"],
        status="COMPLETED",
        stage_key="done",
        # A finished Task holds nothing back: a pause that outlived the work it
        # was about would offer neither resume nor anything else.
        control_state="ACTIVE" if pausing else None,
        clear_active_run=True,
    )
    if pausing:
        tasks.record_event(
            connection,
            aggregate_type="task",
            aggregate_id=str(task["id"]),
            aggregate_revision=updated["revision"],
            type="task.pause_settled",
            actor=actor,
            payload={
                "run_id": str(run["id"]),
                "outcome": "COMPLETED",
                "reason": "the deliverable was accepted while paused",
            },
            correlation_id=str(task["id"]),
        )
    tasks.record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.completed",
        actor=actor,
        payload={"run_id": str(run["id"]), "reason": reason, "deliverable": target["digest"]},
        correlation_id=str(task["id"]),
        occurred_at=now,
    )
    return {"applied": "complete_run", "run_id": str(run["id"]), "task_revision": updated["revision"]}


def _fail_run(
    connection: Any,
    *,
    run: dict[str, Any],
    task: dict[str, Any],
    failure_class: str,
    reason: str,
    actor: str,
) -> dict[str, Any]:
    now = tasks.utcnow()
    connection.execute(
        "UPDATE workflow_runs SET status = 'FAILED', result = %s, updated_at = %s, "
        "ended_at = %s WHERE id = %s",
        (
            json.dumps({"failure_class": failure_class, "reason": reason}),
            now,
            now,
            run["id"],
        ),
    )
    pausing = task["control_state"] in {"PAUSE_REQUESTED", "PAUSED"}
    updated = tasks._touch_task(
        connection,
        task_id=task["id"],
        status="FAILED",
        # Nothing is being held back once the Run has ended.
        control_state="ACTIVE" if pausing else None,
        clear_active_run=True,
    )
    if pausing:
        tasks.record_event(
            connection,
            aggregate_type="task",
            aggregate_id=str(task["id"]),
            aggregate_revision=updated["revision"],
            type="task.pause_settled",
            actor=actor,
            payload={
                "run_id": str(run["id"]),
                "outcome": "FAILED",
                "reason": "the run ended while a pause was pending",
            },
            correlation_id=str(task["id"]),
        )
    tasks.record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.failed",
        actor=actor,
        payload={"run_id": str(run["id"]), "failure_class": failure_class, "reason": reason},
        correlation_id=str(task["id"]),
        occurred_at=now,
    )
    return {"applied": "fail_run", "run_id": str(run["id"]), "task_revision": updated["revision"]}


# --------------------------------------------------------------------------- #
# Human operations
# --------------------------------------------------------------------------- #


def task_detail(connection: Any, task_id: uuid.UUID) -> dict[str, Any] | None:
    """One Task with the operations the Gateway will actually accept for it."""
    detail = tasks.get_task(connection, task_id)
    if detail is None:
        return None
    row = connection.execute("SELECT * FROM tasks WHERE id = %s", (task_id,)).fetchone()
    commands = available_commands(connection, row)
    return {
        **detail,
        "available_commands": commands["available"],
        "unavailable_commands": commands["unavailable"],
    }


def _effective_limits(
    task_limits: dict[str, Any], step_limits: dict[str, Any]
) -> dict[str, Any]:
    """The intersection of what the Task allows and what the step asked for.

    Every numeric limit is a ceiling, so the smaller of the two is the one that
    holds; a limit only one side names still applies.
    """
    effective = dict(step_limits)
    for name, value in (task_limits or {}).items():
        current = effective.get(name)
        if isinstance(value, (int, float)) and isinstance(current, (int, float)):
            effective[name] = min(current, value)
        elif current is None:
            effective[name] = value
    return effective


def available_commands(connection: Any, task: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """What the caller may actually do now, and why the rest is unavailable.

    Derived from the Gateway's own state, so a control the UI shows always maps
    to an operation the Gateway will accept.
    """
    workflow = workflows.get(task["workflow_id"])
    request = tasks.frozen_request(connection, task)
    available: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []

    def block(command: str, reason: str, detail: str) -> None:
        unavailable.append({"type": command, "reason": reason, "detail": detail})

    startable_now = workflow is not None and workflow.startable
    controller_ready = (
        workflow is None
        or workflow.driver != "controller"
        or tasks.controller_available(connection)
    )
    if task["status"] in {"READY", "DRAFT"}:
        if not startable_now:
            block(
                "start",
                "WORKFLOW_NOT_STARTABLE",
                f"{task['workflow_id']} はまだ実行経路が有効ではありません",
            )
        elif workflow.driver == "gateway" and not request.get("action"):
            block("start", "INPUT_REQUIRED", "実行する action が登録されていません")
        elif not controller_ready:
            block(
                "start",
                "CONTROLLER_UNAVAILABLE",
                "Workflow Controller が応答していないため進行できません",
            )
        else:
            available.append({"type": "start", "label": "開始する"})
    else:
        block("start", "INVALID_STATE", f"状態が {task['status']} です")

    terminal = task["status"] in tasks.TASK_TERMINAL
    control = task["control_state"]
    started = task["status"] in {"ACTIVE", "WAITING_INPUT", "WAITING_REVIEW", "BLOCKED"}
    if control in {"PAUSE_REQUESTED", "PAUSED"} and not terminal:
        available.append({"type": "resume", "label": "再開"})
        block("pause", "ALREADY_PAUSED", "すでに一時停止の要求済みです")
    elif started and control == "ACTIVE":
        available.append({"type": "pause", "label": "一時停止"})
        block("resume", "INVALID_STATE", "一時停止していません")
    else:
        block("pause", "INVALID_STATE", f"状態が {task['status']} です")
        block("resume", "INVALID_STATE", "一時停止していません")

    if not terminal and control != "CANCEL_REQUESTED":
        available.append({"type": "cancel", "label": "中止"})
    else:
        block(
            "cancel",
            "ALREADY_STOPPING" if control == "CANCEL_REQUESTED" else "INVALID_STATE",
            "停止要求済みです" if control == "CANCEL_REQUESTED" else f"状態が {task['status']} です",
        )

    if task["status"] == "WAITING_REVIEW" and task["active_run_id"]:
        target = tasks.deliverable(
            connection, task["active_run_id"], task["input_revision"]
        )
        available.append(
            {
                "type": "accept_deliverable",
                "label": "成果物を受け入れる",
                "target_digest": target["digest"],
            }
        )
        available.append(
            {
                "type": "request_changes",
                "label": "修正を依頼する",
                "target_digest": target["digest"],
                "requires": ["reason"],
            }
        )
    else:
        for command in ("accept_deliverable", "request_changes"):
            block(command, "INVALID_STATE", "確認待ちの成果物がありません")

    # What the Task asks for can be changed while nothing is running: the design
    # stops the current execution first, then applies the new version.
    if terminal:
        block("revise_input", "INVALID_STATE", f"状態が {task['status']} です")
    elif (
        task["active_run_id"]
        and tasks.active_attempt(connection, task["active_run_id"]) is not None
    ):
        block(
            "revise_input",
            "EXECUTION_RUNNING",
            "実行中です。一時停止または中止してから内容を変更してください",
        )
    else:
        available.append({"type": "revise_input", "label": "依頼内容を変更する"})

    controller_driven = workflow is not None and workflow.driver == "controller"
    can_retry = (
        task["status"] == "FAILED"
        and startable_now
        and (controller_driven or bool(request.get("action")))
        and (controller_ready or not controller_driven)
    )
    if can_retry:
        available.append({"type": "retry", "label": "再実行する"})
    elif task["status"] != "FAILED":
        block("retry", "INVALID_STATE", "失敗した依頼にのみ再実行できます")
    elif controller_driven and not controller_ready:
        block(
            "retry",
            "CONTROLLER_UNAVAILABLE",
            "Workflow Controller が応答していないため再実行できません",
        )
    else:
        block("retry", "INPUT_REQUIRED", "実行する action が登録されていません")
    return {"available": available, "unavailable": unavailable}


def pause(connection: Any, *, task: dict[str, Any], actor: str) -> dict[str, Any]:
    """Stop starting new work. Anything already running keeps going."""
    run_id = task["active_run_id"]
    running = tasks.active_attempt(connection, run_id) if run_id else None
    control = "PAUSE_REQUESTED" if running else "PAUSED"
    updated = tasks._touch_task(connection, task_id=task["id"], control_state=control)
    tasks.record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.pause_requested" if running else "task.paused",
        actor=actor,
        payload={
            "run_id": str(run_id) if run_id else None,
            "running_attempt_id": str(running["id"]) if running else None,
        },
        correlation_id=str(task["id"]),
    )
    return {"control_state": control, "task_revision": updated["revision"]}


def resume(connection: Any, *, task: dict[str, Any], actor: str) -> dict[str, Any]:
    updated = tasks._touch_task(connection, task_id=task["id"], control_state="ACTIVE")
    completed = _complete_deferred(connection, task=updated, actor=actor)
    if completed is not None:
        return {
            "control_state": "ACTIVE",
            "status": completed["status"],
            "task_revision": completed["revision"],
        }
    tasks.record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.resumed",
        actor=actor,
        payload={"run_id": str(task["active_run_id"]) if task["active_run_id"] else None},
        correlation_id=str(task["id"]),
    )
    return {"control_state": "ACTIVE", "task_revision": updated["revision"]}


def _complete_deferred(
    connection: Any, *, task: dict[str, Any], actor: str
) -> dict[str, Any] | None:
    """Finish what a pause held back, now that the Task is active again.

    A single action whose execution succeeded while the Task was pausing is only
    completed here, so pausing really did stop the Run from finishing itself.
    """
    run_id = task["active_run_id"]
    if run_id is None or task["status"] in tasks.TASK_TERMINAL:
        return None
    run = connection.execute(
        "SELECT * FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,)
    ).fetchone()
    if run is None or run["workflow_id"] != workflows.SINGLE_ACTION_V1.id:
        return None
    if run["status"] in tasks.RUN_TERMINAL:
        return None
    attempt = connection.execute(
        """
        SELECT a.status, a.input_manifest FROM step_attempts a
          JOIN workflow_steps s ON s.id = a.step_id
         WHERE s.run_id = %s ORDER BY a.created_at DESC LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if attempt is None or attempt["status"] != "SUCCEEDED":
        return None
    if int((attempt["input_manifest"] or {}).get("input_revision") or 0) < (
        tasks.requirements_revision_of(
            connection, task["id"], int(task["input_revision"])
        )
    ):
        # The request changed while this was held back, so what succeeded answered
        # a version nobody is asking for now.
        return None
    result = _complete_run(
        connection, run=run, task=task, reason="output_contract", actor=actor
    )
    return connection.execute(
        "SELECT * FROM tasks WHERE id = %s", (task["id"],)
    ).fetchone() if result else None


def cancel(connection: Any, *, task: dict[str, Any], actor: str, reason: str | None) -> dict[str, Any]:
    """Request a stop. It is only complete once execution is known to have ended."""
    now = tasks.utcnow()
    run_id = task["active_run_id"]
    running = tasks.active_attempt(connection, run_id) if run_id else None
    # A question nobody will answer any more is closed with the Task.
    tasks.close_open_input_requests(connection, task_id=task["id"], reason="cancelled")
    if running is not None and not running["worker_job_id"]:
        # Nothing has been delivered yet. Withdrawing the queued delivery only
        # succeeds while it is still waiting; a delivery already in flight is
        # left to the Worker, and the stop is queued when it is accepted.
        stopped = tasks.cancel_pending_dispatch(connection, job_id=running["job_id"])
        if stopped:
            tasks.terminate_attempt(
                connection,
                attempt_id=running["id"],
                status="CANCELLED",
                failure_class="cancelled_before_dispatch",
                result={"job_state": "CANCELLED", "error": "cancelled before delivery"},
            )
            running = None
    if running is None:
        if run_id:
            connection.execute(
                "UPDATE workflow_runs SET status = 'CANCELLED', result = %s, updated_at = %s, "
                "ended_at = %s WHERE id = %s "
        "AND status NOT IN ('COMPLETED','FAILED','CANCELLED','SUPERSEDED')",
                (json.dumps({"reason": reason or "cancelled by operator"}), now, now, run_id),
            )
        updated = tasks._touch_task(
            connection,
            task_id=task["id"],
            status="CANCELLED",
            control_state="ACTIVE",
            clear_active_run=True,
        )
        tasks.record_event(
            connection,
            aggregate_type="task",
            aggregate_id=str(task["id"]),
            aggregate_revision=updated["revision"],
            type="task.cancelled",
            actor=actor,
            payload={"run_id": str(run_id) if run_id else None, "reason": reason},
            correlation_id=str(task["id"]),
        )
        return {
            "status": "CANCELLED",
            "stopping": False,
            "task_revision": updated["revision"],
            "worker_job_id": None,
        }

    updated = tasks._touch_task(
        connection, task_id=task["id"], control_state="CANCEL_REQUESTED"
    )
    if running["worker_job_id"]:
        # Queued in this transaction, so the request reaches the Worker even if
        # this process ends right after answering.
        tasks.enqueue_worker_command(
            connection,
            job_id=running["job_id"],
            worker_job_id=running["worker_job_id"],
            kind="cancel",
            actor=actor,
        )
    tasks.record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.cancel_requested",
        actor=actor,
        payload={
            "run_id": str(run_id),
            "attempt_id": str(running["id"]),
            "worker_job_id": running["worker_job_id"],
            "reason": reason,
        },
        correlation_id=str(task["id"]),
    )
    # Not cancelled yet: the Worker must confirm the work and its artifacts ended.
    return {
        "status": task["status"],
        "stopping": True,
        "task_revision": updated["revision"],
        "worker_job_id": running["worker_job_id"],
    }


def revise_input(
    connection: Any,
    *,
    task: dict[str, Any],
    actor: str,
    objective: str | None = None,
    acceptance_criteria: list[str] | None = None,
    context_refs: list[str] | None = None,
    parameters: dict[str, Any] | None = None,
    limits: dict[str, int] | None = None,
    reason: str = "",
    origin: str = "operator",
) -> dict[str, Any]:
    """Change what the Task asks for, and make the work answer to the change.

    A verification is evidence about the input it was run against, so a version that
    changes what is asked for withdraws a deliverable waiting for a decision, closes
    the questions asked under the previous one, and leaves the Run for the Controller
    to continue. A version that changes only how the work runs — a limit, a reference
    — asks for the same work, so it disturbs none of that. Nothing already recorded is
    rewritten either way.
    """
    if task["status"] in tasks.TASK_TERMINAL:
        raise tasks.InvalidState("this task has finished")
    run_id = task["active_run_id"]
    running = tasks.active_attempt(connection, run_id) if run_id else None
    if running is not None:
        # The design's rule: stop the current execution first, then apply the new
        # version. Changing the request under a running execution would leave
        # work in flight that answers to nothing.
        raise tasks.InvalidState(
            "stop the running execution before changing what the task asks for"
        )
    workflow = workflows.get(task["workflow_id"])
    frozen = tasks.frozen_request(connection, task)
    if frozen.get("action") and context_refs:
        raise tasks.InvalidState(
            f"{frozen['action']} は parameters で実行内容を決めるため、"
            "context_refs は実行に渡らない"
        )
    if frozen.get("action") and parameters is None:
        # A single action executes its parameters, not its objective: a version
        # that left them as they were would run the previous request again.
        raise tasks.InvalidState(
            f"{frozen['action']} is executed from its parameters; change the request "
            "with revise_input and give the parameters this version should run with"
        )
    if (
        objective is None
        and acceptance_criteria is None
        and context_refs is None
        and parameters is None
        and limits is None
    ):
        # A restart instruction with no field changes still creates a version:
        # the instruction itself is what changed. Nothing to compare here.
        pass
    else:
        current = tasks.current_input(connection, task)
        unchanged = (
            (objective is None or objective == current["objective"])
            and (
                acceptance_criteria is None
                or list(acceptance_criteria) == list(current["acceptance_criteria"])
            )
            and (
                context_refs is None
                or list(context_refs) == list(current["context_refs"])
            )
            and (
                parameters is None
                or parameters == (current["request"] or {}).get("parameters")
            )
            and (limits is None or limits == (current["request"] or {}).get("limits"))
        )
        if unchanged:
            # Recording a version identical to the current one would withdraw a
            # deliverable from review and redo work for no change at all.
            raise tasks.InvalidState("this would not change what the task asks for")
        if parameters is not None and workflow is not None and workflow.driver != "gateway":
            # Nothing in this workflow reads parameters: its steps are built from the
            # objective, the conditions and what people have said. Accepting them
            # would record a version whose instruction reaches no step at all —
            # while superseding what the previous version's instructions asked for.
            raise tasks.InvalidState(
                f"{workflow.id} は目的・完了条件・追加指示で作業内容を決めるため、"
                "parameters は受け付けない（目的か完了条件に書く）"
            )
        if tasks.consolidates_instructions(
            current,
            objective=objective,
            acceptance_criteria=acceptance_criteria,
        ):
            # The request itself now says what those instructions asked for. Sending
            # the same objective back unchanged does not: the instruction would then
            # be marked as acted on without anything having acted on it, and no step
            # would ever be told.
            tasks.consume_instructions(
                connection, task_id=task["id"], by="input_revision"
            )
    revised = tasks.add_input_revision(
        connection,
        task=task,
        actor=actor,
        objective=objective,
        acceptance_criteria=acceptance_criteria,
        context_refs=context_refs,
        parameters=parameters,
        limits=limits,
        origin=origin,
        reason=reason,
    )
    updated = revised["task"]
    if int(
        tasks.requirements_revision_of(
            connection, task["id"], int(revised["input_revision"])
        )
    ) != int(revised["input_revision"]):
        # This version changed how the work runs — a limit, a reference — and not
        # what is asked for. Nothing anyone said was replaced, so the Run keeps what
        # it has: a deliverable stays in review, an open question stays open, and
        # work that already answers the request is not done again.
        return {
            "applied": "revise_input",
            "input_revision": revised["input_revision"],
            "task_status": updated["status"],
            "task_revision": updated["revision"],
        }
    if run_id is not None and workflow is not None and workflow.driver == "gateway":
        # One execution is the whole request here: there is no step to return to,
        # so the Run ends and the Task is ready to be run again for this version.
        # Completing it from the execution that answered the previous version
        # would deliver work nobody asked for any more.
        return _restart_single_action(
            connection, task=updated, run_id=run_id, actor=actor, reason=reason
        )
    tasks.close_open_input_requests(
        connection, task_id=task["id"], reason="input_revised"
    )
    if run_id is not None:
        # A question asked under the old version is not answered any more, so the
        # step that asked it is closed as superseded. Leaving it waiting would
        # strand the Run: nothing could answer it and nothing could follow it.
        connection.execute(
            "UPDATE workflow_steps SET status = 'SUPERSEDED', updated_at = %s "
            "WHERE run_id = %s AND kind = 'input_request' "
            "AND status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'SUPERSEDED')",
            (tasks.utcnow(), run_id),
        )
        updated = _withdraw_review(
            connection, task=updated, run_id=run_id, actor=actor, reason=reason
        )
    return {
        "applied": "revise_input",
        "input_revision": revised["input_revision"],
        "task_status": updated["status"],
        "task_revision": updated["revision"],
    }


def _restart_single_action(
    connection: Any,
    *,
    task: dict[str, Any],
    run_id: uuid.UUID,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """End a single-action Run because the request it was running changed."""
    now = tasks.utcnow()
    connection.execute(
        "UPDATE workflow_runs SET status = 'SUPERSEDED', result = %s, updated_at = %s, "
        "ended_at = %s WHERE id = %s "
        "AND status NOT IN ('COMPLETED','FAILED','CANCELLED','SUPERSEDED')",
        (
            json.dumps({"reason": "superseded_by_input_revision"}),
            now,
            now,
            run_id,
        ),
    )
    updated = tasks._touch_task(
        connection,
        task_id=task["id"],
        status="READY",
        stage_key="intake",
        control_state="ACTIVE",
        clear_active_run=True,
    )
    tasks.record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.run_superseded",
        actor=actor,
        payload={
            "run_id": str(run_id),
            "input_revision": updated["input_revision"],
            "reason": reason or "what the task asks for has changed",
        },
        correlation_id=str(task["id"]),
    )
    return {
        "applied": "revise_input",
        "input_revision": updated["input_revision"],
        "task_status": updated["status"],
        "task_revision": updated["revision"],
    }


def _withdraw_review(
    connection: Any,
    *,
    task: dict[str, Any],
    run_id: uuid.UUID,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Take a deliverable back out of review because the request changed.

    The verification that led to the review was about the previous version, so the
    Run returns to where changes are made rather than keeping an acceptance that
    would apply the old evidence to the new request.
    """
    if task["status"] != "WAITING_REVIEW":
        if task["status"] in {"WAITING_INPUT", "BLOCKED"}:
            # Whatever it was waiting for was asked under the old version.
            return tasks._touch_task(connection, task_id=task["id"], status="ACTIVE")
        return task
    now = tasks.utcnow()
    review = connection.execute(
        "SELECT * FROM workflow_steps WHERE run_id = %s AND kind = 'human_review' "
        "AND status = 'WAITING_REVIEW' ORDER BY position DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if review is not None:
        connection.execute(
            "UPDATE workflow_steps SET status = 'SUPERSEDED', updated_at = %s WHERE id = %s",
            (now, review["id"]),
        )
    connection.execute(
        "UPDATE workflow_runs SET status = 'ACTIVE', updated_at = %s WHERE id = %s",
        (now, run_id),
    )
    updated = tasks._touch_task(connection, task_id=task["id"], status="ACTIVE")
    tasks.record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.review_withdrawn",
        actor=actor,
        payload={
            "run_id": str(run_id),
            "input_revision": updated["input_revision"],
            "reason": reason or "what the task asks for has changed",
        },
        correlation_id=str(task["id"]),
    )
    return updated


def decide(
    connection: Any,
    *,
    task: dict[str, Any],
    kind: str,
    target_digest: str,
    reason: str | None,
    actor: str,
) -> dict[str, Any]:
    """Accept or reject exactly the deliverable the decision names."""
    if task["status"] != "WAITING_REVIEW" or not task["active_run_id"]:
        raise tasks.InvalidState("this task has no deliverable waiting for a decision")
    run = _locked_run(connection, task["active_run_id"])
    target = tasks.deliverable(connection, run["id"], run["input_revision"])
    if target["digest"] != target_digest:
        # The results changed after the decision was formed.
        raise tasks.ArtifactMismatch(target["digest"])
    decision = tasks.record_decision(
        connection,
        task=task,
        run_id=run["id"],
        kind=kind,
        target_digest=target["digest"],
        target=target["target"],
        actor=actor,
        reason=reason,
    )
    now = tasks.utcnow()
    if kind == "accept_deliverable":
        result = _complete_run(
            connection, run=run, task=task, reason="accepted_deliverable", actor=actor
        )
        return {**result, "decision_id": str(decision["id"]), "target_digest": target["digest"]}

    workflow = workflows.get(run["workflow_id"])
    review = connection.execute(
        "SELECT * FROM workflow_steps WHERE run_id = %s AND kind = 'human_review' "
        "ORDER BY position DESC LIMIT 1",
        (run["id"],),
    ).fetchone()
    definition = workflow.steps.get(review["logical_key"]) if review else None
    if definition is None or not definition.on_request_changes:
        raise tasks.InvalidState("this workflow has nowhere to send changes back to")
    connection.execute(
        "UPDATE workflow_steps SET status = 'SUCCEEDED', updated_at = %s WHERE id = %s",
        (now, review["id"]),
    )
    connection.execute(
        "UPDATE workflow_runs SET status = 'ACTIVE', updated_at = %s WHERE id = %s",
        (now, run["id"]),
    )
    updated = tasks._touch_task(connection, task_id=task["id"], status="ACTIVE")
    tasks.record_event(
        connection,
        aggregate_type="task",
        aggregate_id=str(task["id"]),
        aggregate_revision=updated["revision"],
        type="task.changes_requested",
        actor=actor,
        payload={
            "run_id": str(run["id"]),
            "decision_id": str(decision["id"]),
            "target_digest": target["digest"],
            "return_to": definition.on_request_changes,
            "reason": reason,
        },
        correlation_id=str(task["id"]),
    )
    return {
        "applied": "request_changes",
        "run_id": str(run["id"]),
        "decision_id": str(decision["id"]),
        "return_to": definition.on_request_changes,
        "target_digest": target["digest"],
        "task_revision": updated["revision"],
    }
