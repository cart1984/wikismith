"""Serving mcpedia over Streamable HTTP, behind a Bearer token."""

from __future__ import annotations

import hmac
import json
import weakref
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from starlette.types import ASGIApp, Receive, Scope, Send

LOOPBACK = ("127.0.0.1", "localhost", "::1")
MIN_TOKEN_LENGTH = 32
MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"

Health = Callable[[], dict[str, str]]
# Each server's health report (`server.health`), registered by `build_server`.
HEALTH: weakref.WeakKeyDictionary[MCPServer, Health] = weakref.WeakKeyDictionary()


class BearerAuth:
    """Rejects HTTP requests without the right `Authorization: Bearer <token>`.

    `/healthz` stays open so container health checks work. It reports states only ("vault": "readable", "index":
    "ready"), never paths or content, and answers 503 when something is broken, so the container counts as
    unhealthy.
    """

    def __init__(self, app: ASGIApp, token: str | None, health: Health | None = None) -> None:
        self.app = app
        self.token = token.encode() if token else None
        self.health = health

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)  # lifespan events
            return
        if scope["path"] == HEALTH_PATH:
            state = self.health() if self.health else {"status": "ok"}
            await _respond(send, 200 if state.get("status") == "ok" else 503, state)
            return
        if self.token is not None and not self._authorized(scope):
            await _respond(send, 401, {"error": "unauthorized"}, {"www-authenticate": "Bearer"})
            return
        await self.app(scope, receive, send)

    def _authorized(self, scope: Scope) -> bool:
        assert self.token is not None
        for name, value in scope.get("headers", []):
            if name == b"authorization" and value[:7].lower() == b"bearer ":
                return hmac.compare_digest(value[7:].strip(), self.token)
        return False


async def _respond(send: Send, status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> None:
    payload = json.dumps(body).encode()
    raw_headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    raw_headers += [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
    await send({"type": "http.response.start", "status": status, "headers": raw_headers})
    await send({"type": "http.response.body", "body": payload})


def check_token(host: str, token: str | None, allow_no_auth: bool) -> None:
    if token is None:
        if host in LOOPBACK and allow_no_auth:
            return
        raise ValueError(
            "HTTP needs a token: set WIKISMITH_TOKEN (e.g. `openssl rand -hex 32`). Only on a loopback "
            "address can you go without one, with --no-auth."
        )
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"WIKISMITH_TOKEN is too short; use at least {MIN_TOKEN_LENGTH} characters.")


def http_app(server: MCPServer, token: str | None, host: str) -> ASGIApp:
    return BearerAuth(server.streamable_http_app(streamable_http_path=MCP_PATH, host=host), token, HEALTH.get(server))


def serve_http(server: MCPServer, host: str, port: int, token: str | None) -> None:
    import uvicorn

    uvicorn.run(http_app(server, token, host), host=host, port=port, log_level="info")
