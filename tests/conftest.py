"""A small, made-up vault used across the tests. Nothing in here is real data."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wikismith.config import VaultConfig, parse_config
from wikismith.vault import Vault

FILES: dict[str, str] = {
    "wikismith.yaml": "name: test-vault\n",
    "CLAUDE.md": "# Test vault\n\nAreas: garden, kitchen.\n",
    "wiki/_log.md": (
        "# Log\n\n"
        "- raw/notes/2026-01-01_tomato-thread.md — 2026-01-02 — garden\n"
        "- raw/captures/2026-01-03_bread-notes.md — 2026-01-04 — kitchen\n"
    ),
    "wiki/garden/_index.md": (
        "---\ntitle: Garden\narea: garden\ndescription: Beds, vegetables and watering.\ntags: [index]\n"
        "updated: 2026-01-02\nsources: []\n---\n\n"
        "# Garden\n\n- [[my-garden]] — beds and sizes.\n- [[tomatoes]] — varieties and watering.\n"
    ),
    "wiki/garden/my-garden.md": (
        "---\ntitle: My garden\narea: garden\ntags: [reference]\nupdated: 2026-01-02\n"
        "sources: [raw/notes/2026-01-01_tomato-thread.md]\n---\n\n"
        "# My garden\n\nThree raised beds, 4 x 8 ft each. See [[tomatoes]].\n"
    ),
    "wiki/garden/tomatoes.md": (
        "---\ntitle: Tomatoes\narea: garden\ntags: [vegetables]\nupdated: 2026-01-02\n"
        "sources: [raw/notes/2026-01-01_tomato-thread.md]\n---\n\n"
        "# Tomatoes\n\n## Varieties\n\nSan Marzano for sauce, Sungold for snacking "
        "(source: raw/notes/2026-01-01_tomato-thread.md).\n\n"
        "## Watering\n\nDeep watering twice a week; mulch keeps the soil moist. "
        "Beds are in [[my-garden]]; compost from [[compost-bin]]; old notes in [[old-hose]].\n"
    ),
    "wiki/kitchen/_index.md": (
        "---\ntitle: Kitchen\narea: kitchen\ndescription: Baking, cooking and kitchen tools.\ntags: [index]\n"
        "updated: 2026-01-04\nsources: []\n---\n\n"
        "# Kitchen\n\n- [[sourdough|Sourdough bread]] — starter and schedule.\n"
    ),
    "wiki/kitchen/sourdough.md": (
        "---\ntitle: Sourdough\narea: kitchen\ntags: [baking]\nupdated: 2026-01-04\n"
        "sources: [raw/captures/2026-01-03_bread-notes.md]\n---\n\n"
        "# Sourdough\n\nFeed the starter 1:1:1 the night before baking. "
        "Bake at 250 °C in a Dutch oven (source: raw/captures/2026-01-03_bread-notes.md).\n"
    ),
    "wiki/kitchen/knife-care.md": (
        "---\ntitle: Knife care\narea: kitchen\ntags: []\nupdated: 2026-01-04\n---\n\n"
        "# Knife care\n\nHone weekly, sharpen twice a year.\n"
    ),
    "archive/garden/old-hose.md": (
        "---\ntitle: Old hose\narea: garden\ntags: []\nupdated: 2025-06-01\nsources: []\n---\n\n"
        "# Old hose\n\nReplaced in 2025.\n"
    ),
    "raw/notes/2026-01-01_tomato-thread.md": "Which tomatoes for sauce? San Marzano.\n",
    "raw/notes/2026-01-05_router-setup.md": "Router admin password is hunter2-not-real.\n",
    "raw/captures/2026-01-03_bread-notes.md": "# Bread notes\n\nStarter 1:1:1.\n",
    "raw/captures/2026-01-06_pepper-seeds.md": (
        "# Pepper seeds\n\nDate: 2026-01-06\n\nStart chili pepper seeds indoors eight weeks before the last frost.\n"
    ),
    ".obsidian/workspace.json": "{}\n",
    "wiki/.hidden/secret.md": "hidden\n",
}


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WIKISMITH_CACHE_DIR", str(tmp_path / "cache"))


@pytest.fixture(autouse=True)
def _isolated_home_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the real global git config or ~/.config/wikismith (`wikismith hook` writes to both)."""
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "home" / ".gitconfig"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    write_files(root, FILES)
    return root


def make_vault(root: Path, **config: object) -> Vault:
    return Vault(root, parse_config({"name": "test-vault", **config}))


@pytest.fixture
def vault(vault_root: Path) -> Vault:
    return make_vault(vault_root, search={"semantic": False})


@pytest.fixture
def config() -> VaultConfig:
    return parse_config({"name": "test-vault"})


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(vault_root: Path, tmp_path: Path) -> Path:
    """The test vault as a git repo with a local bare remote."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(vault_root, "init", "-q", "-b", "main")
    git(vault_root, "config", "user.email", "owner@example.com")
    git(vault_root, "config", "user.name", "Owner")
    git(vault_root, "add", "-A")
    git(vault_root, "commit", "-q", "-m", "init")
    git(vault_root, "remote", "add", "origin", str(remote))
    git(vault_root, "push", "-q", "-u", "origin", "main")
    return vault_root


@pytest.fixture
def anyio_backend() -> str:
    """Run `@pytest.mark.anyio` tests on asyncio only."""
    return "asyncio"
