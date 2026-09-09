#!/usr/bin/env python3
"""Exercise the public MCP endpoint without printing any credentials."""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main() -> None:
    url = os.environ.get("MCP_URL", "https://gateway.craftz.dev/mcp")
    expected_state = os.environ.get("MCP_EXPECT_STATE", "SUCCEEDED")
    headers = {
        "Authorization": f"Bearer {os.environ['GATEWAY_API_TOKEN']}",
    }
    if os.environ.get("MCP_HOST_HEADER"):
        headers["Host"] = os.environ["MCP_HOST_HEADER"]
    if os.environ.get("CF_ACCESS_CLIENT_ID"):
        headers["CF-Access-Client-Id"] = os.environ["CF_ACCESS_CLIENT_ID"]
    if os.environ.get("CF_ACCESS_CLIENT_SECRET"):
        headers["CF-Access-Client-Secret"] = os.environ["CF_ACCESS_CLIENT_SECRET"]

    async with httpx2.AsyncClient(
        headers=headers,
        follow_redirects=True,
        timeout=30,
    ) as http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = await session.list_tools()
                tool_names = {tool.name for tool in tools.tools}
                expected = {"submit_job", "get_job", "wait_for_job", "get_review_url"}
                if tool_names != expected:
                    raise RuntimeError(
                        f"unexpected MCP tools: {sorted(tool_names)}"
                    )
                submit_tool = next(tool for tool in tools.tools if tool.name == "submit_job")
                properties = submit_tool.input_schema.get("properties", {})
                if set(properties.get("action", {}).get("enum", [])) != {
                    "browser.research",
                    "analytics.read",
                    "stripe.read",
                    "code.build",
                    "code.fix",
                    "test.run",
                }:
                    raise RuntimeError("submit_job exposes an unexpected action")
                if set(properties.get("environment", {}).get("enum", [])) != {
                    "research",
                    "preview",
                }:
                    raise RuntimeError("submit_job exposes an unsafe environment")
                if os.environ.get("MCP_SMOKE_LIST_ONLY") == "1":
                    print(f"MCP discovery passed: tools={len(tool_names)}")
                    return

                key = f"mcp-smoke-{uuid.uuid4()}"
                submitted = await session.call_tool(
                    "submit_job",
                    {
                        "action": "test.run",
                        "project_id": "worker-demo",
                        "environment": "research",
                        "parameters": {"operation": "self_test"},
                        "limits": {"timeout_seconds": 60},
                        "idempotency_key": key,
                    },
                )
                if submitted.is_error:
                    raise RuntimeError(f"submit_job failed: {submitted.content}")
                submitted_job = submitted.structured_content
                if not submitted_job or not submitted_job.get("job_id"):
                    raise RuntimeError("submit_job did not return a job_id")

                completed = await session.call_tool(
                    "wait_for_job",
                    {
                        "job_id": submitted_job["job_id"],
                        "timeout_seconds": 60,
                        "poll_interval_seconds": 2,
                    },
                    read_timeout_seconds=90,
                )
                if completed.is_error:
                    raise RuntimeError(f"wait_for_job failed: {completed.content}")
                completed_job = completed.structured_content
                if not completed_job or completed_job.get("state") != expected_state:
                    raise RuntimeError(
                        "job reached an unexpected state: "
                        f"{json.dumps(completed_job, default=str)}"
                    )

                fetched = await session.call_tool(
                    "get_job", {"job_id": submitted_job["job_id"]}
                )
                if fetched.is_error or fetched.structured_content != completed_job:
                    raise RuntimeError("get_job did not return the completed job")

                review = await session.call_tool(
                    "get_review_url", {"job_id": submitted_job["job_id"]}
                )
                if review.is_error or review.structured_content is None:
                    raise RuntimeError("get_review_url failed")

                print(
                    "MCP smoke test passed: "
                    f"job_id={submitted_job['job_id']} "
                    f"state={completed_job['state']} tools={len(tool_names)}"
                )


if __name__ == "__main__":
    asyncio.run(main())
