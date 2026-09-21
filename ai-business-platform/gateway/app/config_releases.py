"""Immutable configuration changes, separate human and GitOps controller identities."""
from __future__ import annotations

import hmac
import json
import os
import uuid
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app import configuration as config, tasks

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS config_releases (
 id UUID PRIMARY KEY,
 draft_id UUID REFERENCES config_drafts(id),
 draft_revision INTEGER,
 source JSONB NOT NULL,
 base_content TEXT NOT NULL,
 content TEXT NOT NULL,
 content_sha256 TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'QUEUED',
 revision INTEGER NOT NULL DEFAULT 1,
 evidence JSONB NOT NULL DEFAULT '{}',
 created_by TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ NOT NULL,
 UNIQUE(draft_id, draft_revision)
);
ALTER TABLE config_releases ADD COLUMN IF NOT EXISTS last_polled_at TIMESTAMPTZ;
"""


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class Intake(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    accepting_jobs: bool
    reason: str = Field(min_length=1, max_length=500)


class Report(Revision):
    state: Literal["REVIEW", "VERIFIED", "MERGED", "BLOCKED"]
    # Only the isolated controller can supply this evidence. No arbitrary HTML.
    evidence: dict[str, Any]


def read(db, release_id, *, lock=False):
    row = db.execute("SELECT * FROM config_releases WHERE id=%s" + (" FOR UPDATE" if lock else ""), (release_id,)).fetchone()
    if not row:
        config.error("RELEASE_NOT_FOUND", "配布候補が見つかりません", 404)
    return row


def event(db, row, actor):
    tasks.record_event(db, aggregate_type="config_release", aggregate_id=str(row["id"]),
                       aggregate_revision=row["revision"], actor=actor, type="config.release_" + row["state"].lower(),
                       payload={"content_sha256": row["content_sha256"], "state": row["state"]})


def transition(db, row, state, actor, evidence=None):
    result = db.execute("UPDATE config_releases SET state=%s,revision=revision+1,evidence=%s,updated_at=%s WHERE id=%s RETURNING *",
                        (state, json.dumps(evidence if evidence is not None else row["evidence"]), tasks.utcnow(), row["id"])).fetchone()
    event(db, result, actor)
    return result


def observed(row, inventory):
    """File observation is not runtime consumption, CI success or rollout success."""
    doc = next((d for d in inventory.get("documents", []) if d["id"] == row["source"]["id"]), None)
    return {"status": "UNKNOWN" if doc is None else "MATCH" if doc["sha256"] == row["content_sha256"] else "DIFFERENT",
            "scope": "installed_file", "sha256": doc["sha256"] if doc else None,
            "loaded_sha256": doc.get("loaded_sha256") if doc else None,
            "loaded_match": doc.get("loaded_sha256") == row["content_sha256"] if doc and doc.get("loaded_sha256") else None,
            "observed_at": inventory.get("observed_at"), "runtime_consumed": False}


def commands(row):
    return [
        {"type": "promote", "label": "この版を Git に反映する", "enabled": row["state"] == "VERIFIED",
         "reason": None if row["state"] == "VERIFIED" else "CI・実行試験の合格確認が必要です"},
        {"type": "rollback", "label": "変更前に戻す下書きを作る", "enabled": row["state"] in {"MERGED", "DEPLOYED"},
         "reason": None if row["state"] in {"MERGED", "DEPLOYED"} else "Git 反映後に使用できます"},
    ]


def build_router(*, pool, auth, config_principal: Callable):
    router = APIRouter(prefix="/v1/config", dependencies=[Depends(auth)])

    def editor(x_config_admin_token: str | None = Header(default=None)):
        return config_principal(x_config_admin_token)

    def controller(x_config_controller_token: str | None = Header(default=None)):
        expected = os.environ.get("CONFIG_CONTROLLER_TOKEN", "")
        if len(expected) < 32:
            config.error("CONTROLLER_UNAVAILABLE", "配布コントローラーが未設定です", 503)
        if not hmac.compare_digest(x_config_controller_token or "", expected):
            config.error("CONTROLLER_REQUIRED", "配布コントローラーの認証が必要です", 401)
        return "config-controller"

    @router.post("/workers/{worker_id}/intake", status_code=202)
    def intake(worker_id: str, request: Intake, actor: str = Depends(editor)):
        with pool.connection() as db:
            # Lock the identity, including when the override has not been created.
            worker = db.execute("SELECT logical_id FROM workers WHERE logical_id=%s FOR UPDATE", (worker_id,)).fetchone()
            if not worker:
                config.error("WORKER_NOT_FOUND", "ワーカーが見つかりません", 404)
            current = tasks.worker_override(db, worker_id)
            revision = current["revision"] if current else 0
            if revision != request.expected_revision:
                config.error("REVISION_CONFLICT", "受付設定が更新されています。画面を更新してください", 409)
            row = tasks.set_worker_intake(db, worker_id=worker_id, accepting_jobs=request.accepting_jobs, actor=actor, reason=request.reason)
        return {"worker_id": worker_id, "desired_revision": row["revision"], "desired_accepting_jobs": row["accepting_jobs"], "applied": False}

    @router.get("/releases")
    def releases():
        with pool.connection() as db:
            rows = db.execute("SELECT * FROM config_releases ORDER BY created_at DESC LIMIT 100").fetchall()
        return {"releases": [{k: v for k, v in row.items() if k not in {"content", "base_content"}} for row in rows]}

    @router.get("/releases/{release_id}")
    def detail(release_id: uuid.UUID):
        with pool.connection() as db:
            row = read(db, release_id)
            row["history"] = db.execute("SELECT type,actor,occurred_at AS created_at FROM platform_events WHERE aggregate_type='config_release' AND aggregate_id=%s ORDER BY cursor DESC LIMIT 100", (str(release_id),)).fetchall()
        try:
            inventory = config.fetch_inventory()
        except HTTPException:
            inventory = {}
        row["observation"] = observed(row, inventory)
        row["available_commands"] = commands(row)
        row["diff"] = config.public({**row, "source": row["source"]})["diff"]
        return row

    @router.post("/drafts/{draft_id}/release", status_code=201)
    def create(draft_id: uuid.UUID, request: Revision, actor: str = Depends(editor)):
        with pool.connection() as db:
            draft = config.read_draft(db, draft_id, lock=True)
            config.check_revision(draft, request.expected_revision)
            existing = db.execute("SELECT * FROM config_releases WHERE draft_id=%s AND draft_revision=%s", (draft_id, draft["revision"])).fetchone()
            if existing:
                return existing
            validation = draft["validation"] or {}
            if draft["state"] != "VALIDATED" or not validation.get("passed") or validation.get("content_sha256") != draft["content_sha256"]:
                config.error("VALIDATION_REQUIRED", "保存した版の基本検証が必要です", 409)
            if draft["content"] == draft["base_content"]:
                config.error("NO_CHANGE", "配布する変更がありません", 409)
            now = tasks.utcnow()
            row = db.execute("INSERT INTO config_releases (id,draft_id,draft_revision,source,base_content,content,content_sha256,created_by,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                             (uuid.uuid4(), draft_id, draft["revision"], json.dumps(draft["source"]), draft["base_content"], draft["content"], draft["content_sha256"], actor, now, now)).fetchone()
            event(db, row, actor)
        return row

    @router.post("/releases/{release_id}/promote")
    def promote(release_id: uuid.UUID, request: Revision, actor: str = Depends(editor)):
        with pool.connection() as db:
            row = read(db, release_id, lock=True)
            config.check_revision(row, request.expected_revision)
            if row["state"] != "VERIFIED":
                config.error("VERIFICATION_REQUIRED", "CI・試験の合格確認が必要です", 409)
            return transition(db, row, "PROMOTE_REQUESTED", actor)

    @router.post("/releases/{release_id}/rollback", status_code=201)
    def rollback(release_id: uuid.UUID, request: Revision, actor: str = Depends(editor)):
        # Rollback is a new draft: the same verification/promotion path applies.
        with pool.connection() as db:
            row = read(db, release_id, lock=True)
            config.check_revision(row, request.expected_revision)
            if row["state"] != "MERGED":
                config.error("NOT_MERGED", "Git に反映済みの変更だけを戻せます", 409)
            inventory = config.fetch_inventory()
            if observed(row, inventory)["status"] != "MATCH":
                config.error("BASE_CHANGED", "現在の配置内容がこの版と一致しません。最新の内容から下書きを作成してください", 409)
            key = "rollback-" + str(release_id)
            prior = db.execute("SELECT * FROM config_drafts WHERE created_by=%s AND idempotency_key=%s", (actor, key)).fetchone()
            if prior:
                return config.public(prior)
            source = {**row["source"], "sha256": row["content_sha256"]}
            now = tasks.utcnow()
            draft = db.execute("INSERT INTO config_drafts (id,source_id,source,base_content,base_sha256,content,content_sha256,created_by,idempotency_key,request_hash,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                               (uuid.uuid4(), source["id"], json.dumps(source), row["content"], row["content_sha256"], row["base_content"], config.digest(row["base_content"]), actor, key, config.digest(key), now, now)).fetchone()
            config.revision_event(db, draft, actor, "config.rollback_drafted")
        return config.public(draft)

    @router.get("/controller/work")
    def work(actor: str = Depends(controller)):
        with pool.connection() as db:
            rows = db.execute("WITH candidates AS (SELECT id FROM config_releases WHERE state IN ('QUEUED','REVIEW','VERIFIED','PROMOTE_REQUESTED') ORDER BY last_polled_at NULLS FIRST,created_at LIMIT 100 FOR UPDATE SKIP LOCKED) UPDATE config_releases SET last_polled_at=%s WHERE id IN (SELECT id FROM candidates) RETURNING *", (tasks.utcnow(),)).fetchall()
        return {"releases": rows}

    @router.post("/controller/releases/{release_id}/report")
    def report(release_id: uuid.UUID, request: Report, actor: str = Depends(controller)):
        if len(json.dumps(request.evidence)) > 16_384:
            config.error("EVIDENCE_TOO_LARGE", "検証結果が大きすぎます")
        allowed = {"QUEUED": {"REVIEW", "BLOCKED"}, "REVIEW": {"REVIEW", "VERIFIED", "BLOCKED"},
                   "VERIFIED": {"VERIFIED", "REVIEW", "BLOCKED"}, "PROMOTE_REQUESTED": {"MERGED", "BLOCKED"}}
        with pool.connection() as db:
            row = read(db, release_id, lock=True)
            config.check_revision(row, request.expected_revision)
            if request.state not in allowed.get(row["state"], set()):
                config.error("INVALID_TRANSITION", "配布状態の遷移が不正です", 409)
            if request.state in {"VERIFIED", "MERGED"}:
                proof = request.evidence
                if (proof.get("content_sha256") != row["content_sha256"] or not proof.get("head_sha")
                    or proof.get("checks_passed") is not True or proof.get("runtime_trial_passed") is not True):
                    config.error("EVIDENCE_REQUIRED", "内容の版に対応した CI・実行試験の証跡が必要です", 409)
            if request.state == row["state"] and request.evidence == row["evidence"]:
                return row
            return transition(db, row, request.state, actor, request.evidence)

    return router
