"""mcpedia over real HTTP: uvicorn on a free port, an MCP client with and without the token."""

import socket
import threading
import time
from collections.abc import Iterator

import httpx2
import pytest
import uvicorn
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from wikismith.httpserve import check_token, http_app
from wikismith.server import build_server
from wikismith.vault import Vault

TOKEN = "t" * 40


@pytest.fixture
def base_url(vault: Vault) -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(http_app(build_server(vault, warm=False), TOKEN, "127.0.0.1"), port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_health_is_open_and_reports_states_only(base_url: str) -> None:
    response = httpx2.get(f"{base_url}/healthz")
    assert response.status_code == 200
    assert response.json() == {"vault": "readable", "index": "building", "status": "ok"}  # no paths, no content


def test_health_fails_when_the_vault_or_index_does(vault: Vault) -> None:
    from wikismith.search import SearchIndex
    from wikismith.server import health

    index = SearchIndex(vault, None)
    index.refresh()
    assert health(vault, index) == {"vault": "readable", "index": "ready", "status": "ok"}
    index.status = "failed"
    assert health(vault, index)["status"] == "failing"
    (vault.root / "wikismith.yaml").unlink()
    assert health(vault, SearchIndex(vault, None))["vault"] == "unreadable"


def test_failing_health_answers_503(vault: Vault) -> None:
    from starlette.testclient import TestClient

    from wikismith.httpserve import HEALTH

    server = build_server(vault, warm=False)
    HEALTH[server] = lambda: {"vault": "unreadable", "index": "building", "status": "failing"}
    with TestClient(http_app(server, TOKEN, "127.0.0.1")) as client:  # type: ignore[arg-type]
        assert client.get("/healthz").status_code == 503


@pytest.mark.parametrize("header", [None, "Bearer wrong", f"Basic {TOKEN}", f"Bearer {TOKEN}x"])
def test_rejects_without_the_token(base_url: str, header: str | None) -> None:
    headers = {"authorization": header} if header else {}
    response = httpx2.post(f"{base_url}/mcp", headers=headers, json={})
    assert response.status_code == 401


@pytest.mark.anyio
async def test_mcp_over_http(base_url: str) -> None:
    http = httpx2.AsyncClient(headers={"authorization": f"Bearer {TOKEN}"})
    async with Client(streamable_http_client(f"{base_url}/mcp", http_client=http), mode="legacy") as client:
        result = await client.call_tool("search", {"query": "San Marzano"})
    assert "wiki/garden/tomatoes.md" in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("host", "token", "no_auth", "ok"),
    [
        ("127.0.0.1", None, True, True),
        ("127.0.0.1", None, False, False),
        ("0.0.0.0", None, True, False),  # no auth only on loopback
        ("0.0.0.0", TOKEN, False, True),
        ("0.0.0.0", "short", False, False),
    ],
)
def test_check_token(host: str, token: str | None, no_auth: bool, ok: bool) -> None:
    if ok:
        check_token(host, token, no_auth)
    else:
        with pytest.raises(ValueError):
            check_token(host, token, no_auth)
