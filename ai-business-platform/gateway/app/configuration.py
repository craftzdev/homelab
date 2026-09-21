"""Installed configuration and durable drafts. Drafts never change live files."""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from pathlib import PurePosixPath
from typing import Any, Callable, Literal

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException
from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, ConfigDict, Field

from app import tasks

MAX_CONTENT = 65_536
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS config_drafts (
    id UUID PRIMARY KEY,
    source_id TEXT NOT NULL,
    source JSONB NOT NULL,
    base_content TEXT NOT NULL,
    base_sha256 TEXT NOT NULL,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'DRAFT',
    validation JSONB,
    created_by TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(created_by, idempotency_key)
);
CREATE TABLE IF NOT EXISTS config_draft_revisions (
    draft_id UUID NOT NULL REFERENCES config_drafts(id),
    revision INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL,
    validation JSONB,
    actor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(draft_id, revision)
);
"""


def digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def error(code: str, message: str, status: int = 422, **extra: Any):
    raise HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


class Document(BaseModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    component: Literal["agent", "worker"]
    kind: Literal["profile", "capabilities", "schema", "skill", "skill_file", "harness"]
    path: str = Field(max_length=500)
    content: str = Field(max_length=MAX_CONTENT)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    loaded_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    repository: str = Field(max_length=100)
    observed_from: Literal["installed_file"]
    skill_id: str | None = Field(default=None, max_length=100)
    source_mapping: dict[str, str] | None = None


class Component(BaseModel):
    component: Literal["agent", "worker"]
    status: Literal["available", "partial", "unavailable"]
    issues: list[dict[str, str]] = Field(default_factory=list, max_length=200)


class RoleAction(BaseModel):
    action: str = Field(max_length=100)
    enabled: bool
    executor: str = Field(max_length=100)


class Role(BaseModel):
    id: str = Field(max_length=100)
    actions: list[RoleAction] = Field(max_length=100)
    skills: list[str] = Field(max_length=100)
    versions: list[str] = Field(max_length=100)


def fetch_inventory() -> dict[str, Any]:
    base = os.environ.get("WORKER_BASE_URL", "").rstrip("/")
    token = os.environ.get("WORKER_API_TOKEN", "")
    if not base or not token:
        error("INVENTORY_UNAVAILABLE", "設定の取得先が構成されていません", 503)
    request = urllib.request.Request(
        base + "/v1/configuration", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read(4_194_305)
        if len(raw) > 4_194_304:
            raise ValueError("inventory too large")
        report = json.loads(raw)
        if not isinstance(report, dict) or not isinstance(report.get("documents"), list) or len(report["documents"]) > 200:
            raise ValueError("invalid inventory")
        documents = [Document.model_validate(item).model_dump() for item in report["documents"]]
        seen: set[str] = set()
        for document in documents:
            path = PurePosixPath(document["path"])
            if path.is_absolute() or ".." in path.parts or "\\" in document["path"] or "\x00" in document["content"]:
                raise ValueError("invalid document")
            if len(document["content"].encode()) > MAX_CONTENT or digest(document["content"]) != document["sha256"]:
                raise ValueError("invalid digest")
            expected = digest(f"{document['component']}:{document['path']}")
            if expected != document["id"] or document["id"] in seen:
                raise ValueError("invalid identity")
            mapping = document.get("source_mapping")
            if mapping is not None and set(mapping) != {"path", "key"}:
                raise ValueError("invalid source mapping")
            seen.add(document["id"])
        components = report.get("components")
        if not isinstance(components, list) or not 1 <= len(components) <= 2:
            raise ValueError("missing component reports")
        components = [Component.model_validate(item).model_dump() for item in components]
        roles = report.get("roles", [])
        if not isinstance(roles, list) or len(roles) > 100:
            raise ValueError("invalid roles")
        roles = [Role.model_validate(item).model_dump() for item in roles]
    except (OSError, ValueError, TypeError) as failure:
        error("INVENTORY_UNAVAILABLE", "実行環境から設定を取得できません", 502)
    return {
        "documents": documents, "components": components, "roles": roles,
        "observed_at": tasks.utcnow().isoformat(), "scope": "installed_files",
        "activation_supported": False,
    }


def bounded_yaml(value: Any) -> None:
    """Reject aliases/cycles and deep structures before JSON expands them."""
    pending = [(value, 0)]
    seen: set[int] = set()
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > 5000 or depth > 32:
            raise ValueError("YAML structure is too large")
        if isinstance(item, (dict, list)):
            if id(item) in seen:
                raise ValueError("YAML aliases are not supported")
            seen.add(id(item))
            children = item.values() if isinstance(item, dict) else item
            pending.extend((child, depth + 1) for child in children)


def validate_content(document: dict[str, Any], content: str) -> dict[str, Any]:
    """Syntax checks only: scripts are parsed, never executed, and refs never fetched."""
    issues: list[str] = []
    kind, path = document["kind"], document["path"]
    if not content.strip():
        issues.append("本文を入力してください")
    if len(content.encode()) > MAX_CONTENT or "\x00" in content:
        issues.append("UTF-8 で64 KiB以下のテキストを指定してください")
    try:
        if kind == "skill":
            match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", content, re.S)
            if not match:
                raise ValueError("SKILL.md に YAML frontmatter が必要です")
            metadata = yaml.safe_load(match.group(1))
            bounded_yaml(metadata)
            if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", metadata["name"]):
                raise ValueError("Skill の name が不正です")
            if metadata["name"] != document.get("skill_id"):
                raise ValueError("Skill の name と配置先が一致しません")
            if not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
                raise ValueError("Skill の description が必要です")
            if not content[match.end():].strip():
                raise ValueError("Skill の手順本文が必要です")
        elif kind == "schema":
            schema = json.loads(content)
            Draft202012Validator.check_schema(schema)
        elif path.endswith(".json"):
            json.loads(content)
        elif path.endswith((".yaml", ".yml")):
            value = yaml.safe_load(content)
            bounded_yaml(value)
            # Refuse alias cycles and excessively expanded YAML without running
            # any constructors or following external schema references.
            if len(json.dumps(value)) > 1_048_576:
                raise ValueError("展開後の YAML が大きすぎます")
            if kind == "capabilities":
                if not isinstance(value, dict) or not isinstance(value.get("actions"), dict):
                    raise ValueError("actions の対応表が必要です")
                for name, item in value["actions"].items():
                    if not isinstance(name, str) or not isinstance(item, dict):
                        raise ValueError("action の定義が不正です")
                    for key in ("executor", "worker_endpoint", "profile", "profile_version", "schema"):
                        if not isinstance(item.get(key), str) or not item[key]:
                            raise ValueError(f"{name}: {key} が必要です")
                    if not isinstance(item.get("enabled", False), bool):
                        raise ValueError(f"{name}: enabled は真偽値です")
        elif path.endswith(".py"):
            ast.parse(content)
        elif kind == "skill_file" and not path.endswith((".md", ".txt")):
            issues.append("このファイル形式の構文検証は未対応です。適用には専用CIの検証が必要です")
    except (ValueError, TypeError, RecursionError, SyntaxError, SchemaError, yaml.YAMLError):
        issues.append("構文または必須項目が不正です。ファイル形式と定義を確認してください")
    return {
        "passed": not issues, "scope": "syntax_only", "issues": issues,
        "content_sha256": digest(content), "activation_ready": False,
        "pending_checks": ["参照先との整合性", "実行環境での検証", "Git・CI・Release の作成と適用"],
    }


class DraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str = Field(min_length=1, max_length=MAX_CONTENT)


class DraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=MAX_CONTENT)


class ValidateDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


def public(row: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in row.items() if key not in {"request_hash", "idempotency_key"}}
    path = row["source"]["path"]
    result["diff"] = "".join(difflib.unified_diff(
        row["base_content"].splitlines(keepends=True), row["content"].splitlines(keepends=True),
        fromfile=f"installed/{path}", tofile=f"draft/{path}",
    ))
    result["applied"] = False
    return result


def revision_event(db: Any, row: dict[str, Any], actor: str, event_type: str):
    db.execute(
        "INSERT INTO config_draft_revisions (draft_id,revision,content,content_sha256,state,validation,actor,created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (row["id"], row["revision"], row["content"], row["content_sha256"], row["state"],
         json.dumps(row["validation"]) if row["validation"] is not None else None, actor, tasks.utcnow()),
    )
    tasks.record_event(
        db, aggregate_type="config_draft", aggregate_id=str(row["id"]), type=event_type,
        actor=actor, aggregate_revision=row["revision"],
        payload={"source_id": row["source_id"], "content_sha256": row["content_sha256"], "state": row["state"]},
    )


def read_draft(db: Any, draft_id: uuid.UUID, lock: bool = False) -> dict[str, Any]:
    row = db.execute("SELECT * FROM config_drafts WHERE id=%s" + (" FOR UPDATE" if lock else ""), (draft_id,)).fetchone()
    if row is None:
        error("DRAFT_NOT_FOUND", "下書きが見つかりません", 404)
    return row


def check_revision(row: dict[str, Any], expected: int):
    if row["revision"] != expected:
        error("REVISION_CONFLICT", "別の操作で下書きが更新されています", 409, current_revision=row["revision"])


def build_router(*, pool: Any, auth: Any, config_principal: Callable[[str | None], str]) -> APIRouter:
    router = APIRouter(prefix="/v1/config", dependencies=[Depends(auth)])

    def editor(x_config_admin_token: str | None = Header(default=None)) -> str:
        return config_principal(x_config_admin_token)

    @router.get("/inventory")
    def inventory():
        return fetch_inventory()

    @router.get("/drafts")
    def drafts():
        with pool.connection() as db:
            rows = db.execute("SELECT * FROM config_drafts ORDER BY updated_at DESC, id LIMIT 100").fetchall()
        return {"drafts": [{key: value for key, value in public(row).items() if key not in {"content", "base_content", "diff"}} for row in rows]}

    @router.get("/drafts/{draft_id}")
    def detail(draft_id: uuid.UUID):
        with pool.connection() as db:
            row = public(read_draft(db, draft_id))
            row["history"] = db.execute(
                "SELECT revision,content_sha256,state,actor,created_at FROM config_draft_revisions WHERE draft_id=%s ORDER BY revision DESC",
                (draft_id,),
            ).fetchall()
        return row

    @router.post("/drafts", status_code=201)
    def create(request: DraftCreate, actor: str = Depends(editor), idempotency_key: str = Header(min_length=8, max_length=200)):
        request_hash = digest(json.dumps(request.model_dump(), sort_keys=True))

        def replay(row):
            if row["request_hash"] != request_hash:
                error("IDEMPOTENCY_CONFLICT", "同じ操作キーが別の内容に使われています", 409)
            return public(row)

        with pool.connection() as db:
            existing = db.execute("SELECT * FROM config_drafts WHERE created_by=%s AND idempotency_key=%s", (actor, idempotency_key)).fetchone()
            if existing:
                return replay(existing)
        source = next((item for item in fetch_inventory()["documents"] if item["id"] == request.source_id), None)
        if source is None:
            error("SOURCE_UNAVAILABLE", "編集元の設定が取得できません", 409)
        if source["sha256"] != request.base_sha256:
            error("SOURCE_CHANGED", "編集元の設定が更新されています。最新の内容を確認してください", 409)
        if len(request.content.encode()) > MAX_CONTENT or "\x00" in request.content:
            error("CONTENT_TOO_LARGE", "64 KiB以下のテキストを指定してください")
        metadata = {key: value for key, value in source.items() if key != "content"}
        now = tasks.utcnow()
        with pool.connection() as db:
            row = db.execute(
                "INSERT INTO config_drafts (id,source_id,source,base_content,base_sha256,content,content_sha256,created_by,idempotency_key,request_hash,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(created_by,idempotency_key) DO NOTHING RETURNING *",
                (uuid.uuid4(), request.source_id, json.dumps(metadata), source["content"], source["sha256"],
                 request.content, digest(request.content), actor, idempotency_key, request_hash, now, now),
            ).fetchone()
            if row is None:
                return replay(db.execute("SELECT * FROM config_drafts WHERE created_by=%s AND idempotency_key=%s", (actor, idempotency_key)).fetchone())
            revision_event(db, row, actor, "config.draft_created")
        return public(row)

    @router.patch("/drafts/{draft_id}")
    def update(draft_id: uuid.UUID, request: DraftUpdate, actor: str = Depends(editor)):
        if len(request.content.encode()) > MAX_CONTENT or "\x00" in request.content:
            error("CONTENT_TOO_LARGE", "64 KiB以下のテキストを指定してください")
        with pool.connection() as db:
            row = read_draft(db, draft_id, lock=True)
            check_revision(row, request.expected_revision)
            row = db.execute(
                "UPDATE config_drafts SET content=%s,content_sha256=%s,revision=revision+1,state='DRAFT',validation=NULL,updated_at=%s WHERE id=%s RETURNING *",
                (request.content, digest(request.content), tasks.utcnow(), draft_id),
            ).fetchone()
            revision_event(db, row, actor, "config.draft_updated")
        return public(row)

    @router.post("/drafts/{draft_id}/validate")
    def validate(draft_id: uuid.UUID, request: ValidateDraft, actor: str = Depends(editor)):
        with pool.connection() as db:
            saved = read_draft(db, draft_id)
            check_revision(saved, request.expected_revision)
        report = fetch_inventory()
        source = next((item for item in report["documents"] if item["id"] == saved["source_id"]), None)
        result = validate_content(saved["source"], saved["content"])
        result["base_current"] = source is not None and source["sha256"] == saved["base_sha256"]
        if not result["base_current"]:
            result["passed"] = False
            result["issues"].append("編集元が更新されたか取得できません。差分の再確認が必要です")
        with pool.connection() as db:
            row = read_draft(db, draft_id, lock=True)
            check_revision(row, request.expected_revision)
            row = db.execute(
                "UPDATE config_drafts SET validation=%s,state=%s,revision=revision+1,updated_at=%s WHERE id=%s RETURNING *",
                (json.dumps(result), "VALIDATED" if result["passed"] else "VALIDATION_FAILED", tasks.utcnow(), draft_id),
            ).fetchone()
            revision_event(db, row, actor, "config.draft_validated")
        return public(row)

    return router
