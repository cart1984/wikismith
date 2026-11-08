import datetime as dt
from pathlib import Path

import pytest
from mcp.client import Client

from conftest import git, make_vault
from wikismith.captures import Captures, InvalidCapture, slugify
from wikismith.scaffold import install_hook
from wikismith.secretscan import Scanner, SecretsBlocked
from wikismith.server import build_server
from wikismith.vault import Vault

TODAY = dt.date(2026, 2, 1)


def captures(vault: Vault, policy: str = "redact") -> Captures:
    vault = make_vault(vault.root, secrets={"policy": policy}, search={"semantic": False})
    return Captures(vault, Scanner(), today=lambda: TODAY)


def test_slugify() -> None:
    assert slugify("Router setup: VLANs & Wi-Fi!") == "router-setup-vlans-wi-fi"
    assert slugify("Grüße aus Köln") == "grusse-aus-koln"
    assert slugify("???") == "capture"
    assert len(slugify("x" * 200)) == 60


def test_capture_is_written_committed_and_pushed(vault: Vault, repo: Path, tmp_path: Path) -> None:
    result = captures(vault).add("Pepper harvest", "Picked 2 kg of chilis.", source="chat in repo garden-planner")
    assert result.path == "raw/captures/2026-02-01_pepper-harvest.md"
    assert (result.committed, result.pushed, result.warnings) == (True, True, ())
    text = (repo / result.path).read_text()
    assert text == (
        "# Pepper harvest\n\nDate: 2026-02-01\nSource: chat in repo garden-planner\n\nPicked 2 kg of chilis.\n"
    )
    assert git(repo, "log", "-1", "--format=%s") == "Add capture: Pepper harvest"
    assert git(tmp_path / "remote.git", "log", "-1", "--format=%s", "main") == "Add capture: Pepper harvest"


def test_only_the_capture_is_committed(vault: Vault, repo: Path) -> None:
    (repo / "wiki/garden/tomatoes.md").write_text("edited by hand\n")
    git(repo, "add", "wiki/garden/tomatoes.md")  # staged, but not ours to commit
    captures(vault).add("Note", "Something.")
    assert git(repo, "show", "--name-only", "--format=", "HEAD") == "raw/captures/2026-02-01_note.md"
    assert "wiki/garden/tomatoes.md" in git(repo, "diff", "--cached", "--name-only")


def test_never_overwrites(vault: Vault, repo: Path) -> None:
    first = captures(vault).add("Same title", "One.")
    second = captures(vault).add("Same title", "Two.")
    assert second.path == "raw/captures/2026-02-01_same-title-2.md"
    assert (repo / first.path).read_text().endswith("One.\n")


def test_leading_title_heading_not_duplicated(vault: Vault, repo: Path) -> None:
    result = captures(vault).add("Seeds", "# Seeds\n\nOrdered tomato seeds.")
    assert (repo / result.path).read_text().count("# Seeds") == 1


def test_secrets_redacted_by_default(vault: Vault, repo: Path) -> None:
    result = captures(vault).add("Router", "Admin password is hunter2-not-real.")
    assert "hunter2" not in (repo / result.path).read_text()
    assert result.warnings == ("Redacted before saving: password in a sentence.",)


def test_secrets_blocked(vault: Vault, repo: Path) -> None:
    with pytest.raises(SecretsBlocked):
        captures(vault, "block").add("Router", "Admin password is hunter2-not-real.")
    assert not list(vault.captures_dir.glob("2026-02-01_*"))


def test_invalid_input(vault: Vault) -> None:
    with pytest.raises(InvalidCapture):
        captures(vault).add("  ", "text")
    with pytest.raises(InvalidCapture):
        captures(vault).add("Title", "   ")


def test_without_git_the_vault_is_read_only(vault: Vault) -> None:
    before = sorted(vault.captures_dir.glob("*.md"))
    with pytest.raises(InvalidCapture, match="no git repository, so mcpedia is read-only"):
        captures(vault).add("Note", "No repo here.")
    assert sorted(vault.captures_dir.glob("*.md")) == before


def test_commit_captures_off_saves_for_the_owner(repo: Path) -> None:
    vault = make_vault(repo, git={"commit_captures": False}, search={"semantic": False})
    result = Captures(vault, Scanner()).add("Note", "I'll commit this myself.")
    assert (result.committed, result.pushed) == (False, False)
    assert "git.commit_captures is false" in result.warnings[0]
    assert (vault.root / result.path).is_file()
    assert result.path in git(repo, "status", "--porcelain")


def test_on_another_branch_saves_without_committing(vault: Vault, repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "forge/2026-02-01")
    result = captures(vault).add("Note", "During a forge.")
    assert not result.committed
    assert "branch 'forge/2026-02-01'" in result.warnings[0]


def test_push_failure_is_a_warning(vault: Vault, repo: Path) -> None:
    git(repo, "remote", "set-url", "origin", str(repo.parent / "missing.git"))
    result = captures(vault).add("Note", "Offline.")
    assert result.committed and not result.pushed
    assert any("push failed" in w for w in result.warnings)


@pytest.mark.anyio
async def test_add_capture_tool(vault: Vault, repo: Path) -> None:
    async with Client(build_server(vault, warm=False), mode="legacy") as client:
        result = await client.call_tool("add_capture", {"title": "Seed order", "content": "Ordered basil."})
        text = result.content[0].text  # type: ignore[union-attr]
        assert "committed and pushed" in text
        found = await client.call_tool("search", {"query": "basil seed order"})
        assert "[unforged capture" in found.content[0].text  # type: ignore[union-attr]
        blocked = await client.call_tool("add_capture", {"title": "", "content": "x"})
        assert blocked.is_error


def test_commits_pass_the_secret_hook_without_wikismith_on_path(
    vault: Vault, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_hook(repo)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # git, but no wikismith: the server puts its own on the hook's PATH
    result = captures(vault).add("Pepper harvest", "Picked 2 kg of chilis.")
    assert result.committed, result.warnings
