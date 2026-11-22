"""Mechanical vault checks: everything an audit needs that doesn't take judgment."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, get_args

from wikismith.gitops import Git, GitError
from wikismith.logfile import unforged_raw_files
from wikismith.media import is_image, too_large
from wikismith.pages import Catalog, Page
from wikismith.secretscan import Scanner
from wikismith.vault import Vault

CheckName = Literal["links", "orphans", "frontmatter", "index", "names", "unforged", "secrets", "large", "similar"]
ALL_CHECKS: tuple[CheckName, ...] = get_args(CheckName)
# What `wikismith check` runs by default. `similar` only guesses, so it runs when asked for: a guess mustn't fail the
# check, hold a merge or make a forge rewrite pages that only look alike.
CHECKS: tuple[CheckName, ...] = tuple(c for c in ALL_CHECKS if c != "similar")
REQUIRED_KEYS = ("title", "area", "tags", "updated", "sources")
SHINGLE = 8  # words per run compared by the `similar` check
SIMILAR_SHARE = 0.2  # report a pair when this share of the smaller page's runs also appears in the other
SIMILAR_MIN_RUNS = 5  # ...and they share at least this many runs, so two short stubs don't match on boilerplate
LONG_LINE = 2000  # characters; agents' file readers may cut lines longer than this
MAX_TEXT_BYTES = 10 * 1024 * 1024  # larger raw files are reported by size only


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
            "names": self.names,
            "unforged": self.unforged,
            "secrets": self.secrets,
            "large": self.large,
            "similar": self.similar,
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
        # Images and other files stay in raw/ and are embedded from there, so raw/ counts as well.
        dirs = (self.vault.wiki_dir, self.vault.archive_dir, self.vault.raw_dir)
        files = {p.name for d in dirs if d.is_dir() for p in d.rglob("*") if p.is_file()}
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

    def names(self, catalog: Catalog) -> list[Issue]:
        """Page names used more than once across the wiki and archive: `[[name]]` and `read_page("name")` can't tell
        such pages apart."""
        by_name: dict[str, list[str]] = {}
        for page in self._content_pages(catalog, ("wiki", "archive")):
            if not page.is_index:
                by_name.setdefault(page.name.lower(), []).append(page.path)
        issues = []
        for paths in by_name.values():
            for path in paths if len(paths) > 1 else []:
                others = ", ".join(p for p in paths if p != path)
                issues.append(Issue(path, f"same page name as {others}: rename one, e.g. with its area as a prefix"))
        return issues

    def unforged(self, catalog: Catalog) -> list[Issue]:
        """Raw files not forged yet, with their size, so a forge can plan to read long ones in chunks."""
        return [
            Issue(self.vault.rel(p), f"not in the forge log yet ({_size_note(p)})")
            for p in unforged_raw_files(self.vault)
        ]

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

    def similar(self, catalog: Catalog) -> list[Issue]:
        """Pairs of wiki pages that share much of their text (runs of the same words): possible duplicates, for an
        agent or the owner to judge. Reported on the smaller page of each pair."""
        pages = [p for p in self._content_pages(catalog) if not p.is_index]
        runs = {p.path: _runs(p) for p in pages}
        holders: dict[int, list[str]] = {}
        for path, own in runs.items():
            for run in own:
                holders.setdefault(run, []).append(path)
        shared: dict[tuple[str, str], int] = {}
        for paths in holders.values():
            for i, a in enumerate(paths):
                for b in paths[i + 1 :]:
                    shared[(a, b)] = shared.get((a, b), 0) + 1
        issues = []
        for (a, b), n in sorted(shared.items()):
            small, other = (a, b) if len(runs[a]) <= len(runs[b]) else (b, a)
            share = n / len(runs[small])
            if n >= SIMILAR_MIN_RUNS and share >= SIMILAR_SHARE:
                issues.append(Issue(small, f"{share:.0%} of its text also appears in {other}: the same topic?"))
        return sorted(issues, key=lambda i: (i.path, i.message))

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


_CITATION = re.compile(r"\(source:[^)]*\)")
_WORD = re.compile(r"\w+")


def _runs(page: Page) -> set[int]:
    """Hashes of the page's runs of `SHINGLE` consecutive words, without frontmatter and source citations (two pages
    citing the same raw file aren't alike for that)."""
    words = _WORD.findall(_CITATION.sub(" ", page.doc.frontmatter.body).lower())
    return {hash(tuple(words[i : i + SHINGLE])) for i in range(len(words) - SHINGLE + 1)}


def _size_note(path: Path) -> str:
    """The size of a raw file: "312 lines" for text (plus its longest line when that is long), "48 KB" otherwise."""
    try:
        size = path.stat().st_size
    except OSError:
        return "unreadable"
    kb = f"{max(1, size // 1024)} KB"
    if size > MAX_TEXT_BYTES:  # a video or archive needn't be read to say how big it is
        return kb
    try:
        text = path.read_bytes().decode("utf-8")
    except OSError:
        return "unreadable"
    except UnicodeDecodeError:
        return kb
    lines = text.splitlines()
    note = f"{len(lines)} line{'' if len(lines) == 1 else 's'}"
    longest = max((len(line) for line in lines), default=0)
    if longest > LONG_LINE:
        note += f", longest line {longest} characters"
    return note


def _key(target: str) -> str:
    return target.strip().removesuffix(".md").strip("/").lower()


def _page_keys(page: Page) -> set[str]:
    """Every way a link can point at this page: name, area/name, full path."""
    stem = page.path.removesuffix(".md").lower()
    parts = stem.split("/")
    return {"/".join(parts[i:]) for i in range(len(parts))}


def _link_keys(pages: list[Page]) -> set[str]:
    return {k for p in pages for k in _page_keys(p)}
