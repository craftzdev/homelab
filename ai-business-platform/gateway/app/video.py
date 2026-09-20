"""Bounded ComfyUI executor. Only a checked-in workflow can execute on the GPU."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app import tasks

BASE_URL = os.environ.get("COMFYUI_BASE_URL", "").rstrip("/")
DATA_DIR = Path(os.environ.get("VIDEO_DATA_DIR", "/data/videos"))
MAX_BYTES = 128 * 1024 * 1024
MAX_STORED_BYTES = 2 * 1024 * 1024 * 1024
RETENTION_SECONDS = 7 * 86400
LOCK_ID = 734859202
LOG = logging.getLogger(__name__)


class VideoParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    seed: int = Field(default=20260920, ge=0, le=2**32 - 1)
    workflow: str = Field(default="fasth3-5s-v1", pattern=r"^fasth3-5s-v1$")


class VideoLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    timeout_seconds: int = Field(default=900, ge=120, le=1800)


def configured() -> bool:
    return BASE_URL == "https://omen45.tailb6c7d.ts.net:8443"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("ComfyUI redirects are not allowed")


def open_request(route, body=None):
    if not configured():
        raise ValueError("ComfyUI endpoint is not configured")
    request = urllib.request.Request(
        BASE_URL + route,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    # No proxy environment or redirect can redirect generation / artifact traffic.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect()
    ).open(request, timeout=20)


def api(route, body=None):
    with open_request(route, body) as response:
        data = response.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("ComfyUI response exceeds limit")
    return json.loads(data)


def workflow(parameters, job_id):
    params = VideoParameters.model_validate(parameters)
    graph = copy.deepcopy(json.loads(
        Path(__file__).with_name("fasth3-5s-v1.json").read_text()
    ))
    graph["104"]["inputs"]["prompt"] = params.prompt
    graph["15"]["inputs"]["noise_seed"] = params.seed
    graph["92"]["inputs"]["filename_prefix"] = f"Gateway/{uuid.UUID(str(job_id))}"
    return graph


def output_query(output, job_id):
    filename = output.get("filename", "")
    if (output.get("type") != "output" or output.get("subfolder") != "Gateway"
        or Path(filename).name != filename or "\\" in filename
        or not filename.startswith(str(uuid.UUID(str(job_id))) + "_")
        or not filename.endswith(".mp4")):
        raise ValueError("unexpected ComfyUI artifact")
    return urllib.parse.urlencode({"filename": filename, "subfolder": "Gateway", "type": "output"})


def artifact_path(job_id):
    return DATA_DIR / f"{uuid.UUID(str(job_id))}.mp4"


def save_artifact(query, job_id):
    target = artifact_path(job_id)
    temporary = target.with_suffix(".part")
    digest = hashlib.sha256()
    size = 0
    started = time.monotonic()
    try:
        with open_request("/view?" + query) as response, temporary.open("wb") as out:
            if response.headers.get_content_type() != "video/mp4":
                raise ValueError("expected an MP4 video")
            if int(response.headers.get("Content-Length", "0")) > MAX_BYTES:
                raise ValueError("video exceeds size limit")
            while chunk := response.read(65536):
                if not size and chunk[4:8] != b"ftyp":
                    raise ValueError("invalid MP4 header")
                size += len(chunk)
                if size > MAX_BYTES or time.monotonic() - started > 120:
                    raise ValueError("video exceeds transfer limit")
                digest.update(chunk)
                out.write(chunk)
            if size < 16:
                raise ValueError("empty video")
            out.flush()
            os.fsync(out.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"sha256": digest.hexdigest(), "bytes": size, "media_type": "video/mp4"}


def set_result(db, job_id, state, result, prompt_id=None):
    # Same lock order as every other writer: project, then task, then the job.
    owner = db.execute(
        "SELECT project_id, task_id FROM jobs WHERE id=%s", (job_id,)
    ).fetchone()
    if owner is not None:
        db.execute(
            "SELECT id FROM projects WHERE id=%s FOR UPDATE", (owner["project_id"],)
        ).fetchone()
        if owner.get("task_id"):
            db.execute(
                "SELECT id FROM tasks WHERE id=%s FOR UPDATE", (owner["task_id"],)
            ).fetchone()
    db.execute(
        "UPDATE jobs SET state=%s, result=%s, worker_job_id=COALESCE(%s,worker_job_id), "
        "updated_at=NOW() WHERE id=%s AND action='video.generate'",
        (state, json.dumps(result), prompt_id, job_id),
    )
    _project_ledger(db, job_id, state, result)
    db.commit()


# This runner owns video jobs end to end, so it reports its own progress to the
# Task ledger instead of going through the Worker callback route.
LEDGER_EVENTS = {"RUNNING": "started", "SUCCEEDED": "completed", "FAILED_FINAL": "failed"}


def _project_ledger(db, job_id, state, result):
    if state not in LEDGER_EVENTS and state != "NEEDS_REVIEW":
        return
    job = db.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
    if job is None or job.get("attempt_id") is None:
        return
    if state == "NEEDS_REVIEW":
        # The remote outcome is unknown; never report it as finished either way.
        tasks.project_blocked(
            db,
            job=job,
            reason=result.get("error", "result_unknown"),
            detail={"resubmit_safe": result.get("resubmit_safe")},
            actor="video-runner",
        )
        return
    tasks.project_worker_event(
        db,
        job=job,
        event_type=LEDGER_EVENTS[state],
        data=result,
        occurred_at=datetime.now(timezone.utc),
        actor="video-runner",
    )


def cleanup():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    size = 0
    for item in DATA_DIR.iterdir():
        if item.suffix not in {".mp4", ".part"} or item.is_symlink():
            continue
        try:
            uuid.UUID(item.stem)
        except ValueError:
            continue
        stat = item.stat()
        if now - stat.st_mtime > RETENTION_SECONDS:
            item.unlink()
        else:
            size += stat.st_size
    return size


def tick(db):
    """Called under a session advisory lock; process at most one GPU job at a time."""
    used = cleanup()
    row = db.execute(
        "SELECT * FROM jobs WHERE action='video.generate' AND state IN "
        "('VIDEO_SUBMITTING','RUNNING','QUEUED') "
        "ORDER BY CASE WHEN state='QUEUED' THEN 1 ELSE 0 END,created_at LIMIT 1"
    ).fetchone()
    db.commit()
    if row is None:
        return
    job_id = row["id"]
    if row["state"] == "VIDEO_SUBMITTING":
        # Crash between remote acceptance and local commit: NEVER blindly resubmit.
        set_result(db, job_id, "NEEDS_REVIEW", {
            "error": "submission_outcome_unknown", "resubmit_safe": False,
            "message": "Check ComfyUI history for this Gateway job ID before retrying.",
        })
        return
    payload = row["input"]["payload"]
    timeout = VideoLimits.model_validate(payload.get("limits", {})).timeout_seconds
    elapsed = (datetime.now(timezone.utc) - row["created_at"]).total_seconds()
    if elapsed > timeout:
        set_result(db, job_id, "NEEDS_REVIEW" if row["worker_job_id"] else "FAILED_FINAL", {
            "error": "video_deadline_exceeded", "remote_job_may_continue": bool(row["worker_job_id"]),
            "comfy_prompt_id": row["worker_job_id"],
        })
        return
    if row["state"] == "QUEUED":
        if used > MAX_STORED_BYTES - MAX_BYTES:
            set_result(db, job_id, "FAILED_FINAL", {"error": "video_storage_capacity_reached"})
            return
        queue = api("/queue")
        if queue.get("queue_running") or queue.get("queue_pending"):
            return  # Do not interrupt or jump ahead of user-owned jobs.
        graph = workflow(payload["parameters"], job_id)
        set_result(db, job_id, "VIDEO_SUBMITTING", {"phase": "submitting"})
        try:
            submitted = api("/prompt", {"prompt": graph, "client_id": f"gateway-{job_id}"})
            prompt_id = str(uuid.UUID(submitted["prompt_id"]))
        except urllib.error.HTTPError as error:
            # Validation rejection is definitive; upstream/server failures aren't.
            definitive = error.code == 400
            set_result(db, job_id, "FAILED_FINAL" if definitive else "NEEDS_REVIEW", {
                "error": "workflow_rejected" if definitive else "submission_outcome_unknown",
                "resubmit_safe": definitive,
            })
            return
        except Exception:
            set_result(db, job_id, "NEEDS_REVIEW", {
                "error": "submission_outcome_unknown", "resubmit_safe": False,
            })
            return
        set_result(db, job_id, "RUNNING", {"phase": "generating", "comfy_prompt_id": prompt_id}, prompt_id)
        return

    prompt_id = str(uuid.UUID(row["worker_job_id"]))
    history = api("/history/" + prompt_id).get(prompt_id)
    if not history:
        return
    status = history.get("status", {})
    if status.get("status_str") == "error":
        set_result(db, job_id, "FAILED_FINAL", {"error": "comfy_execution_failed", "comfy_prompt_id": prompt_id})
        return
    if not status.get("completed"):
        return
    try:
        outputs = history["outputs"]["92"]["images"]
        if len(outputs) != 1:
            raise ValueError("expected one video")
        query = output_query(outputs[0], job_id)
        artifact = save_artifact(query, job_id)
    except (ValueError, KeyError, TypeError):
        set_result(db, job_id, "FAILED_FINAL", {"error": "invalid_video_artifact", "comfy_prompt_id": prompt_id})
        return
    set_result(db, job_id, "SUCCEEDED", {
        "comfy_prompt_id": prompt_id, "workflow": "fasth3-5s-v1",
        "width": 896, "height": 512, "frames": 124, "fps": 24,
        "artifact": {**artifact, "url": f"https://gateway.craftz.dev/v1/jobs/{job_id}/video",
                     "authentication_required": True, "retention_days": 7},
        "review": {"url": BASE_URL + "/view?" + query, "tailnet_only": True,
                   "signed": False, "source": "comfyui"},
    })


class VideoRunner:
    def __init__(self, pool):
        self.pool = pool
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="comfy-video", daemon=True)

    def run_once(self):
        with self.pool.connection() as db:
            acquired = db.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (LOCK_ID,)).fetchone()["acquired"]
            db.commit()
            if acquired:
                try:
                    tick(db)
                finally:
                    db.rollback()
                    db.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
                    db.commit()

    def run(self):
        while not self.stop.is_set():
            try:
                self.run_once()
            except Exception:
                # Never log prompts, credentials, or untrusted upstream exception text.
                LOG.warning("Video reconciliation unavailable; retrying without resubmission")
            self.stop.wait(3)
