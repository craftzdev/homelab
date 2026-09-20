#!/usr/bin/env python3
"""A stand-in Worker for a local end-to-end run.

It implements the Worker's side of the contract — accept a dispatch, report
progress and a result by callback, answer status, apply an intake change and
cancel — without running Codex. Enough to see the Gateway, its scheduler, the
Agent's Workflow Controller and the callback surface actually work together over
HTTP. See docs/task-ledger.md for how to run it.

It reports fixed, obviously synthetic results: it is a test double, never a
substitute for the real Worker in any environment that matters.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("STUB_WORKER_PORT", "9099"))
CALLBACK_URL = os.environ.get("CALLBACK_URL", "http://127.0.0.1:8081/v1/worker-events")
CALLBACK_TOKEN = os.environ["WORKER_CALLBACK_TOKEN"]
WORKER_TOKEN = os.environ["WORKER_API_TOKEN"]

# What to report for each action, in the shape the real executors produce.
PRD = {
    "title": "CSV 取り込み（stub）",
    "slug": "csv-import",
    "problem": "明細を手入力している",
    "target_audience": "個人事業主",
    "value_proposition": "CSV から一括登録できる",
    "features": ["CSV アップロード"],
    "acceptance_criteria": ["正常な CSV から明細を登録できる"],
    "analytics_events": ["csv_imported"],
}
jobs: dict[str, dict] = {}
lock = threading.Lock()
# Intake state the Gateway asked for, as the real Worker keeps it.
intake = {"accepting_jobs": True, "revision": 0}


def build_result(worker_job_id: str) -> dict:
    return {
        "mode": "stub",
        "succeeded": True,
        "project_id": "smoke-product",
        "base_commit": "0" * 40,
        "changed_files": ["app/main.py"],
        "tests": [{"name": "pytest", "passed": True}],
        "patch_digest": uuid.uuid5(uuid.NAMESPACE_URL, worker_job_id).hex * 2,
        "workspace_digest": "c" * 64,
        "change_manifest": [{"path": "app/main.py", "sha256": "d" * 64, "state": "present"}],
        "artifacts": ["changes.patch"],
    }


def qa_result(job: dict) -> dict:
    criteria = job["request"]["parameters"].get("acceptance_criteria") or []
    return {
        "mode": "stub",
        "succeeded": True,
        "report": {
            "verdict": "pass",
            "summary": "すべての条件を確認しました",
            "acceptance_criteria": [
                {"criterion": item, "verdict": "pass", "evidence": "stub log"}
                for item in criteria
            ],
            "risks": [],
            "source_worker_job_id": job["request"]["parameters"].get("source_worker_job_id"),
        },
    }


def result_for(job: dict) -> dict:
    action = job["request"]["action"]
    if action == "product.plan":
        return {"mode": "stub", "succeeded": True, "report": PRD}
    if action == "qa.review":
        return qa_result(job)
    return build_result(job["worker_job_id"])


def callback(job: dict, event_type: str, sequence: int, data: dict) -> None:
    payload = json.dumps(
        {
            "event_id": f"{job['worker_job_id']}:{sequence}",
            "gateway_job_id": job["request"]["gateway_job_id"],
            "dispatch_id": job["request"]["dispatch_id"],
            "worker_job_id": job["worker_job_id"],
            "event_type": event_type,
            "sequence": sequence,
            "occurred_at": "2026-09-20T00:00:00+00:00",
            "data": data,
        }
    ).encode()
    request = urllib.request.Request(
        CALLBACK_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {CALLBACK_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            print(f"callback {event_type} -> HTTP {response.status}", flush=True)
    except urllib.error.HTTPError as error:
        print(f"callback {event_type} refused: HTTP {error.code} {error.read(512)}", flush=True)
    except urllib.error.URLError as error:
        print(f"callback {event_type} failed: {error}", flush=True)


def run_job(job: dict) -> None:
    callback(job, "accepted", 1, {})
    callback(job, "started", 2, {})
    with lock:
        if job.get("cancel_requested"):
            callback(job, "failed", 3, {"error": "cancelled", "cancelled": True})
            job["state"] = "CANCELLED"
            return
        job["state"] = "SUCCEEDED"
    callback(job, "completed", 3, result_for(job))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _authorised(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {WORKER_TOKEN}"

    def _send(self, status: int, body: dict) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if not self._authorised():
            return self._send(401, {"detail": "unauthorized"})
        if self.path == "/v1/status":
            with lock:
                running = sum(1 for job in jobs.values() if job["state"] == "RUNNING")
            return self._send(
                200,
                {
                    "logical_id": "stub-worker",
                    "pool": "default",
                    "instance_id": "stub-1",
                    "accepting_jobs": intake["accepting_jobs"],
                    "intake_revision": intake["revision"],
                    "max_concurrency": 1,
                    "running": running,
                    "queued": 0,
                    "callback_backlog": 0,
                    "actions": ["product.plan", "code.build", "code.fix", "qa.review"],
                },
            )
        return self._send(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if not self._authorised():
            return self._send(401, {"detail": "unauthorized"})
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        cancel = re.fullmatch(r"/v1/jobs/([^/]+)/cancel", self.path)
        if cancel:
            with lock:
                job = jobs.get(cancel.group(1))
                if job is None:
                    return self._send(404, {"detail": "job not found"})
                job["cancel_requested"] = True
                status = (
                    "NO_EFFECT_ALREADY_FINISHED"
                    if job["state"] in {"SUCCEEDED", "FAILED_FINAL", "CANCELLED"}
                    else "CANCEL_REQUESTED"
                )
            return self._send(200, {"worker_job_id": cancel.group(1), "status": status})
        if self.path == "/v1/runtime/accepting":
            body = json.loads(raw)
            revision = int(body.get("revision") or 0)
            with lock:
                if revision <= intake["revision"]:
                    return self._send(409, {"detail": "not a newer intake revision"})
                intake["accepting_jobs"] = bool(body.get("accepting_jobs"))
                intake["revision"] = revision
            print(f"intake {intake}", flush=True)
            return self._send(
                200,
                {
                    "accepting_jobs": intake["accepting_jobs"],
                    "intake_revision": intake["revision"],
                },
            )
        created = re.fullmatch(r"/v1/jobs/([a-z]+)", self.path)
        if not created:
            return self._send(404, {"detail": "not found"})
        request = json.loads(raw)
        with lock:
            for existing in jobs.values():
                if existing["request"]["dispatch_id"] == request["dispatch_id"]:
                    # Deduplicated on dispatch id, like the real Worker.
                    return self._send(
                        202,
                        {"worker_job_id": existing["worker_job_id"], "status": "ACCEPTED"},
                    )
            worker_job_id = f"wjob_{uuid.uuid4().hex}"
            job = {
                "worker_job_id": worker_job_id,
                "request": request,
                "state": "RUNNING",
                "cancel_requested": False,
            }
            jobs[worker_job_id] = job
        print(f"accepted {request['action']} as {worker_job_id}", flush=True)
        threading.Thread(target=run_job, args=(job,), daemon=True).start()
        return self._send(202, {"worker_job_id": worker_job_id, "status": "ACCEPTED"})

    def log_message(self, *_args) -> None:
        return  # The prints above are the interesting part.


if __name__ == "__main__":
    print(f"stub worker listening on {PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
