"""Real protocol tests: spawn the server over stdio and over Streamable HTTP (uvicorn) and talk to
it with the official MCP client (initialize/discover, tools/list, tools/call)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import anyio
import httpx2
import pytest
from django.db import connection
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

pytestmark = [pytest.mark.protocol, pytest.mark.django_db(transaction=True)]
ROOT = Path(__file__).resolve().parent.parent
TOKEN = "test-token-please-change"


def server_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MCP_")}
    env.update(
        POSTGRES_DB=connection.settings_dict["NAME"],
        MI_INCLUDE_SYNTHETIC_DATA="true",
        LOG_LEVEL="WARNING",
        PYTHONPATH=str(ROOT),
        **extra,
    )
    return env


def wait_for_other_sessions_to_close(timeout: float = 10.0) -> None:
    """Server subprocesses hold DB connections; wait until PostgreSQL has released them."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
            if cursor.fetchone()[0] == 0:
                return
        time.sleep(0.1)


async def exercise(client: Client) -> None:
    tools = await client.list_tools()
    assert len(tools.tools) == 8
    result = await client.call_tool("search_products", {"query": "botas alpinismo", "limit": 3})
    assert not result.is_error and result.structured_content["data"]
    result = await client.call_tool(
        "check_compatibility", {"product_ids": ["example-outdoor-alpine-gtx-example", "demo-peaks-ice-lever-12"]}
    )
    assert result.structured_content["data"]["status"] == "compatible"


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_stdio_transport(demo_catalog, mode):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mountain_intelligence.mcp_server", "stdio"],
        env=server_env(),
        cwd=str(ROOT),
    )

    async def run():
        async with Client(params, mode=mode, read_timeout_seconds=30) as client:
            await exercise(client)

    anyio.run(run)
    wait_for_other_sessions_to_close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server(demo_catalog):
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "mountain_intelligence.mcp_server", "http", "--port", str(port)],
        env=server_env(MCP_AUTH_TOKENS=TOKEN, MCP_RATE_LIMIT_PER_MINUTE="1000"),
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx2.get(base + "/healthz", timeout=1).status_code == 200:
                break
        except httpx2.TransportError:
            pass
        if proc.poll() is not None:
            pytest.fail(proc.stderr.read().decode())
        time.sleep(0.1)
    yield base
    proc.terminate()
    proc.wait(timeout=10)
    wait_for_other_sessions_to_close()


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_streamable_http_transport_with_auth(http_server, mode):
    async def run():
        http_client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
        async with Client(streamable_http_client(http_server + "/mcp", http_client=http_client), mode=mode) as c:
            await exercise(c)

    anyio.run(run)


def test_http_rejects_unauthorized_and_bad_hosts(http_server):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    no_auth = httpx2.post(http_server + "/mcp", json=body, headers=headers)
    assert no_auth.status_code == 401 and "Bearer" in no_auth.headers["www-authenticate"]
    wrong = httpx2.post(http_server + "/mcp", json=body, headers={**headers, "Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    ok = httpx2.post(http_server + "/mcp", json=body, headers={**headers, "Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200 and ok.json()["result"]["serverInfo"]["name"] == "mountain-intelligence"
    rebinding = httpx2.post(
        http_server + "/mcp", json=body, headers={**headers, "Authorization": f"Bearer {TOKEN}", "Host": "evil.example"}
    )
    assert rebinding.status_code in (400, 421)
    bad_origin = httpx2.post(
        http_server + "/mcp",
        json=body,
        headers={**headers, "Authorization": f"Bearer {TOKEN}", "Origin": "https://evil.example"},
    )
    assert bad_origin.status_code == 403
    assert httpx2.get(http_server + "/healthz").json() == {"status": "ok"}


def test_http_refuses_to_start_without_auth(db):
    proc = subprocess.run(
        [sys.executable, "-m", "mountain_intelligence.mcp_server", "http", "--port", str(free_port())],
        env=server_env(),
        cwd=str(ROOT),
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode != 0 and b"MCP_AUTH_TOKENS" in proc.stderr
    wait_for_other_sessions_to_close()


def test_public_mode_skips_token_check_but_default_requires_it():
    from mountain_intelligence.mcp_server.http import BearerAuthMiddleware

    async def run():
        reached: list[str] = []
        sent: list[dict] = []

        async def app(scope, receive, send):
            reached.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "path": "/mcp", "headers": []}
        await BearerAuthMiddleware(app, tokens=["secret"], allow_unauthenticated=True)(scope, receive, send)
        assert reached == ["/mcp"] and sent[0]["status"] == 200

        sent.clear()
        await BearerAuthMiddleware(app, tokens=["secret"])(dict(scope), receive, send)
        assert reached == ["/mcp"] and sent[0]["status"] == 401

    anyio.run(run)
