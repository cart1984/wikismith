"""Pages of the vault, and Obsidian-style name resolution."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Literal

from wikismith.markdown import Document, parse
from wikismith.vault import Vault

PageKind = Literal["wiki", "archive", "capture"]

INDEX_NAME = "_index"


@dataclass
class Page:
    path: str  # vault-relative, POSIX
    kind: PageKind
    area: str | None
    text: str
    mtime: float

    @cached_property
    def doc(self) -> Document:
        return parse(self.text)

    @property
    def name(self) -> str:
        return Path(self.path).stem

    @property
    def title(self) -> str:
        title = self.doc.frontmatter.data.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
        for h in self.doc.headings:
            if h.level == 1:
                return h.text
        return self.name

    @property
    def is_index(self) -> bool:
        return self.name == INDEX_NAME


def load_page(vault: Vault, path: Path, kind: PageKind) -> Page:
    rel = vault.rel(path)
    area = None
    if kind in ("wiki", "archive"):
        base = vault.wiki_dir if kind == "wiki" else vault.archive_dir
        parts = path.resolve().relative_to(base.resolve()).parts
        area = parts[0] if len(parts) > 1 else None
    return Page(rel, kind, area, path.read_text(encoding="utf-8"), path.stat().st_mtime)


class NotFound(LookupError):
    pass


class Ambiguous(LookupError):
    def __init__(self, name: str, candidates: list[str]) -> None:
        super().__init__(f"{name!r} matches several pages: {', '.join(candidates)}")
        self.candidates = candidates


class Catalog:
    """All pages of a vault, rebuilt from disk on every call so it's never stale (about 10 ms for 100 pages)."""

    def __init__(self, vault: Vault, include_archive: bool = False) -> None:
        self.vault = vault
        self.pages: list[Page] = [load_page(vault, p, "wiki") for p in vault.markdown_files(vault.wiki_dir)]
        if include_archive:
            self.pages += [load_page(vault, p, "archive") for p in vault.markdown_files(vault.archive_dir)]
        self._by_path = {p.path: p for p in self.pages}

    def get(self, path: str) -> Page | None:
        return self._by_path.get(path)

    def areas(self) -> list[str]:
        return sorted({p.area for p in self.pages if p.kind == "wiki" and p.area})

    def in_area(self, area: str) -> list[Page]:
        return [p for p in self.pages if p.kind == "wiki" and p.area == area]

    def description(self, area: str) -> str:
        """What an area holds, in one line: `description:` in the frontmatter of its `_index.md`."""
        index = next((p for p in self.in_area(area) if p.is_index), None)
        value = index.doc.frontmatter.data.get("description") if index else None
        return " ".join(str(value).split()) if value else ""

    def resolve(self, name: str) -> Page:
        """Find a page by path, area-relative path or bare name, like Obsidian does.

        `wiki/garden/tomatoes.md`, `garden/tomatoes`, `tomatoes` and `Tomatoes` all work. Wiki pages win over archived
        ones with the same name.
        """
        key = name.strip().removesuffix(".md").strip("/")
        if not key:
            raise NotFound("empty page name")
        if (page := self._by_path.get(key + ".md")) is not None:
            return page
        lowered = key.lower()
        for kind in ("wiki", "archive"):
            matches = [p for p in self.pages if p.kind == kind and _matches(p, lowered)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise Ambiguous(name, sorted(p.path for p in matches))
        raise NotFound(f"No page named {name!r}")


def _matches(page: Page, key: str) -> bool:
    stem_path = page.path.removesuffix(".md").lower()
    return stem_path.endswith("/" + key) or page.name.lower() == key
