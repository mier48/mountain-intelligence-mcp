"""Streamable HTTP deployment: auth, rate limiting, Host/Origin validation and health endpoint.

Authentication (MVP): static bearer tokens configured through MCP_AUTH_TOKENS and compared in
constant time. This is a private-deployment mechanism for known clients; it is NOT the MCP OAuth
2.1 authorization flow and must not be presented as universally interoperable. See docs/security.md.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from django.conf import settings
from django.db import close_old_connections, connection
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)
PUBLIC_PATHS = frozenset({"/healthz"})


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def _send_json(send: Send, status: int, payload: dict, headers: list[tuple[bytes, bytes]] | None = None) -> None:
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *(headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, tokens: list[str], allow_unauthenticated: bool = False):
        self.app = app
        self.digests = [_digest(t) for t in tokens if t]
        self.allow_unauthenticated = allow_unauthenticated

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS or self.allow_unauthenticated:
            await self.app(scope, receive, send)
            return
        header = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
        scheme, _, token = header.partition(" ")
        if scheme.lower() == "bearer" and token:
            presented = _digest(token.strip())
            if any(hmac.compare_digest(presented, d) for d in self.digests):
                scope.setdefault("state", {})["client_key"] = presented.hex()[:16]
                await self.app(scope, receive, send)
                return
        logger.info("rejected unauthenticated MCP request", extra={"path": scope["path"]})
        await _send_json(
            send,
            401,
            {"error": "invalid_token", "error_description": "Authentication required"},
            [(b"www-authenticate", b'Bearer realm="mountain-intelligence", error="invalid_token"')],
        )


class RateLimitMiddleware:
    """Sliding-window limit per client IP (in-process; one limiter per server process)."""

    def __init__(self, app: ASGIApp, per_minute: int):
        self.app = app
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.per_minute <= 0 or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        client = (scope.get("client") or ("unknown", 0))[0]
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(client, deque())
            while hits and hits[0] <= now - 60:
                hits.popleft()
            limited = len(hits) >= self.per_minute
            if not limited:
                hits.append(now)
            if len(self._hits) > 10000:  # bound memory
                self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > now - 60}
        if limited:
            await _send_json(send, 429, {"error": "rate_limited"}, [(b"retry-after", b"60")])
            return
        await self.app(scope, receive, send)


def _db_ok() -> bool:
    close_old_connections()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            return cursor.fetchone() == (1,)
    except Exception:
        logger.warning("health check: database unavailable")
        return False
    finally:
        close_old_connections()


async def healthz(_: Request) -> JSONResponse:
    ok = await anyio.to_thread.run_sync(_db_ok)
    return JSONResponse({"status": "ok" if ok else "unavailable"}, status_code=200 if ok else 503)


def build_http_app(mcp: MCPServer) -> Starlette:
    if not settings.MCP_AUTH_TOKENS and not settings.MCP_ALLOW_UNAUTHENTICATED:
        raise RuntimeError(
            "Refusing to start: set MCP_AUTH_TOKENS (or MCP_ALLOW_UNAUTHENTICATED=true for local development only)"
        )
    if settings.MCP_ALLOW_UNAUTHENTICATED:
        logger.warning("MCP HTTP endpoint running WITHOUT authentication (development only)")
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.MCP_ALLOWED_HOSTS,
        allowed_origins=settings.MCP_ALLOWED_ORIGINS,
    )
    mcp_app = mcp.streamable_http_app(
        streamable_http_path=settings.MCP_HTTP_PATH,
        json_response=True,
        stateless_http=True,
        transport_security=security,
        max_request_body_size=settings.MCP_MAX_REQUEST_BODY_BYTES,
    )

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    return Starlette(
        routes=[Route("/healthz", healthz, methods=["GET"]), Mount("/", app=mcp_app)],
        middleware=[
            Middleware(RateLimitMiddleware, per_minute=settings.MCP_RATE_LIMIT_PER_MINUTE),
            Middleware(
                BearerAuthMiddleware,
                tokens=settings.MCP_AUTH_TOKENS,
                allow_unauthenticated=settings.MCP_ALLOW_UNAUTHENTICATED,
            ),
        ],
        lifespan=lifespan,
    )
