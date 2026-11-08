"""Adding captures: the only way agents write to the vault."""

from __future__ import annotations

import datetime as dt
import re
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from wikismith.gitops import NO_GIT, Git
from wikismith.secretscan import Scanner, apply_policy
from wikismith.vault import Vault

MAX_TITLE = 120
MAX_CONTENT = 100_000


class InvalidCapture(ValueError):
    pass


@dataclass(frozen=True)
class CaptureResult:
    path: str
    committed: bool
    pushed: bool
    warnings: tuple[str, ...]

    def summary(self) -> str:
        if self.pushed:
            state = "committed and pushed"
        elif self.committed:
            state = "committed"
        else:
            state = "saved (not committed)"
        lines = [f"Captured to {self.path}, {state}."]
        lines += [f"Note: {w}" for w in self.warnings]
        return "\n".join(lines)


class Captures:
    def __init__(self, vault: Vault, scanner: Scanner, today: Callable[[], dt.date] | None = None) -> None:
        self.vault = vault
        self.scanner = scanner
        self.git = Git(vault.root, vault.config)
        self.today = today or vault.today
        self._lock = threading.Lock()

    def add(
        self, title: str, content: str, source: str | None = None, frontmatter: dict[str, Any] | None = None
    ) -> CaptureResult:
        """Save a capture. `frontmatter` is for mcpedia itself (e.g. the areas the owner approved), never for agents."""
        if not self.git.is_repo():
            raise InvalidCapture(NO_GIT)
        title = " ".join(title.split())
        content = content.strip()
        if not title or len(title) > MAX_TITLE:
            raise InvalidCapture(f"The title must be 1 to {MAX_TITLE} characters.")
        if not content:
            raise InvalidCapture("The content is empty.")
        if len(content) > MAX_CONTENT:
            raise InvalidCapture(f"The content is too long (over {MAX_CONTENT} characters); split it up.")
        date = self.today().isoformat()
        content = re.sub(rf"^#\s+{re.escape(title)}\s*\n", "", content, flags=re.IGNORECASE).strip()
        header = f"# {title}\n\nDate: {date}\n"
        if source and source.strip():
            header += f"Source: {' '.join(source.split())}\n"
        if frontmatter:
            header = "---\n" + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True) + "---\n\n" + header
        result = apply_policy(self.scanner, f"{header}\n{content}\n", self.vault.config.secrets.policy)
        warnings = [result.warning] if result.warning else []

        with self._lock:
            path = self._create(f"{date}_{slugify(title)}", result.text)
            rel = self.vault.rel(path)
            commit = self.git.commit([rel], f"Add capture: {title}")
        return CaptureResult(rel, commit.committed, commit.pushed, tuple(warnings + list(commit.warnings)))

    def _create(self, stem: str, text: str) -> Path:
        self.vault.captures_dir.mkdir(parents=True, exist_ok=True)
        for n in range(1, 100):
            path = self.vault.captures_dir / (f"{stem}.md" if n == 1 else f"{stem}-{n}.md")
            try:
                with path.open("x", encoding="utf-8") as f:  # "x": never overwrite an existing capture
                    f.write(text)
                return path
            except FileExistsError:
                continue
        raise InvalidCapture("Too many captures with this title today.")


# Letters that Unicode normalization can't turn into ASCII.
_TRANSLITERATE = str.maketrans({"ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "ł": "l", "đ": "d", "þ": "th"})


def slugify(title: str, max_len: int = 60) -> str:
    plain = title.lower().translate(_TRANSLITERATE)
    ascii_title = unicodedata.normalize("NFKD", plain).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "capture"
