"""Durable dispatch.

A Job is registered and its delivery is queued in one commit; this loop performs
the delivery afterwards. Ending the API process therefore cannot lose an accepted
Job, and every retry reuses the same `dispatch_id`, so a Worker that already took
the work returns the same execution instead of running it twice.

Runs as a thread inside the public process and as its own process
(`python -m app.scheduler`); an advisory lock keeps exactly one active.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app import tasks

LOG = logging.getLogger(__name__)
LOCK_ID = 734859205
POLL_SECONDS = float(os.environ.get("DISPATCH_POLL_SECONDS", "1"))
BATCH = int(os.environ.get("DISPATCH_BATCH", "5"))

# A refused credential says this Gateway was not recognised; it is not a verdict on
# the request. A restored or rotated token makes the identical request acceptable, so
# a refusal of this kind is never definitive: the stop or the dispatch stays queued.
AUTH_REFUSALS = frozenset({401, 403, 407})
# After the bounded backoff, an undelivered Job is reported as needing checking
# instead of being retried silently forever.
UNKNOWN_ATTEMPTS_BEFORE_BLOCKING = len(tasks.DISPATCH_BACKOFF_SECONDS)


class Delivery:
    """How one queued dispatch is sent. Kept injectable for tests."""

    def __init__(self, base_url: str, token: str, *, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def status(self) -> dict[str, Any]:
        """Read the Worker's own report of itself."""
        request = urllib.request.Request(
            f"{self.base_url}/v1/status",
            method="GET",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read(64 * 1024) or b"{}")
        except urllib.error.HTTPError as error:
            raise Rejected(f"worker refused the status request: HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            raise Unknown(f"worker did not answer the status request: {error}") from error
        if not isinstance(body, dict):
            raise Unknown("worker status was not a report")
        return body

    def set_intake(
        self, *, accepting_jobs: bool, revision: int, actor: str, reason: str
    ) -> dict[str, Any]:
        """Apply the Gateway's desired intake state on the Worker."""
        request = urllib.request.Request(
            f"{self.base_url}/v1/runtime/accepting",
            data=json.dumps(
                {
                    "accepting_jobs": accepting_jobs,
                    "revision": revision,
                    "actor": actor,
                    "reason": reason,
                }
            ).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read(64 * 1024) or b"{}")
        except urllib.error.HTTPError as error:
            if error.code == 409:
                # The Worker has a newer revision than this one.
                raise Rejected("the worker already applied a newer intake revision") from error
            raise Unknown(f"worker refused the intake change: HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            raise Unknown(f"worker did not answer the intake change: {error}") from error

    def cancel(self, worker_job_id: str) -> str:
        """Ask the Worker to stop one execution and return what it answered."""
        request = urllib.request.Request(
            f"{self.base_url}/v1/jobs/{urllib.parse.quote(worker_job_id)}/cancel",
            data=b"",
            method="POST",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read(4096) or b"{}")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise Rejected("the worker does not know this execution") from error
            if error.code in AUTH_REFUSALS:
                raise Unknown(
                    f"the worker refused this gateway's credential: HTTP {error.code}"
                ) from error
            if error.code < 500 and error.code not in {408, 429}:
                raise Rejected(f"worker refused the stop: HTTP {error.code}") from error
            raise Unknown(f"worker unavailable: HTTP {error.code}") from error
        except (
            urllib.error.URLError,
            TimeoutError,
            ValueError,
            OSError,
            http.client.HTTPException,
        ) as error:
            raise Unknown(f"worker did not answer the stop: {error}") from error
        # CANCEL_REQUESTED, CANCELLED or NO_EFFECT_ALREADY_FINISHED: the Worker
        # reports completion of the stop through its callback, not here.
        return str(body.get("status") or "UNKNOWN")

    def send(self, dispatch: dict[str, Any]) -> str:
        """Deliver and return the Worker's execution id, or raise.

        `Rejected` means the Worker refused this request and retrying the same
        body cannot change that. Anything else leaves the outcome unknown.
        """
        request = urllib.request.Request(
            f"{self.base_url}/v1/jobs/{dispatch['endpoint']}",
            data=json.dumps(dispatch["payload"]).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status != 202:
                    raise Unknown(f"worker returned HTTP {response.status}")
                body = json.load(response)
            worker_job_id = body["worker_job_id"]
        except urllib.error.HTTPError as error:
            if error.code in AUTH_REFUSALS:
                # Nothing was accepted and nothing about this request was judged:
                # the credential was. It is retried rather than failing the Attempt.
                raise Unknown(
                    f"the worker refused this gateway's credential: HTTP {error.code}"
                ) from error
            if error.code < 500 and error.code not in {408, 429}:
                raise Rejected(f"worker refused the dispatch: HTTP {error.code}") from error
            raise Unknown(f"worker unavailable: HTTP {error.code}") from error
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
        ) as error:
            # A response that stops half way through raises `IncompleteRead`, which
            # is an `http.client.HTTPException` rather than an `OSError`. The request
            # may well have been accepted, so the outcome is unknown either way.
            raise Unknown(f"worker did not answer: {error}") from error
        except (KeyError, TypeError, ValueError) as error:
            raise Unknown(f"worker answer was unreadable: {error}") from error
        if not isinstance(worker_job_id, str) or not worker_job_id:
            raise Unknown("worker answered without an execution id")
        return worker_job_id


class Rejected(RuntimeError):
    """The Worker refused the request itself."""


class Unknown(RuntimeError):
    """The delivery outcome is not established."""


def deliver_commands(pool: Any, delivery: Delivery, *, limit: int = BATCH) -> dict[str, int]:
    """Send queued runtime requests, such as stopping an execution."""
    with pool.connection() as connection:
        claimed = tasks.claim_worker_commands(connection, limit)
        connection.commit()
    counts = {"sent": 0, "refused": 0, "unknown": 0}
    for command in claimed:
        state, error, bucket = "SENT", None, "sent"
        if not delivery.configured():
            # An operator can supply this; a requested stop is not abandoned for it.
            state, error, bucket = "UNKNOWN", "worker access is not configured", "unknown"
        else:
            try:
                answer = delivery.cancel(command["worker_job_id"])
            except Rejected as rejected:
                state, error, bucket = "REFUSED", str(rejected), "refused"
            except Unknown as unknown:
                state, error, bucket = "UNKNOWN", str(unknown), "unknown"
            except Exception as unexpected:  # noqa: BLE001 - a stop is not dropped
                LOG.exception(
                    "Stopping %s failed unexpectedly", command["worker_job_id"]
                )
                state, error, bucket = (
                    "UNKNOWN",
                    f"the stop failed unexpectedly: {unexpected}",
                    "unknown",
                )
            else:
                error = answer
        with pool.connection() as connection:
            tasks.settle_worker_command(
                connection, command=command, state=state, error=error
            )
            connection.commit()
        counts[bucket] += 1
    return counts


def deliver_once(pool: Any, delivery: Delivery, *, limit: int = BATCH) -> dict[str, int]:
    """Send the deliveries that are due and record what each one established."""
    with pool.connection() as connection:
        claimed = tasks.claim_dispatches(connection, limit)
        connection.commit()
    counts = {"accepted": 0, "rejected": 0, "unknown": 0}
    for dispatch in claimed:
        if not delivery.configured():
            _settle(
                pool,
                dispatch,
                state="REJECTED",
                error="worker dispatch is not configured",
                definitive=True,
            )
            counts["rejected"] += 1
            continue
        try:
            worker_job_id = delivery.send(dispatch)
        except Rejected as rejected:
            _settle(pool, dispatch, state="REJECTED", error=str(rejected), definitive=True)
            counts["rejected"] += 1
        except Unknown as unknown:
            _settle(pool, dispatch, state="UNKNOWN", error=str(unknown), definitive=False)
            counts["unknown"] += 1
        except Exception as unexpected:  # noqa: BLE001 - nothing may go unsettled
            # Anything not classified above establishes nothing either, and a
            # dispatch left claimed would be reconciled by a lease timeout instead
            # of being reported. It is settled as unknown, with what happened.
            LOG.exception("Dispatch %s failed unexpectedly", dispatch["dispatch_id"])
            _settle(
                pool,
                dispatch,
                state="UNKNOWN",
                error=f"the delivery failed unexpectedly: {unexpected}",
                definitive=False,
            )
            counts["unknown"] += 1
        else:
            _accept(pool, dispatch, worker_job_id)
            counts["accepted"] += 1
    return counts


def _accept(pool: Any, dispatch: dict[str, Any], worker_job_id: str) -> None:
    """Record the accepted delivery, and honour a stop requested meanwhile.

    The Task is locked before its control state is read, in the same order every
    other writer uses, so a cancellation committing at the same moment cannot be
    missed by both sides.
    """
    with pool.connection() as connection:
        job = connection.execute(
            "SELECT * FROM jobs WHERE id = %s", (dispatch["job_id"],)
        ).fetchone()
        if job is None:
            tasks.settle_dispatch(
                connection,
                dispatch=dispatch,
                state="ACCEPTED",
                worker_job_id=worker_job_id,
            )
            connection.commit()
            return
        connection.execute(
            "SELECT id FROM projects WHERE id = %s FOR UPDATE", (job["project_id"],)
        ).fetchone()
        stopping = None
        if job.get("task_id"):
            stopping = connection.execute(
                "SELECT control_state FROM tasks WHERE id = %s FOR UPDATE",
                (job["task_id"],),
            ).fetchone()
        connection.execute(
            """
            UPDATE jobs
               SET state = CASE WHEN state = 'QUEUED' OR state = 'RECONCILING'
                                THEN 'DISPATCHED' ELSE state END,
                   worker_job_id = COALESCE(worker_job_id, %s),
                   dispatch_id = %s,
                   updated_at = %s
             WHERE id = %s
            """,
            (worker_job_id, dispatch["dispatch_id"], tasks.utcnow(), dispatch["job_id"]),
        )
        tasks.settle_dispatch(
            connection, dispatch=dispatch, state="ACCEPTED", worker_job_id=worker_job_id
        )
        if stopping and stopping["control_state"] == "CANCEL_REQUESTED":
            # The stop was requested before the Worker had an execution to stop;
            # now that it has one, ask for it.
            tasks.enqueue_worker_command(
                connection,
                job_id=job["id"],
                worker_job_id=worker_job_id,
                kind="cancel",
                actor="gateway",
            )
        connection.commit()


def _settle(
    pool: Any,
    dispatch: dict[str, Any],
    *,
    state: str,
    error: str,
    definitive: bool,
) -> None:
    """Record the delivery result, and report it on the Task when it is settled."""
    with pool.connection() as connection:
        job = connection.execute(
            "SELECT * FROM jobs WHERE id = %s", (dispatch["job_id"],)
        ).fetchone()
        if job is None:
            tasks.settle_dispatch(connection, dispatch=dispatch, state="REJECTED", error=error)
            connection.commit()
            return
        # Lock order everywhere: project, then task, then the job row.
        connection.execute(
            "SELECT id FROM projects WHERE id = %s FOR UPDATE", (job["project_id"],)
        ).fetchone()
        if job.get("task_id"):
            connection.execute(
                "SELECT id FROM tasks WHERE id = %s FOR UPDATE", (job["task_id"],)
            ).fetchone()
        tasks.settle_dispatch(connection, dispatch=dispatch, state=state, error=error)
        refused_after_an_earlier_send = (
            definitive and int(dispatch.get("attempts") or 1) > 1
        )
        if refused_after_an_earlier_send:
            # This is not the first time this dispatch was sent. A refusal now — a
            # rotated credential, a withdrawn action — says nothing about whether an
            # earlier send was accepted, and the Worker may be running it. The outcome
            # is unknown, and retrying will not resolve it, so it is reported as
            # needing checking straight away rather than after the backoff.
            definitive = False
            error = (
                "a later delivery was refused, so the outcome of the earlier one is "
                f"unknown: {error}"
            )
            tasks.project_dispatch_outcome(
                connection, job=job, definitive=False, reason=error
            )
        if definitive:
            connection.execute(
                "UPDATE jobs SET state = 'FAILED_FINAL', result = %s, updated_at = %s "
                "WHERE id = %s AND state IN ('QUEUED', 'RECONCILING')",
                (json.dumps({"error": error, "outcome_known": True}), tasks.utcnow(), job["id"]),
            )
            refreshed = connection.execute(
                "SELECT * FROM jobs WHERE id = %s", (job["id"],)
            ).fetchone()
            tasks.project_dispatch_outcome(
                connection, job=refreshed, definitive=True, reason=error
            )
        elif (
            not refused_after_an_earlier_send
            and dispatch["attempts"] >= UNKNOWN_ATTEMPTS_BEFORE_BLOCKING
        ):
            # The bounded backoff is spent: say the outcome is unknown rather
            # than keep a card looking healthy while nothing is running.
            connection.execute(
                "UPDATE jobs SET state = 'RECONCILING', result = %s, updated_at = %s "
                "WHERE id = %s AND state = 'QUEUED'",
                (
                    json.dumps({"error": error, "outcome_known": False}),
                    tasks.utcnow(),
                    job["id"],
                ),
            )
            refreshed = connection.execute(
                "SELECT * FROM jobs WHERE id = %s", (job["id"],)
            ).fetchone()
            tasks.project_dispatch_outcome(
                connection, job=refreshed, definitive=False, reason=error
            )
        connection.commit()


WORKER_POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "15"))
WORKER_LOGICAL_ID = os.environ.get("WORKER_LOGICAL_ID", "ai-business-worker")


