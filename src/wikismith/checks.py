"""Mechanical vault checks: everything an audit needs that doesn't take judgment."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, get_args

from wikismith.gitops import Git, GitError
from wikismith.logfile import unforged_raw_files
from wikismith.media import is_image, too_large
from wikismith.pages import Catalog, Page
from wikismith.secretscan import Scanner
from wikismith.vault import Vault

CheckName = Literal["links", "orphans", "frontmatter", "index", "unforged", "secrets", "large"]
CHECKS: tuple[CheckName, ...] = get_args(CheckName)
REQUIRED_KEYS = ("title", "area", "tags", "updated", "sources")


@dataclass(frozen=True)
class Issue:
    path: str
    message: str


class Checker:
    def __init__(self, vault: Vault, scanner: Scanner) -> None:
        self.vault = vault
        self.scanner = scanner

    def run(self, names: tuple[CheckName, ...] = CHECKS) -> dict[CheckName, list[Issue]]:
        catalog = Catalog(self.vault, include_archive=True)
        runners: dict[CheckName, Callable[[Catalog], list[Issue]]] = {
            "links": self.links,
            "orphans": self.orphans,
            "frontmatter": self.frontmatter,
            "index": self.index,
            "unforged": self.unforged,
            "secrets": self.secrets,
            "large": self.large,
        }
        return {name: runners[name](catalog) for name in names}

    def _content_pages(self, catalog: Catalog, kinds: tuple[str, ...] = ("wiki",)) -> list[Page]:
        log = self.vault.rel(self.vault.log_file)
        return [p for p in catalog.pages if p.kind in kinds and p.path != log]

    def links(self, catalog: Catalog) -> list[Issue]:
        """Wikilinks that lead nowhere, and wiki links that lead into the archive."""
        names = _link_keys(catalog.pages)
        archived = _link_keys([p for p in catalog.pages if p.kind == "archive"])
        wiki_names = _link_keys([p for p in catalog.pages if p.kind == "wiki"])
        files = {p.name for d in (self.vault.wiki_dir, self.vault.archive_dir) if d.is_dir() for p in d.rglob("*")}
        issues = []
        for page in self._content_pages(catalog):
            for link in page.doc.links:
                key = _key(link.target)
                suffix = PurePosixPath(link.target).suffix
                if suffix and suffix != ".md":
                    if PurePosixPath(link.target).name not in files:
                        issues.append(Issue(page.path, f"[[{link.target}]]: file not found"))
                elif key not in names:
                    issues.append(Issue(page.path, f"[[{link.target}]]: no such page"))
                elif key in archived and key not in wiki_names:
                    issues.append(Issue(page.path, f"[[{link.target}]]: points to an archived page"))
        return issues

    def orphans(self, catalog: Catalog) -> list[Issue]:
        """Wiki pages nothing links to (area indexes and the log don't need incoming links)."""
        linked = {_key(link.target) for p in self._content_pages(catalog) for link in p.doc.links}
        return [
            Issue(p.path, "no page links here")
            for p in self._content_pages(catalog)
            if not p.is_index and not (_page_keys(p) & linked)
        ]

    def frontmatter(self, catalog: Catalog) -> list[Issue]:
        issues = []
        for page in self._content_pages(catalog, ("wiki", "archive")):
            fm = page.doc.frontmatter
            if not fm.present:
                issues.append(Issue(page.path, "no frontmatter"))
                continue
            if fm.error:
                issues.append(Issue(page.path, f"frontmatter: {fm.error}"))
                continue
            missing = [k for k in REQUIRED_KEYS if k not in fm.data]
            if missing:
                issues.append(Issue(page.path, f"frontmatter is missing: {', '.join(missing)}"))
            if page.area and fm.data.get("area") not in (None, page.area):
                issues.append(Issue(page.path, f"area is {fm.data['area']!r} but the page is in {page.area!r}"))
            updated = fm.data.get("updated")
            if updated is not None and not isinstance(updated, dt.date):
                issues.append(Issue(page.path, f"updated is not a YYYY-MM-DD date: {updated!r}"))
            for key in ("tags", "sources"):
                if key in fm.data and not isinstance(fm.data[key], list):
                    issues.append(Issue(page.path, f"{key} should be a list"))
        return issues

    def index(self, catalog: Catalog) -> list[Issue]:
        """Areas without an `_index.md` or a `description:` in it, and pages their area index doesn't list."""
        issues = []
        for area in catalog.areas():
            pages = catalog.in_area(area)
            idx = next((p for p in pages if p.is_index), None)
            if idx is None:
                issues.append(Issue(f"{self.vault.config.layout.wiki}/{area}", "area has no _index.md"))
                continue
            if not catalog.description(area):
                issues.append(
                    Issue(idx.path, "no `description:` in the frontmatter (agents see it as the area's summary)")
                )
            listed = {_key(link.target) for link in idx.doc.links}
            issues += [
                Issue(p.path, f"not listed in {idx.path}")
                for p in pages
                if not p.is_index and not (_page_keys(p) & listed)
            ]
        return issues

    def unforged(self, catalog: Catalog) -> list[Issue]:
        return [Issue(self.vault.rel(p), "not in the forge log yet") for p in unforged_raw_files(self.vault)]

    def secrets(self, catalog: Catalog) -> list[Issue]:
        """Secrets in the wiki and archive (values are never shown)."""
        issues = []
        for page in self._content_pages(catalog, ("wiki", "archive")):
            for f in self.scanner.scan(page.text):
                issues.append(Issue(page.path, f"line {f.line}: looks like a {f.description}"))
        return issues

    def large(self, catalog: Catalog) -> list[Issue]:
        """Files over the vault's file limits (`files.*`): committed before the hook, without it, or kept on purpose."""
        limits = self.vault.config.files
        issues = []
        for rel in self._files():
            path = self.vault.root / rel
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if not too_large(path, size, limits):
                continue
            kb = size // 1024
            if is_image(path):
                why = f"image, {kb} KB (limits: {limits.image_max_kb} KB, {limits.image_max_px} px long edge)"
                issues.append(Issue(rel, f"{why}: `wikismith shrink {rel}` makes a smaller copy"))
            else:
                issues.append(Issue(rel, f"{kb} KB, over the {limits.max_kb} KB limit"))
        return issues

    def _files(self) -> list[str]:
        """The vault's files: the ones git tracks, or every file without git."""
        try:
            listing = Git(self.vault.root, self.vault.config).run("-c", "core.quotePath=false", "ls-files", "-z")
            return [name for name in listing.split("\0") if name]
        except GitError:
            root = self.vault.root
            return sorted(
                str(p.relative_to(root))
                for p in root.rglob("*")
                if p.is_file() and ".git" not in p.relative_to(root).parts
            )


def format_report(results: dict[CheckName, list[Issue]], limit: int = 50) -> str:
    lines = []
    for name, issues in results.items():
        if not issues:
            lines.append(f"## {name}: OK")
            continue
        lines.append(f"## {name}: {len(issues)} issue(s)")
        lines += [f"- {i.path}: {i.message}" for i in issues[:limit]]
        if len(issues) > limit:
            lines.append(f"- ... and {len(issues) - limit} more")
    return "\n".join(lines)


def _key(target: str) -> str:
    return target.strip().removesuffix(".md").strip("/").lower()


def _page_keys(page: Page) -> set[str]:
    """Every way a link can point at this page: name, area/name, full path."""
    stem = page.path.removesuffix(".md").lower()
    parts = stem.split("/")
    return {"/".join(parts[i:]) for i in range(len(parts))}


def _link_keys(pages: list[Page]) -> set[str]:
    return {k for p in pages for k in _page_keys(p)}
