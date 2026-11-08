"""The vault on disk: its folders, which paths agents may read, its cache folder and its dates."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from wikismith.config import VaultConfig, load_config


def default_cache_dir(configured: str | None) -> Path:
    """wikismith's cache root: models live here, each vault's own files in a folder below (`Vault.cache_dir`)."""
    if env := os.environ.get("WIKISMITH_CACHE_DIR"):
        return Path(env)
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "wikismith"


def model_dir(configured: str | None) -> Path:
    """Where embedding models live: `WIKISMITH_MODEL_DIR` (the Docker image's own /models), else the cache root."""
    if env := os.environ.get("WIKISMITH_MODEL_DIR"):
        return Path(env)
    return default_cache_dir(configured) / "models"


class AccessDenied(ValueError):
    """A path outside what agents may read, or one that tries to escape the vault."""


class Vault:
    def __init__(self, root: Path, config: VaultConfig) -> None:
        self.root = root.resolve()
        self.config = config
        layout = config.layout
        self.raw_dir = self.root / layout.raw
        self.wiki_dir = self.root / layout.wiki
        self.archive_dir = self.root / layout.archive
        self.captures_dir = self.root / layout.captures
        self.reports_dir = self.root / layout.reports
        self.log_file = self.root / layout.log
        self.instructions_file = self.root / layout.instructions

    @classmethod
    def open(cls, root: Path) -> Vault:
        return cls(root, load_config(root))

    @property
    def cache_dir(self) -> Path:
        """This vault's embeddings and jobs: named after the vault plus a hash of its path, so two vaults never share
        one (moving a vault costs one re-embedding)."""
        slug = re.sub(r"[^a-z0-9]+", "-", self.config.name.lower()).strip("-") or "vault"
        digest = hashlib.sha256(str(self.root).encode()).hexdigest()[:6]
        return default_cache_dir(self.config.search.cache_dir) / f"{slug}-{digest}"

    def today(self) -> dt.date:
        """Today in the vault's time zone (`timezone`), else the machine's."""
        if self.config.timezone:
            return dt.datetime.now(ZoneInfo(self.config.timezone)).date()
        return dt.date.today()

    def readable_roots(self) -> list[Path]:
        roots = [self.root / folder for folder in self.config.access.read]
        if self.config.access.search_captures:
            roots.append(self.captures_dir)
        return roots

    def resolve_readable(self, rel_path: str) -> Path:
        """Turn an agent-supplied vault-relative path into an absolute one, or refuse.

        Refuses absolute paths, `..`, hidden parts (`.git`, `.obsidian`, ...), symlinks that lead elsewhere, and
        anything outside the readable folders. Never says why in detail, so a refusal doesn't confirm that a file
        exists.
        """
        if not rel_path or "\x00" in rel_path or "\\" in rel_path:
            raise AccessDenied(f"Not readable: {rel_path!r}")
        pure = PurePosixPath(rel_path)
        if pure.is_absolute() or any(part in ("..", "") or part.startswith(".") for part in pure.parts):
            raise AccessDenied(f"Not readable: {rel_path!r}")
        resolved = (self.root / pure).resolve()
        if not any(_is_within(resolved, r.resolve()) for r in self.readable_roots()):
            raise AccessDenied(f"Not readable: {rel_path!r}")
        return resolved

    def is_readable(self, path: Path) -> bool:
        try:
            self.resolve_readable(self.rel(path))
        except (AccessDenied, ValueError):
            return False
        return True

    def rel(self, path: Path) -> str:
        """Vault-relative POSIX path, the form agents see and cite."""
        return path.resolve().relative_to(self.root).as_posix()

    def markdown_files(self, folder: Path) -> Iterator[Path]:
        """All `.md` files under a folder, sorted, skipping hidden folders and escaping symlinks."""
        if not folder.is_dir():
            return
        base = folder.resolve()
        for path in sorted(folder.rglob("*.md")):
            rel_parts = path.relative_to(folder).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            if path.is_file() and _is_within(path.resolve(), base):
                yield path


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents
