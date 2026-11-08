"""The forge log (`wiki/_log.md`): which raw files have been forged into the wiki."""

from __future__ import annotations

import re
from pathlib import Path

from wikismith.vault import Vault

# "- raw/notes/x.md — 2026-01-02 — garden", optionally with the path in backticks. The path runs up to the first " — ",
# so file names with spaces work; without a dash it's the whole rest of the line.
_ENTRY = re.compile(r"^\s*[-*]\s+`?(.+?\.\w+)`?\s*(?:\s—\s|$)")


def logged_paths(vault: Vault) -> set[str]:
    if not vault.log_file.is_file():
        return set()
    return logged_in(vault.log_file.read_text(encoding="utf-8"))


def logged_in(log: str) -> set[str]:
    """The raw files a forge log's text lists."""
    return {m.group(1) for line in log.splitlines() if (m := _ENTRY.match(line))}


def unforged_raw_files(vault: Vault) -> list[Path]:
    """Raw files not listed in the log yet, i.e. still waiting to be forged."""
    logged = logged_paths(vault)
    if not vault.raw_dir.is_dir():
        return []
    files = []
    for path in sorted(vault.raw_dir.rglob("*")):
        rel_parts = path.relative_to(vault.raw_dir).parts
        if path.is_file() and not any(p.startswith(".") for p in rel_parts) and vault.rel(path) not in logged:
            files.append(path)
    return files


def unforged_captures(vault: Vault) -> list[Path]:
    captures = vault.captures_dir.resolve()
    return [p for p in unforged_raw_files(vault) if p.suffix == ".md" and captures in p.resolve().parents]
