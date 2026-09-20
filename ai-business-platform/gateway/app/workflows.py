"""Typed Workflow definitions.

The effective progression rules live in code and typed definitions, never in
prose: editing a Markdown profile must not be able to introduce a new state
transition. Definitions are validated at import time so an unreachable step,
an unknown transition target or an unbounded cycle fails fast instead of
stalling a Run in production.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# Kanban columns. A Workflow may only place a step in one of these stages.
STAGE_KEYS: tuple[str, ...] = (
    "intake",
    "planning",
    "implementation",
    "qa",
    "analysis",
    "content_creation",
    "editorial",
    "review",
    "done",
)

StepKind = Literal["action", "human_review", "input_request"]

# Which column a single action belongs to when it runs on its own. Only used by
# single-action-v1, where the Workflow carries no planning of its own.
ACTION_STAGES: dict[str, str] = {
    "product.plan": "planning",
    "code.build": "implementation",
    "code.fix": "implementation",
    "video.generate": "implementation",
    "qa.review": "qa",
    "test.run": "qa",
    "growth.plan": "analysis",
    "analytics.read": "analysis",
    "stripe.read": "analysis",
    "browser.research": "analysis",
    "content.draft": "content_creation",
}
DEFAULT_ACTION_STAGE = "implementation"


def action_stage(action: str) -> str:
    return ACTION_STAGES.get(action, DEFAULT_ACTION_STAGE)


# Bare handoff references the Run resolves from its own artifacts. Anything else
# must name a step output, so a typo cannot silently mean "nothing".
RUN_REFERENCES: tuple[str, ...] = (
    "latest_code_change",
    "latest_report",
    "latest_artifact",
    "task_input",
)


@dataclass(frozen=True)
class Step:
    key: str
    stage: str
    kind: StepKind = "action"
    action: str | None = None
    agent: str | None = None
    output_contract: str | None = None
    # Named outputs later steps may consume, so "plan.prd" is checked against
    # what plan actually declares rather than against the step name alone.
    outputs: tuple[str, ...] = field(default_factory=tuple)
    decision: str | None = None
    input_from: str | None = None
    acceptance_from: str | None = None
    feedback_from: str | None = None
    on_success: str | None = None
    on_fail: str | None = None
    on_inconclusive: str | None = None
    on_request_changes: str | None = None
    optional: bool = False
    # A step that a back edge may return to, bounded by max_revision_cycles.
    revision_entry: bool = False
    # Whether this step is the verification a completion condition may rely on.
    verification: bool = False

    def transitions(self) -> dict[str, str]:
        named = {
            "on_success": self.on_success,
            "on_fail": self.on_fail,
            "on_inconclusive": self.on_inconclusive,
            "on_request_changes": self.on_request_changes,
        }
        return {name: target for name, target in named.items() if target}


@dataclass(frozen=True)
class Workflow:
    id: str
    version: str
    entry_step: str
    completion: Literal["accepted_deliverable", "output_contract"]
    steps: dict[str, Step]
    # Quality gates the completion condition depends on, verified by validate().
    completion_requires: tuple[str, ...] = field(default_factory=tuple)
    max_revision_cycles: int = 2
    max_job_attempts_per_step: int = 3
    # Whether the Gateway can start this Workflow with the code deployed today.
    # A definition nothing can drive is published as a catalog entry, never as a
    # startable route.
    startable: bool = False
    # "gateway" workflows are fully driven by the request that starts them;
    # "controller" workflows need the Workflow Controller to propose each step.
    driver: Literal["gateway", "controller"] = "gateway"
    summary: str = ""
    requires: tuple[str, ...] = field(default_factory=tuple)

    def step(self, key: str) -> Step:
        return self.steps[key]

    @property
    def work_producing_step(self) -> str | None:
        """Where the deliverable is produced from nothing.

        A step that declares the change as its own output. When what a Run produced
        cannot be used — a report about a change rather than a change — this is where
        it starts again, because there is nothing for a revision step to continue.
        """
        for step in self.steps.values():
            if step.kind == "action" and "code_change" in step.outputs:
                return step.key
        return None

    @property
    def work_revision_step(self) -> str | None:
        """Where the work itself is done again when the request changes.

        A question that is superseded and a review that is withdrawn both mean the
        work has to answer something new, so both return here rather than to the
        verification of what was built for the previous version.
        """
        for step in self.steps.values():
            if step.revision_entry and step.kind == "action":
                return step.key
        return None

    @property
    def stages(self) -> tuple[str, ...]:
        ordered: list[str] = []
        for step in self.steps.values():
            if step.stage not in ordered:
                ordered.append(step.stage)
        return tuple(ordered)


class WorkflowDefinitionError(ValueError):
    """A definition that CI and import-time validation must reject."""


def validate(workflow: Workflow) -> None:
    steps = workflow.steps
    for key, step in steps.items():
        if key != step.key:
            raise WorkflowDefinitionError(f"{workflow.id}: step key mismatch for {key}")
        if step.stage not in STAGE_KEYS:
            raise WorkflowDefinitionError(f"{workflow.id}.{key}: unknown stage {step.stage}")
        if step.kind == "action" and not (step.action and step.agent and step.output_contract):
            raise WorkflowDefinitionError(
                f"{workflow.id}.{key}: action steps need action, agent and output_contract"
            )
        if step.kind == "human_review" and not step.decision:
            raise WorkflowDefinitionError(f"{workflow.id}.{key}: human review needs a decision")
        if step.kind == "input_request" and not step.on_success:
            raise WorkflowDefinitionError(
                f"{workflow.id}.{key}: an input request needs the step to resume into"
            )
        for name, target in step.transitions().items():
            if target not in steps:
                raise WorkflowDefinitionError(
                    f"{workflow.id}.{key}.{name}: unknown transition target {target}"
                )
        for name, reference in (
            ("input_from", step.input_from),
            ("acceptance_from", step.acceptance_from),
            ("feedback_from", step.feedback_from),
        ):
            if reference and not _resolvable(reference, steps):
                raise WorkflowDefinitionError(
                    f"{workflow.id}.{key}.{name}: {reference} is not produced by any step"
                )

    if workflow.entry_step not in steps:
        raise WorkflowDefinitionError(f"{workflow.id}: entry step {workflow.entry_step} is missing")

    reachable = _reachable(workflow)
    unreachable = sorted(set(steps) - reachable)
    if unreachable:
        raise WorkflowDefinitionError(f"{workflow.id}: unreachable steps {unreachable}")

    if workflow.max_revision_cycles < 0 or workflow.max_job_attempts_per_step < 1:
        raise WorkflowDefinitionError(f"{workflow.id}: limits must bound execution")
    cycles = _cycles(workflow)
    for cycle in cycles:
        if not any(steps[key].revision_entry for key in cycle):
            raise WorkflowDefinitionError(
                f"{workflow.id}: the cycle {' -> '.join(cycle)} has no declared "
                "revision entry, so its repetitions would be unbounded"
            )
    if cycles and workflow.max_revision_cycles < 1:
        raise WorkflowDefinitionError(f"{workflow.id}: a cycle needs max_revision_cycles >= 1")

    if "qa_pass" in workflow.completion_requires:
        _require_qa_pass_before_acceptance(workflow)
    if workflow.completion == "accepted_deliverable" and "qa_pass" not in workflow.completion_requires:
        raise WorkflowDefinitionError(
            f"{workflow.id}: accepting a deliverable must state the quality gate it requires"
        )


def _require_qa_pass_before_acceptance(workflow: Workflow) -> None:
    """A human review step must only be reachable from a passing verification.

    Acceptance may only follow the success edge of a step declared as the
    verification, so a failing, inconclusive or entirely unverified result cannot
    let the reviewer's "accept" stand in for a verdict nobody reached.
    """
    verifications = {key for key, step in workflow.steps.items() if step.verification}
    if not verifications:
        raise WorkflowDefinitionError(
            f"{workflow.id}: a qa_pass completion needs a step declared as the verification"
        )
    reviews = {key for key, step in workflow.steps.items() if step.kind == "human_review"}
    if not reviews:
        raise WorkflowDefinitionError(
            f"{workflow.id}: accepting a deliverable needs a human review step"
        )
    if workflow.entry_step in reviews:
        raise WorkflowDefinitionError(
            f"{workflow.id}: acceptance cannot be the entry step, because nothing has "
            "been verified yet"
        )
    incoming: dict[str, list[tuple[str, str]]] = {key: [] for key in workflow.steps}
    for key, step in workflow.steps.items():
        for name, target in step.transitions().items():
            incoming[target].append((key, name))
    for review in reviews:
        if not incoming[review]:
            raise WorkflowDefinitionError(
                f"{workflow.id}.{review}: the acceptance step is never reached"
            )
        for source, name in incoming[review]:
            if source not in verifications or name != "on_success":
                raise WorkflowDefinitionError(
                    f"{workflow.id}.{source}.{name}: only a passing verification may "
                    f"reach the acceptance step {review}"
                )


def _resolvable(reference: str, steps: dict[str, Step]) -> bool:
    # "plan.prd" refers to a declared output of the plan step; a bare name must be
    # one the Run can actually resolve from its own artifacts.
    if "." not in reference:
        return reference in RUN_REFERENCES
    producer, output = reference.split(".", 1)
    return producer in steps and output in steps[producer].outputs


def _reachable(workflow: Workflow) -> set[str]:
    seen: set[str] = set()
    queue = [workflow.entry_step]
    while queue:
        key = queue.pop()
        if key in seen:
            continue
        seen.add(key)
        queue.extend(workflow.steps[key].transitions().values())
    return seen


def _cycles(workflow: Workflow) -> list[tuple[str, ...]]:
    """Every cycle reachable from the entry step, as its ordered steps.

    Definitions hold a handful of steps, so an exhaustive walk is cheap and
    finds cycles whichever edge happens to close them.
    """
    cycles: list[tuple[str, ...]] = []
    path: list[str] = []

    def walk(key: str) -> None:
        if key in path:
            cycles.append(tuple(path[path.index(key):]))
            return
        path.append(key)
        for target in workflow.steps[key].transitions().values():
            walk(target)
        path.pop()

    walk(workflow.entry_step)
    return cycles


SINGLE_ACTION_V1 = Workflow(
    id="single-action-v1",
    version="1.0.0",
    entry_step="execute",
    completion="output_contract",
    startable=True,
    driver="gateway",
    summary="Run one bounded action and show its result.",
    steps={
        "execute": Step(
            key="execute",
            # Replaced per Task with the stage of the requested action.
            stage="implementation",
            action="*",
            agent="*",
            output_contract="action-output-v1",
            outputs=("result",),
        )
    },
)

MVP_BUILD_V1 = Workflow(
    id="mvp-build-v1",
    version="1.0.0",
    entry_step="plan",
    completion="accepted_deliverable",
    completion_requires=("qa_pass",),
    startable=True,
    driver="controller",
    summary="Product specifies, Developer implements, QA verifies, a human accepts.",
    requires=("workflow-controller",),
    steps={
        "plan": Step(
            key="plan",
            stage="planning",
            action="product.plan",
            agent="product-manager-v1",
            output_contract="prd-v1",
            outputs=("prd",),
            on_success="implement",
        ),
        "implement": Step(
            key="implement",
            stage="implementation",
            action="code.build",
            agent="software-engineer-v1",
            output_contract="code-change-v1",
            outputs=("code_change",),
            input_from="plan.prd",
            on_success="qa",
        ),
        "qa": Step(
            key="qa",
            stage="qa",
            action="qa.review",
            agent="qa-engineer-v1",
            output_contract="qa-report-v1",
            outputs=("report",),
            verification=True,
            input_from="latest_code_change",
            acceptance_from="plan.prd",
            on_success="review",
            on_fail="fix",
            # An inconclusive verification is not a reason to ask for acceptance:
            # the missing information is requested and QA runs again.
            on_inconclusive="request_input",
        ),
        "request_input": Step(
            key="request_input",
            stage="qa",
            kind="input_request",
            revision_entry=True,
            on_success="qa",
        ),
        "fix": Step(
            key="fix",
            stage="implementation",
            action="code.fix",
            agent="software-engineer-v1",
            output_contract="code-change-v1",
            outputs=("code_change",),
            input_from="latest_code_change",
            feedback_from="qa.report",
            revision_entry=True,
            on_success="qa",
        ),
        "review": Step(
            key="review",
            stage="review",
            kind="human_review",
            decision="accept_deliverable",
            on_request_changes="fix",
        ),
    },
)

WORKFLOWS: dict[str, Workflow] = {
    workflow.id: workflow for workflow in (SINGLE_ACTION_V1, MVP_BUILD_V1)
}

for _workflow in WORKFLOWS.values():
    validate(_workflow)


def get(workflow_id: str) -> Workflow | None:
    return WORKFLOWS.get(workflow_id)


def startable_ids() -> tuple[str, ...]:
    return tuple(key for key, workflow in WORKFLOWS.items() if workflow.startable)


def catalog() -> list[dict[str, object]]:
    return [
        {
            "workflow_id": workflow.id,
            "version": workflow.version,
            "summary": workflow.summary,
            "startable": workflow.startable,
            "driver": workflow.driver,
            "unavailable_reason": None if workflow.startable else "NOT_IMPLEMENTED_YET",
            "requires": list(workflow.requires),
            "stages": list(workflow.stages),
            "completion": workflow.completion,
            "limits": {
                "max_revision_cycles": workflow.max_revision_cycles,
                "max_job_attempts_per_step": workflow.max_job_attempts_per_step,
            },
            "steps": [
                {
                    "key": step.key,
                    "stage": step.stage,
                    "kind": step.kind,
                    "action": step.action,
                    "agent": step.agent,
                    "output_contract": step.output_contract,
                    "decision": step.decision,
                    "transitions": step.transitions(),
                }
                for step in workflow.steps.values()
            ],
        }
        for workflow in WORKFLOWS.values()
    ]
