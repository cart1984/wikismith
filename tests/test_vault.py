import os
from pathlib import Path

import pytest

from conftest import make_vault, write_files
from wikismith.vault import AccessDenied, Vault


def test_reads_wiki_pages(vault: Vault) -> None:
    path = vault.resolve_readable("wiki/garden/tomatoes.md")
    assert path == vault.root / "wiki/garden/tomatoes.md"


def test_reads_captures_when_searchable(vault: Vault) -> None:
    assert vault.resolve_readable("raw/captures/2026-01-06_pepper-seeds.md").is_file()


@pytest.mark.parametrize(
    "path",
    [
        "raw/notes/2026-01-05_router-setup.md",  # raw outside captures
        "../vault/wiki/garden/tomatoes.md",
        "wiki/../raw/notes/2026-01-05_router-setup.md",
        "/etc/passwd",
        "wiki/.hidden/secret.md",
        ".obsidian/workspace.json",
        "wikismith.yaml",
        "CLAUDE.md",
        "wiki\\garden\\tomatoes.md",
        "wiki/garden/tomatoes.md\x00",
        "",
    ],
)
def test_refuses(vault: Vault, path: str) -> None:
    with pytest.raises(AccessDenied):
        vault.resolve_readable(path)


def test_archive_readable_unless_configured_away(vault: Vault, vault_root: Path) -> None:
    assert vault.resolve_readable("archive/garden/old-hose.md").is_file()
    with pytest.raises(AccessDenied):
        make_vault(vault_root, access={"read": ["wiki"]}).resolve_readable("archive/garden/old-hose.md")


def test_captures_not_readable_when_disabled(vault_root: Path) -> None:
    vault = make_vault(vault_root, access={"search_captures": False})
    with pytest.raises(AccessDenied):
        vault.resolve_readable("raw/captures/2026-01-06_pepper-seeds.md")


def test_refuses_symlink_escape(vault: Vault) -> None:
    link = vault.wiki_dir / "garden/router.md"
    os.symlink(vault.root / "raw/notes/2026-01-05_router-setup.md", link)
    with pytest.raises(AccessDenied):
        vault.resolve_readable("wiki/garden/router.md")
    assert link not in list(vault.markdown_files(vault.wiki_dir))


def test_markdown_files_skips_hidden(vault: Vault) -> None:
    files = [vault.rel(p) for p in vault.markdown_files(vault.wiki_dir)]
    assert "wiki/garden/tomatoes.md" in files
    assert not any("/.hidden/" in f for f in files)
    assert files == sorted(files)


def test_open_reads_config(vault_root: Path) -> None:
    assert Vault.open(vault_root).config.name == "test-vault"


def test_each_vault_has_its_own_cache_folder(tmp_path: Path) -> None:
    write_files(tmp_path / "a", {"wikismith.yaml": "name: Personal Notes\n"})
    write_files(tmp_path / "b", {"wikismith.yaml": "name: Personal Notes\n"})  # same name, other folder
    first, second = Vault.open(tmp_path / "a").cache_dir, Vault.open(tmp_path / "b").cache_dir
    assert first != second and first.parent == second.parent == tmp_path / "cache"
    assert first.name.startswith("personal-notes-") and len(first.name) == len("personal-notes-") + 6


def test_today_follows_the_timezone(vault_root: Path) -> None:
    import datetime as dt
    from zoneinfo import ZoneInfo

    vault = make_vault(vault_root, timezone="Pacific/Kiritimati")
    assert vault.today() == dt.datetime.now(ZoneInfo("Pacific/Kiritimati")).date()
    assert make_vault(vault_root).today() == dt.date.today()
