"""Durable configuration conversations. Model output is a proposal, never a release."""
from __future__ import annotations

import difflib
import hmac
import json
import os
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field

from app import configuration as config, tasks

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS config_chat_sessions (
 id UUID PRIMARY KEY, source JSONB NOT NULL, base_content TEXT NOT NULL,
 content TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 created_by TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
 UNIQUE(created_by, idempotency_key)
);
CREATE TABLE IF NOT EXISTS config_chat_turns (
 id UUID PRIMARY KEY, session_id UUID NOT NULL REFERENCES config_chat_sessions(id),
 message TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'QUEUED',
 context JSONB NOT NULL, request_sha256 TEXT NOT NULL,
 reply TEXT, proposal TEXT, failure TEXT, draft_id UUID REFERENCES config_drafts(id),
 idempotency_key TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
 UNIQUE(session_id, idempotency_key)
);
"""


class Create(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    base_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class Revision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)


class Message(Revision):
    message: str = Field(min_length=1, max_length=8000)


class Accept(Revision):
    turn_id: uuid.UUID


class Report(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    state: Literal['RUNNING', 'COMPLETED', 'FAILED']
    reply: str | None = Field(default=None, max_length=16000)
    proposal: str | None = Field(default=None, max_length=config.MAX_CONTENT)
    failure: str | None = Field(default=None, max_length=500)


def read(db, session_id, lock=False):
    row = db.execute('SELECT * FROM config_chat_sessions WHERE id=%s' + (' FOR UPDATE' if lock else ''), (session_id,)).fetchone()
    if not row:
        config.error('CHAT_NOT_FOUND', '会話が見つかりません', 404)
    return row


def revision(row, expected):
    if row['revision'] != expected:
        config.error('REVISION_CONFLICT', '会話が更新されています。最新の内容を確認してください', 409)


def public(db, row):
    result = {k: v for k, v in row.items() if k not in {'idempotency_key', 'request_hash'}}
    result['source'] = dict(row['source'])
    config.attach_names(db, [result['source']])
    turns = db.execute('SELECT * FROM config_chat_turns WHERE session_id=%s ORDER BY created_at,id', (row['id'],)).fetchall()
    result['turns'] = []
    for turn in turns:
        item = {k: v for k, v in turn.items() if k not in {'context', 'idempotency_key'}}
        if turn['proposal'] is not None:
            item['diff'] = ''.join(difflib.unified_diff(row['base_content'].splitlines(keepends=True), turn['proposal'].splitlines(keepends=True), fromfile='配置中/' + row['source']['path'], tofile='変更案/' + row['source']['path']))
        result['turns'].append(item)
    return result


def build_router(*, pool, auth, config_principal):
    router = APIRouter(prefix='/v1/config/chat', dependencies=[Depends(auth)])

    def editor(x_config_admin_token: str | None = Header(default=None)):
        return config_principal(x_config_admin_token)

    def controller(x_config_controller_token: str | None = Header(default=None)):
        expected = os.environ.get('CONFIG_CONTROLLER_TOKEN', '')
        if len(expected) < 32 or not hmac.compare_digest(x_config_controller_token or '', expected):
            config.error('CONTROLLER_REQUIRED', '設定アシスタントの認証が必要です', 401)

    @router.get('/sessions')
    def sessions():
        with pool.connection() as db:
            rows = db.execute('SELECT id,source,revision,created_at,updated_at FROM config_chat_sessions ORDER BY updated_at DESC LIMIT 50').fetchall()
            config.attach_names(db, [row['source'] for row in rows])
        return {'sessions': rows}

    @router.post('/sessions', status_code=201)
    def create(body: Create, actor=Depends(editor), idempotency_key: str = Header(min_length=8, max_length=200)):
        fingerprint = config.digest(json.dumps(body.model_dump(), sort_keys=True))
        with pool.connection() as db:
            row = db.execute('SELECT * FROM config_chat_sessions WHERE created_by=%s AND idempotency_key=%s', (actor, idempotency_key)).fetchone()
            if row:
                if row['request_hash'] != fingerprint:
                    config.error('IDEMPOTENCY_CONFLICT', '同じ操作キーが別の対象に使われています', 409)
                return public(db, row)
        source = next((d for d in config.fetch_inventory()['documents'] if d['id'] == body.source_id), None)
        if not source or source['sha256'] != body.base_sha256:
            config.error('SOURCE_CHANGED', '対象の設定が更新されています。一覧を再取得してください', 409)
        if source['kind'] not in {'skill', 'harness', 'profile'}:
            config.error('CHAT_KIND_UNSUPPORTED', 'チャットではスキル・ハーネス・役割の本文を編集できます', 422)
        now = tasks.utcnow()
        with pool.connection() as db:
            row = db.execute('INSERT INTO config_chat_sessions (id,source,base_content,content,created_by,idempotency_key,request_hash,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(created_by,idempotency_key) DO NOTHING RETURNING *',
                (uuid.uuid4(), json.dumps({k:v for k,v in source.items() if k != 'content'}), source['content'], source['content'], actor, idempotency_key, fingerprint, now, now)).fetchone()
            if row is None:
                row = db.execute('SELECT * FROM config_chat_sessions WHERE created_by=%s AND idempotency_key=%s', (actor,idempotency_key)).fetchone()
                if row['request_hash'] != fingerprint:
                    config.error('IDEMPOTENCY_CONFLICT', '同じ操作キーが別の対象に使われています', 409)
            return public(db, row)

    @router.get('/sessions/{session_id}')
    def detail(session_id: uuid.UUID):
        with pool.connection() as db:
            return public(db, read(db, session_id))

    @router.post('/sessions/{session_id}/messages', status_code=202)
    def message(session_id: uuid.UUID, body: Message, actor=Depends(editor), idempotency_key: str = Header(min_length=8, max_length=200)):
        if not body.message.strip() or '\x00' in body.message:
            config.error('INVALID_MESSAGE', 'メッセージを入力してください', 422)
        with pool.connection() as db:
            # Bound model work across sessions, including concurrent submissions.
            db.execute('SELECT pg_advisory_xact_lock(6300941)')
            row = read(db, session_id, True)
            previous = db.execute('SELECT * FROM config_chat_turns WHERE session_id=%s AND idempotency_key=%s', (session_id,idempotency_key)).fetchone()
            if previous:
                if previous['message'] != body.message:
                    config.error('IDEMPOTENCY_CONFLICT', '同じ送信キーが別のメッセージに使われています', 409)
                return public(db, row)
            revision(row, body.expected_revision)
            turns = db.execute('SELECT * FROM config_chat_turns WHERE session_id=%s ORDER BY created_at,id', (session_id,)).fetchall()
            if any(t['state'] in {'QUEUED','RUNNING'} for t in turns):
                config.error('CHAT_BUSY', '返信を待ってから送信してください', 409)
            if len(turns) >= 40:
                config.error('CHAT_LIMIT', 'この会話は40往復までです。新しい会話を開始してください', 409)
            pending = db.execute("SELECT count(*) n FROM config_chat_turns WHERE state IN ('QUEUED','RUNNING')").fetchone()['n']
            if pending >= 4:
                config.error('CHAT_CAPACITY', 'アシスタントが混み合っています。少し待って送信してください', 429)
            history = [{'user': t['message'][:2000], 'assistant': (t['reply'] or '')[:3000]} for t in turns[-10:] if t['state'] == 'COMPLETED']
            context = {'source': row['source'], 'content': row['content'], 'history': history, 'message': body.message}
            fingerprint = config.digest(json.dumps(context, sort_keys=True))
            now = tasks.utcnow()
            db.execute('INSERT INTO config_chat_turns (id,session_id,message,context,request_sha256,idempotency_key,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                (uuid.uuid4(),session_id,body.message,json.dumps(context),fingerprint,idempotency_key,now,now))
            row = db.execute('UPDATE config_chat_sessions SET revision=revision+1,updated_at=%s WHERE id=%s RETURNING *', (now,session_id)).fetchone()
            return public(db,row)

    @router.post('/sessions/{session_id}/accept')
    def accept(session_id: uuid.UUID, body: Accept, actor=Depends(editor)):
        inventory = config.fetch_inventory()
        with pool.connection() as db:
            row = read(db,session_id,True)
            turn = db.execute('SELECT * FROM config_chat_turns WHERE id=%s AND session_id=%s', (body.turn_id,session_id)).fetchone()
            if not turn:
                config.error('TURN_NOT_FOUND', '変更案が見つかりません', 404)
            if turn['draft_id']:
                return config.public(config.read_draft(db,turn['draft_id']))
            revision(row,body.expected_revision)
            latest = db.execute('SELECT id FROM config_chat_turns WHERE session_id=%s ORDER BY created_at DESC,id DESC LIMIT 1', (session_id,)).fetchone()
            if latest['id'] != turn['id'] or turn['state'] != 'COMPLETED' or turn['proposal'] is None:
                config.error('PROPOSAL_UNAVAILABLE', '最新の返信にある変更案を選択してください', 409)
            source = next((d for d in inventory['documents'] if d['id'] == row['source']['id']), None)
            if source is None or source['sha256'] != row['source']['sha256']:
                config.error('SOURCE_CHANGED', '配置中の設定が更新されています。新しい会話で差分を確認してください', 409)
            if turn['proposal'] == row['base_content']:
                config.error('NO_CHANGE', '配置中の設定と変更案に差分がありません', 409)
            validation = config.validate_content(row['source'],turn['proposal'])
            validation['base_current'] = True
            now = tasks.utcnow()
            draft = db.execute('INSERT INTO config_drafts (id,source_id,source,base_content,base_sha256,content,content_sha256,created_by,idempotency_key,request_hash,created_at,updated_at,state,validation) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',
                (uuid.uuid4(),row['source']['id'],json.dumps(row['source']),row['base_content'],row['source']['sha256'],turn['proposal'],config.digest(turn['proposal']),actor,'chat-'+str(turn['id']),turn['request_sha256'],now,now,'VALIDATED' if validation['passed'] else 'VALIDATION_FAILED',json.dumps(validation))).fetchone()
            config.revision_event(db,draft,actor,'config.chat_proposal_accepted')
            db.execute('UPDATE config_chat_turns SET draft_id=%s WHERE id=%s',(draft['id'],turn['id']))
            db.execute('UPDATE config_chat_sessions SET revision=revision+1,updated_at=%s WHERE id=%s',(now,session_id))
            return config.public(draft)

    @router.get('/controller/work', dependencies=[Depends(controller)])
    def work():
        with pool.connection() as db:
            expired = db.execute("UPDATE config_chat_turns SET state='FAILED',failure='応答がタイムアウトしました。再送信してください。',updated_at=%s WHERE state IN ('QUEUED','RUNNING') AND created_at < %s - INTERVAL '15 minutes' RETURNING session_id", (tasks.utcnow(),tasks.utcnow())).fetchall()
            for item in expired:
                db.execute('UPDATE config_chat_sessions SET revision=revision+1,updated_at=%s WHERE id=%s',(tasks.utcnow(),item['session_id']))
            rows = db.execute("SELECT id,context,request_sha256,state,created_at FROM config_chat_turns WHERE state IN ('QUEUED','RUNNING') ORDER BY CASE state WHEN 'RUNNING' THEN 0 ELSE 1 END,created_at LIMIT 1").fetchall()
        return {'turns':rows}

    @router.post('/controller/turns/{turn_id}/report', dependencies=[Depends(controller)])
    def report(turn_id: uuid.UUID, body: Report):
        if body.state == 'COMPLETED' and (not body.reply or not body.reply.strip()):
            config.error('INVALID_REPLY', '空の応答は保存できません', 422)
        if body.proposal is not None and (body.state != 'COMPLETED' or not body.proposal.strip() or '\x00' in body.proposal or len(body.proposal.encode()) > config.MAX_CONTENT):
            config.error('INVALID_PROPOSAL', '変更案の形式が不正です', 422)
        with pool.connection() as db:
            identity = db.execute('SELECT session_id FROM config_chat_turns WHERE id=%s',(turn_id,)).fetchone()
            if not identity:
                config.error('TURN_NOT_FOUND', '会話が見つかりません', 404)
            session = read(db,identity['session_id'],True)
            turn = db.execute('SELECT * FROM config_chat_turns WHERE id=%s FOR UPDATE',(turn_id,)).fetchone()
            if turn['request_sha256'] != body.request_sha256:
                config.error('CHAT_IDENTITY_CONFLICT','応答の対象が一致しません',409)
            if turn['state'] in {'COMPLETED','FAILED'}:
                return {'state':turn['state']}
            if turn['state'] == body.state:
                return {'state':turn['state']}
            db.execute('UPDATE config_chat_turns SET state=%s,reply=%s,proposal=%s,failure=%s,updated_at=%s WHERE id=%s',
                (body.state,body.reply,body.proposal,body.failure,tasks.utcnow(),turn_id))
            db.execute('UPDATE config_chat_sessions SET revision=revision+1,content=%s,updated_at=%s WHERE id=%s',
                (body.proposal if body.proposal is not None else session['content'],tasks.utcnow(),session['id']))
        return {'state':body.state}

    return router
