"""End-to-end through a real MCP client, in-process."""

import pytest
from mcp.client import Client
from mcp.types import TextContent

from wikismith.server import build_server
from wikismith.vault import Vault

pytestmark = pytest.mark.anyio


async def call(vault: Vault, tool: str, **args: object) -> tuple[bool, str]:
    async with Client(build_server(vault, warm=False), mode="legacy") as client:
        result = await client.call_tool(tool, args)
    content = result.content[0]
    assert isinstance(content, TextContent)
    return bool(result.is_error), content.text


async def test_prompts(vault: Vault) -> None:
    async with Client(build_server(vault, warm=False), mode="legacy") as client:
        prompts = {p.name: p for p in (await client.list_prompts()).prompts}
        assert set(prompts) == {"forge", "audit", "capture"}
        texts = {}
        for name in prompts:
            message = (await client.get_prompt(name)).messages[0]
            assert isinstance(message.content, TextContent)
            texts[name] = message.content.text
    assert "run_forge" in texts["forge"] and "list_pending" in texts["forge"]
    assert "run_audit" in texts["audit"]
    assert "add_capture" in texts["capture"]
    assert all(
        "Never edit vault files yourself" in t or "Never write vault files yourself" in t for t in texts.values()
    )


async def test_tools_are_listed_read_only(vault: Vault) -> None:
    async with Client(build_server(vault, warm=False), mode="legacy") as client:
        tools = (await client.list_tools()).tools
    names = {t.name for t in tools}
    assert {"search", "read_page", "list_areas", "list_area", "vault_guide", "add_capture"} <= names
    for t in tools:
        assert t.annotations is not None
        if t.name in ("add_capture", "answer_questions", "run_forge", "run_audit"):
            assert t.annotations.read_only_hint is False and t.annotations.destructive_hint is False
        elif t.name in ("merge_forge", "discard_forge"):
            assert t.annotations.destructive_hint is True
        else:
            assert t.annotations.read_only_hint is True


async def test_read_page(vault: Vault) -> None:
    is_error, text = await call(vault, "read_page", page="tomatoes")
    assert not is_error and "San Marzano" in text


async def test_read_page_denied_is_a_tool_error(vault: Vault) -> None:
    is_error, text = await call(vault, "read_page", page="raw/notes/2026-01-05_router-setup.md")
    assert is_error and "Not readable" in text
    assert "hunter2" not in text


async def test_list_area(vault: Vault) -> None:
    is_error, text = await call(vault, "list_area", area="garden")
    assert not is_error and "wiki/garden/tomatoes.md" in text
