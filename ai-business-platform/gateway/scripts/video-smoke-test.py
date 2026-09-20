#!/usr/bin/env python3
"""Opt-in external REST/MCP E2E. Generates two bounded videos; never prints tokens."""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid

BASE = "https://gateway.craftz.dev"
HEADERS = {
    "User-Agent": "Craftz-Gateway-Video-Smoke/1.0",
    "Authorization": "Bearer " + os.environ["GATEWAY_API_TOKEN"],
    "CF-Access-Client-Id": os.environ["CF_ACCESS_CLIENT_ID"],
    "CF-Access-Client-Secret": os.environ["CF_ACCESS_CLIENT_SECRET"],
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("unexpected redirect; check Cloudflare Access credentials")


def request(route, body=None, extra=None, binary=False):
    req = urllib.request.Request(BASE + route, data=json.dumps(body).encode() if body is not None else None,
                                 headers={**HEADERS, **(extra or {})})
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
        data = response.read()
        return data if binary else json.loads(data)


def rpc(method, params):
    response = request("/mcp", {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params})
    if "error" in response:
        raise RuntimeError("MCP request failed")
    result = response["result"]
    if result.get("isError"):
        raise RuntimeError("MCP tool failed")
    return result


def tool(name, arguments):
    return rpc("tools/call", {"name": name, "arguments": arguments})["structuredContent"]


def main():
    init = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                              "clientInfo": {"name": "gateway-video-e2e", "version": "1"}})
    HEADERS["MCP-Protocol-Version"] = init["protocolVersion"]
    tools = rpc("tools/list", {})["tools"]
    submit = next(t for t in tools if t["name"] == "submit_job")
    assert "video.generate" in submit["inputSchema"]["properties"]["action"]["enum"]
    # Authentication and arbitrary workflow injection remain rejected.
    try:
        request("/v1/jobs/"+str(uuid.uuid4()), extra={"Authorization": "Bearer invalid"})
        raise AssertionError("invalid bearer accepted")
    except urllib.error.HTTPError as error:
        assert error.code == 401
    body = {"action": "video.generate", "project_id": "video-smoke", "environment": "preview",
            "parameters": {"prompt": "A red toy sailboat floating on a calm pond in warm morning light, gentle camera movement.", "seed": 2026092003},
            "limits": {"timeout_seconds": 900}}
    try:
        request("/v1/jobs", {**body, "parameters": {**body["parameters"], "url": "http://127.0.0.1"}},
                {"Idempotency-Key": "invalid-video-"+str(uuid.uuid4())})
        raise AssertionError("arbitrary URL accepted")
    except urllib.error.HTTPError as error:
        assert error.code == 422
    results = []
    for transport in ("REST", "MCP"):
        key = "video-e2e-" + str(uuid.uuid4())
        body["parameters"]["seed"] += 1
        if transport == "REST":
            submitted = request("/v1/jobs", body, {"Idempotency-Key": key})
        else:
            submitted = tool("submit_job", {**body, "idempotency_key": key})
        job_id = submitted["job_id"]
        replay = tool("submit_job", {**body, "idempotency_key": key})
        assert replay["job_id"] == job_id and replay["idempotent_replay"]
        print(json.dumps({"transport": transport, "job_id": job_id, "state": submitted["state"], "replay_verified": True}), flush=True)
        deadline = time.monotonic() + 950
        while time.monotonic() < deadline:
            job = tool("get_job", {"job_id": job_id})
            if job["state"] in {"SUCCEEDED", "FAILED_FINAL", "NEEDS_REVIEW"}:
                break
            time.sleep(5)
        assert job["state"] == "SUCCEEDED", {"job_id": job_id, "state": job["state"], "result": job["result"]}
        data = request(f"/v1/jobs/{job_id}/video", binary=True)
        assert data[4:8] == b"ftyp"
        assert hashlib.sha256(data).hexdigest() == job["result"]["artifact"]["sha256"]
        assert len(data) == job["result"]["artifact"]["bytes"]
        review = tool("get_review_url", {"job_id": job_id})
        assert review["available"] and review["review"]["tailnet_only"]
        results.append({"transport": transport, "job_id": job_id, "state": "SUCCEEDED", "result": job["result"]})
        print(json.dumps(results[-1]), flush=True)
    print("REST + MCP video generation E2E passed", flush=True)


if __name__ == "__main__":
    main()
