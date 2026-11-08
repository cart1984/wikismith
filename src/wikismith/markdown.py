"""Just enough Markdown parsing for a wiki: frontmatter, wikilinks, headings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

_FENCE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.MULTILINE | re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_WIKILINK = re.compile(r"!?\[\[([^\]\|#\n]*)(?:#([^\]\|\n]*))?(?:\|([^\]\n]*))?\]\]")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Frontmatter:
    data: dict[str, Any]
    body: str
    present: bool
    error: str | None = None


@dataclass(frozen=True)
class Link:
    target: str
    heading: str | None = None
    alias: str | None = None


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    offset: int


@dataclass(frozen=True)
class Document:
    frontmatter: Frontmatter
    links: tuple[Link, ...] = ()
    headings: tuple[Heading, ...] = ()


def parse_frontmatter(text: str) -> Frontmatter:
    """Split `---` YAML frontmatter from the body. A broken block is reported, not raised."""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return Frontmatter({}, text, present=False)
    lines = text.splitlines(keepends=True)
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            block = "".join(lines[1:i])
            body = "".join(lines[i + 1 :])
            try:
                data = yaml.safe_load(block)
            except yaml.YAMLError as e:
                return Frontmatter({}, body, present=True, error=yaml_error(e))
            if data is None:
                data = {}
            if not isinstance(data, dict):
                return Frontmatter({}, body, present=True, error="frontmatter is not a mapping")
            return Frontmatter(data, body, present=True)
    return Frontmatter({}, text, present=True, error="frontmatter is not closed with ---")


def blank_code(text: str, inline: bool = True) -> str:
    """Replace fenced code blocks (and inline code, if `inline`) with spaces, keeping newlines and every character's
    position, so links and headings inside code are ignored and offsets still point into the original text."""
    text = _FENCE.sub(_spaces, text)
    return _INLINE_CODE.sub(_spaces, text) if inline else text


def _spaces(m: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", m.group(0))


def wikilinks(text: str) -> tuple[Link, ...]:
    links = []
    for m in _WIKILINK.finditer(blank_code(text)):
        target = m.group(1).strip()
        if target:
            links.append(Link(target, (m.group(2) or "").strip() or None, (m.group(3) or "").strip() or None))
    return tuple(links)


def headings(text: str) -> tuple[Heading, ...]:
    """Headings with their character offset in `text`. Fenced code is skipped; inline code is kept, since it can be part
    of a heading's text."""
    blanked = blank_code(text, inline=False)
    return tuple(Heading(len(m.group(1)), m.group(2), m.start()) for m in _HEADING.finditer(blanked))


def parse(text: str) -> Document:
    fm = parse_frontmatter(text)
    return Document(fm, wikilinks(fm.body), headings(fm.body))


def yaml_error(e: yaml.YAMLError, first_line: int = 2) -> str:
    """One line: where and what (PyYAML's own message spans several lines). `first_line` is the file line where the YAML
    starts (2 for frontmatter, after the opening `---`)."""
    problem = getattr(e, "problem", None) or str(e).splitlines()[0]
    mark = getattr(e, "problem_mark", None)
    where = f" on line {mark.line + first_line}" if mark is not None else ""
    return f"invalid YAML{where}: {problem}"
