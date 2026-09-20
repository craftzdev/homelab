"""Video contract and durable reconciler tests; no network or GPU calls."""
import hashlib
import io
import uuid
from email.message import Message

import pytest
from test_approvals import BOT, execute, gateway
from app import video


@pytest.fixture(autouse=True)
def video_config(monkeypatch, tmp_path, clean_database):
    monkeypatch.setattr(video, "BASE_URL", "https://omen45.tailb6c7d.ts.net:8443")
    monkeypatch.setattr(video, "DATA_DIR", tmp_path)
    monkeypatch.setattr(video, "api", lambda *a: (_ for _ in ()).throw(AssertionError("unexpected API call")))


def submit(client, key="video-test-key", **overrides):
    body = {"action": "video.generate", "environment": "preview", "project_id": "video-demo",
            "parameters": {"prompt": "A red sailboat"}}
    body.update(overrides)
    return client.post("/v1/jobs", headers={**BOT, "Idempotency-Key": key}, json=body)


def tick():
    with gateway.pool.connection() as db:
        video.tick(db)


@pytest.mark.parametrize("params", [
    {"prompt": ""}, {"prompt": "   "}, {"prompt": "x"*2001},
    {"prompt": "x", "seed": -1}, {"prompt": "x", "seed": True},
    {"prompt": "x", "seed": "10"}, {"prompt": "x", "seed": 2**32},
    {"prompt": "x", "workflow": "arbitrary"}, {"prompt": "x", "url": "http://localhost"},
    {"prompt": "x", "graph": {}}, {"prompt": "x", "width": 8192},
])
def test_validation(client, params):
    assert submit(client, parameters=params).status_code == 422


def test_auth_and_production(client, monkeypatch):
    assert client.get(f"/v1/jobs/{uuid.uuid4()}/video").status_code == 401
    assert submit(client, environment="production").status_code == 403
    monkeypatch.setattr(video, "BASE_URL", "")
    assert submit(client).status_code == 503


def test_idempotency_capacity(client):
    first = submit(client).json()
    assert submit(client).json()["job_id"] == first["job_id"]
    assert submit(client, parameters={"prompt": "different"}).status_code == 409
    for n in range(7):
        assert submit(client, key=f"video-test-extra-{n}").status_code == 202
    assert submit(client, key="video-over-capacity").status_code == 429
    assert submit(client).status_code == 202


def test_workflow_fixed():
    job_id = uuid.uuid4()
    graph = video.workflow({"prompt": "test", "seed": 42}, job_id)
    assert graph["104"]["inputs"] == {"clip": ["13", 0], "vae": ["11", 0],
                                     "prompt": "test", "width": 896, "height": 512, "length": 124}
    assert graph["9"]["inputs"]["steps"] == 8
    assert graph["15"]["inputs"]["noise_seed"] == 42
    assert graph["92"]["inputs"]["filename_prefix"] == f"Gateway/{job_id}"


def test_submit_and_recover_running(client, monkeypatch):
    job_id = submit(client).json()["job_id"]
    pid = str(uuid.uuid4())
    calls = []
    def api(route, body=None):
        calls.append(route)
        return {"queue_running": [], "queue_pending": []} if route == "/queue" else {"prompt_id": pid}
    monkeypatch.setattr(video, "api", api)
    tick()
    assert gateway._load_job(uuid.UUID(job_id))["worker_job_id"] == pid
    # A new reconciler iteration after restart must poll the original ID, not POST again.
    monkeypatch.setattr(video, "api", lambda route: calls.append(route) or {})
    tick()
    assert calls == ["/queue", "/prompt", f"/history/{pid}"]


def test_unknown_submission_never_retries(client, monkeypatch):
    job_id = submit(client).json()["job_id"]
    def api(route, body=None):
        if route == "/queue":
            return {}
        raise TimeoutError()
    monkeypatch.setattr(video, "api", api)
    tick()
    assert gateway._load_job(uuid.UUID(job_id))["state"] == "NEEDS_REVIEW"
    monkeypatch.setattr(video, "api", lambda *a: pytest.fail("must not resubmit"))
    tick()


def test_crash_during_submission(client):
    job_id = submit(client).json()["job_id"]
    execute("UPDATE jobs SET state='VIDEO_SUBMITTING' WHERE id=%s", (job_id,))
    tick()
    assert gateway._load_job(uuid.UUID(job_id))["state"] == "NEEDS_REVIEW"


def test_busy_gpu_and_deadline(client, monkeypatch):
    job_id = submit(client).json()["job_id"]
    monkeypatch.setattr(video, "api", lambda route: {"queue_running": [[1]], "queue_pending": []})
    tick()
    assert gateway._load_job(uuid.UUID(job_id))["state"] == "QUEUED"
    execute("UPDATE jobs SET created_at=NOW()-INTERVAL '1 hour' WHERE id=%s", (job_id,))
    tick()
    assert gateway._load_job(uuid.UUID(job_id))["state"] == "FAILED_FINAL"


class Response(io.BytesIO):
    headers = Message()
    headers["Content-Type"] = "video/mp4"