def poll_worker(pool: Any, delivery: Delivery, *, logical_id: str = WORKER_LOGICAL_ID) -> dict[str, Any] | None:
    """Push the intake state the Gateway wants, then record what the Worker says."""
    if not delivery.configured():
        return None
    _apply_intake(pool, delivery, logical_id)
    try:
        report = delivery.status()
    except (Rejected, Unknown) as error:
        with pool.connection() as connection:
            tasks.record_worker_report(
                connection, logical_id=logical_id, report=None, error=str(error)
            )
            connection.commit()
        return None
    with pool.connection() as connection:
        tasks.record_worker_report(
            connection, logical_id=report.get("logical_id") or logical_id, report=report
        )
        connection.commit()
    return report


def _apply_intake(pool: Any, delivery: Delivery, logical_id: str) -> None:
    """Send the desired intake state until the Worker reports that revision."""
    with pool.connection() as connection:
        desired = tasks.worker_override(connection, logical_id)
        observed = connection.execute(
            "SELECT intake_revision FROM workers WHERE logical_id = %s", (logical_id,)
        ).fetchone()
        connection.commit()
    if desired is None:
        return
    if observed and observed["intake_revision"] == desired["revision"]:
        return
    try:
        delivery.set_intake(
            accepting_jobs=desired["accepting_jobs"],
            revision=desired["revision"],
            actor=desired["actor"],
            reason=desired["reason"] or "",
        )
    except (Rejected, Unknown) as error:
        # The Worker keeps whatever it applied last; the difference stays visible
        # as desired_revision against intake_revision.
        LOG.warning("Worker intake state not applied yet: %s", error)


