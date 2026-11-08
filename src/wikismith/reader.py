"""Read tools: pages, areas. Plain functions over the vault; `server.py` exposes them over MCP."""

from __future__ import annotations

from collections.abc import Callable

from wikismith.pages import Catalog, NotFound, Page
from wikismith.search import SearchIndex
from wikismith.vault import AccessDenied, Vault

Masker = Callable[[str], str]


SNIPPET_CHARS = 300


class Reader:
    def __init__(self, vault: Vault, index: SearchIndex | None = None, mask: Masker | None = None) -> None:
        self.vault = vault
        self.index = index or SearchIndex(vault, None)
        self.mask: Masker = mask or (lambda text: text)

    def search(self, query: str, top_k: int = 5, area: str | None = None, include_archive: bool = False) -> str:
        hits = self.index.search(query, top_k=max(1, min(top_k, 20)), area=area, include_archive=include_archive)
        if not hits:
            return f"No results for {query!r}."
        lines = []
        for n, hit in enumerate(hits, 1):
            c = hit.chunk
            where = c.title if not c.heading else f"{c.title} > {c.heading}"
            note = ""
            if c.kind == "capture":
                note = " [unforged capture: newer than the wiki, not yet reconciled into it]"
            elif c.kind == "archive":
                note = " [archived: outdated]"
            if c.masked:
                note += " [contains a masked secret]"
            snippet = " ".join(self.mask(c.text).split())
            if len(snippet) > SNIPPET_CHARS:
                snippet = snippet[:SNIPPET_CHARS].rsplit(" ", 1)[0] + " …"
            lines.append(f"{n}. **{where}** — {c.path}{note}\n   {snippet}")
        return "\n".join(lines)

    @property
    def archive_readable(self) -> bool:
        return self.vault.config.layout.archive in self.vault.config.access.read

    def catalog(self) -> Catalog:
        return Catalog(self.vault, include_archive=self.archive_readable)

    def read_page(self, page: str) -> str:
        """Read a page by path (`wiki/garden/tomatoes.md`, `raw/captures/...`) or name (`tomatoes`)."""
        if page.endswith(".md") and "/" in page:
            path = self.vault.resolve_readable(page)
            if not path.is_file():
                raise NotFound(f"No page at {page!r}")
            rel, text = self.vault.rel(path), path.read_text(encoding="utf-8")
        else:
            found = self.catalog().resolve(page)
            if not self.vault.is_readable(self.vault.root / found.path):
                raise AccessDenied(f"Not readable: {page!r}")
            rel, text = found.path, found.text
        archived = rel.startswith(self.vault.config.layout.archive.rstrip("/") + "/")
        note = " [archived: outdated]" if archived else ""
        return f"Path: {rel}{note}\n\n{self.mask(text)}"

    def list_areas(self) -> str:
        catalog = self.catalog()
        areas = catalog.areas()
        if not areas:
            return "The wiki has no areas yet."
        lines = []
        for area in areas:
            pages = catalog.in_area(area)
            index = next((p for p in pages if p.is_index), None)
            summary = self.mask(catalog.description(area) or (_summary(index) if index else "(no _index.md)"))
            count = sum(1 for p in pages if not p.is_index)
            lines.append(f"- **{area}** ({count} pages): {summary}")
        return "Areas:\n\n" + "\n".join(lines)

    def list_area(self, area: str) -> str:
        catalog = self.catalog()
        pages = catalog.in_area(area)
        if not pages:
            raise NotFound(f"No area {area!r}. Areas: {', '.join(catalog.areas()) or 'none'}")
        index = next((p for p in pages if p.is_index), None)
        listed = {link.target.lower() for link in index.doc.links} if index else set()
        out = []
        if index:
            out += [f"Index ({index.path}):", "", self.mask(index.doc.frontmatter.body.strip()), ""]
        out.append("Pages:")
        for p in pages:
            if p.is_index:
                continue
            note = "" if p.name.lower() in listed else " (not in the index)"
            out.append(f"- {p.path} — {p.title}{note}")
        return "\n".join(out)


def _summary(index: Page) -> str:
    """First paragraph after the H1 of an area index: the fallback when it has no `description:`."""
    for block in index.doc.frontmatter.body.split("\n\n"):
        block = block.strip()
        if block and not block.startswith("#"):
            return " ".join(block.split())
    return index.title