def test_success_download_and_mcp(client, monkeypatch):
    job_id = submit(client).json()["job_id"]
    pid = str(uuid.uuid4())
    execute("UPDATE jobs SET state='RUNNING',worker_job_id=%s WHERE id=%s", (pid, job_id))
    output = {"filename": job_id+"_00001_.mp4", "subfolder": "Gateway", "type": "output"}
    monkeypatch.setattr(video, "api", lambda route: {pid: {
        "status": {"completed": True, "status_str": "success"}, "outputs": {"92": {"images": [output]}}}})
    data = b'\x00\x00\x00\x18ftypisom' + b'0'*40
    monkeypatch.setattr(video, "open_request", lambda route: Response(data))
    tick()
    result = client.get(f"/v1/jobs/{job_id}", headers=BOT).json()
    assert result["state"] == "SUCCEEDED"
    assert result["result"]["artifact"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert client.get(f"/v1/jobs/{job_id}/video", headers=BOT).content == data
    assert gateway.mcp_get_job(job_id)["state"] == "SUCCEEDED"
    assert gateway.mcp_get_review_url(job_id)["review"]["tailnet_only"]
    video.artifact_path(job_id).unlink()
    assert client.get(f"/v1/jobs/{job_id}/video", headers=BOT).status_code == 410


@pytest.mark.parametrize("change", [
    {"filename": "../../etc/passwd.mp4"}, {"subfolder": "../input"}, {"type": "input"},
    {"filename": "https://evil/video.mp4"}, {"filename": "other-job.mp4"},
])
def test_artifact_paths(change):
    job_id = uuid.uuid4()
    output = {"filename": f"{job_id}_00001_.mp4", "subfolder": "Gateway", "type": "output"}
    with pytest.raises(ValueError):
        video.output_query({**output, **change}, job_id)


def test_redirect_rejected():
    with pytest.raises(ValueError):
        video.NoRedirect().redirect_request(None,None,302,None,None,"http://evil")


def test_bad_video_removed(monkeypatch):
    job_id = uuid.uuid4()
    monkeypatch.setattr(video, "open_request", lambda route: Response(b"not a valid movie"))
    with pytest.raises(ValueError):
        video.save_artifact("x", job_id)
    assert not video.artifact_path(job_id).exists()
    assert not video.artifact_path(job_id).with_suffix(".part").exists()


def test_mcp_submit(client):
    result = gateway.mcp_submit_job(action="video.generate", project_id="video-demo",
        environment="preview", parameters={"prompt":"a sailboat"}, idempotency_key="mcp-video-key")
    assert result["state"] == "QUEUED"
    assert gateway.mcp_submit_job(action="video.generate", project_id="video-demo",
        environment="preview", parameters={"prompt":"a sailboat"}, idempotency_key="mcp-video-key")["idempotent_replay"]


@pytest.mark.parametrize("limits", [{"timeout_seconds": 1}, {"timeout_seconds": 1801}, {"other": 10}])
def test_limits(client, limits):
    assert submit(client, limits=limits).status_code == 422


def test_storage_capacity(client, monkeypatch):
    job_id = submit(client).json()["job_id"]
    monkeypatch.setattr(video, "cleanup", lambda: video.MAX_STORED_BYTES)
    tick()
    assert gateway.mcp_get_job(job_id)["result"]["error"] == "video_storage_capacity_reached"


def test_running_timeout_does_not_interrupt(client):
    job_id = submit(client).json()["job_id"]
    pid = str(uuid.uuid4())
    execute("UPDATE jobs SET state='RUNNING',worker_job_id=%s,created_at=NOW()-INTERVAL '1 hour' WHERE id=%s", (pid, job_id))
    tick()
    assert gateway.mcp_get_job(job_id)["state"] == "NEEDS_REVIEW"
    assert gateway.mcp_get_job(job_id)["result"]["remote_job_may_continue"]


def test_multiprocess_lock(client, monkeypatch):
    submit(client)
    calls = []
    monkeypatch.setattr(video, "api", lambda route: calls.append(route) or {"queue_running": [[1]]})
    runner = video.VideoRunner(gateway.pool)
    with gateway.pool.connection() as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (video.LOCK_ID,))
        holder.commit()
        runner.run_once()
        assert calls == []
        holder.execute("SELECT pg_advisory_unlock(%s)", (video.LOCK_ID,))
        holder.commit()
    runner.run_once()
    assert calls == ["/queue"]


def test_oversized_artifact(monkeypatch):
    monkeypatch.setattr(video, "MAX_BYTES", 16)
    monkeypatch.setattr(video, "open_request", lambda route: Response(b'0000ftyp' + b'0'*40))
    with pytest.raises(ValueError):
        video.save_artifact("x", uuid.uuid4())


def test_worker_cannot_complete_video(client, monkeypatch):
    job_id = submit(client).json()["job_id"]
    monkeypatch.setattr(gateway, "API_SURFACE", "callback")
    response = client.post("/v1/worker-events", headers={"Authorization": "Bearer test-callback-credential"}, json={
        "event_id": "fake-video-complete", "gateway_job_id": job_id, "dispatch_id": f"gateway:{job_id}:1",
        "worker_job_id": "fake", "event_type": "completed", "sequence": 1, "occurred_at": gateway.utcnow().isoformat(), "data": {}})
    assert response.status_code == 409
    assert gateway.mcp_get_job(job_id)["state"] == "QUEUED"