class Scheduler:
    """The dispatch loop, held by one process at a time."""

    def __init__(self, pool: Any, delivery: Delivery, *, poll_seconds: float = POLL_SECONDS):
        self.pool = pool
        self.delivery = delivery
        self.poll_seconds = poll_seconds
        self.stop = threading.Event()
        self.last_worker_poll = 0.0
        self.thread = threading.Thread(target=self.run, name="dispatch-scheduler", daemon=True)

    def run_once(self) -> dict[str, int] | None:
        with self.pool.connection() as connection:
            acquired = connection.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired", (LOCK_ID,)
            ).fetchone()["acquired"]
            connection.commit()
            if not acquired:
                return None
            try:
                delivered = deliver_once(self.pool, self.delivery)
                commands = deliver_commands(self.pool, self.delivery)
                if time.monotonic() - self.last_worker_poll >= WORKER_POLL_SECONDS:
                    self.last_worker_poll = time.monotonic()
                    poll_worker(self.pool, self.delivery)
                return {**delivered, **{f"command_{k}": v for k, v in commands.items()}}
            finally:
                connection.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
                connection.commit()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                LOG.warning("Dispatch delivery unavailable; retrying")
            self.stop.wait(self.poll_seconds)


def main() -> None:
    """Run the scheduler as its own process."""
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    logging.basicConfig(
        level=os.environ.get("SCHEDULER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    pool = ConnectionPool(
        os.environ["DATABASE_URL"], min_size=1, max_size=3, kwargs={"row_factory": dict_row}
    )
    pool.wait()
    delivery = Delivery(
        os.environ.get("WORKER_BASE_URL", "").rstrip("/"),
        os.environ.get("WORKER_API_TOKEN", ""),
    )
    scheduler = Scheduler(pool, delivery)
    LOG.info("Dispatch scheduler starting")
    try:
        scheduler.run()
    finally:
        pool.close()


if __name__ == "__main__":
    main()
