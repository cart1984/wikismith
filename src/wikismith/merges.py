"""Merge verification: what a job branch changes, and whether mcpedia can merge it without the owner.

The server doesn't trust the agent's own account of its changes; it checks the branch against its merge base. A
branch merges by itself only if every change is one of these:

- **additive**: new pages, new lines, frontmatter that only grows (`updated` may change, lists may get longer),
  and index entries extended at the end (same links, the old text unchanged at the start);
- **a verified move**: lines removed from one place appear unchanged in the branch (page merges and splits, reordered
  sections); index entries of pages moved out of the wiki may go;
- **an unchanged archive move**: a page renamed from `wiki/` to `archive/` with identical content;
- **a mechanical fix**: a line whose only change is a link, if the branch leaves fewer check issues than it found;
- **a volatile line** (with `review.auto_merge: volatile`, the default): a line that was provisional before the branch
  changed it: in a section whose heading carries one of `review.volatile_tags` (`## Plan #plan`, with its
  subsections), or carrying such a tag itself. Open questions, plans and status are expected to be replaced by
  newer information. At most `review.volatile_max_lines` per branch; adding a tag to an existing line still waits.

Everything else waits: rewritten or removed text, changed frontmatter values, deletions whose text appears nowhere,
new areas without the owner's approval, binary files, flagged pages, `wikismith.yaml`, anything outside the job's
folders, new check issues, and possible secrets or personal data. A branch that changes `raw/` is never merged.
"""

from __future__ import annotations

import difflib
import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from wikismith.config import CONFIG_FILE, JobKind
from wikismith.config import Review as ReviewSettings
from wikismith.gitops import Git, GitError
from wikismith.markdown import parse_frontmatter
from wikismith.secretscan import Scanner
from wikismith.vault import Vault

CheckIssues = frozenset[tuple[str, str]]  # (path, message) from `wikismith check`
LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
HEADING = re.compile(r"^(#{1,6})\s")
FENCE = re.compile(r"^\s*(```|~~~)")
EXAMPLES = 3  # unverified lines shown per file
DIFF_LINES_PER_FILE = 60  # diff lines shown per file that needs the owner's OK ...
DIFF_LINES_TOTAL = 200  # ... and in all
REVIEW_HEADING = "### Changes that need your OK"


@dataclass
class Review:
    """The verdict on a branch: why it waits (empty: mergeable), and what was verified."""

    waits: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)  # never merged, even by the owner
    blocking: dict[str, str] = field(default_factory=dict)  # file -> why it needs the owner's OK

    def wait(self, reason: str, path: str | None = None, why: str | None = None) -> None:
        """Record why the branch waits; `path` and a short `why` name the file the owner has to look at."""
        self.waits.append(reason)
        if path and why:
            self.block(path, why)

    def block(self, path: str, why: str) -> None:
        """Mark a file as one the owner has to look at; its diff is shown with the summary."""
        self.blocking[path] = f"{self.blocking[path]}; {why}" if path in self.blocking else why

    @property
    def mergeable(self) -> bool:
        return not self.waits and not self.refused


@dataclass
class _Change:
    status: str  # A, M, D, R
    path: str  # the new path (the old one for D)
    old_path: str | None
    old: str | None  # text at the merge base
    new: str | None  # text on the branch


def review(
    vault: Vault,
    branch: str,
    kind: JobKind,
    before: CheckIssues | None = None,
    after: CheckIssues | None = None,
) -> Review:
    """Verify a job branch against its merge base with the main branch. `before` and `after` are the check issues
    of the worktree before and after the agent ran (unknown for a branch reviewed later)."""
    git = Git(vault.root, vault.config)
    layout, config = vault.config.layout, vault.config
    base = git.run("merge-base", config.git.branch, branch)
    out = Review()
    binary = _binary_paths(git, base, branch)
    changes = _changes(git, base, branch, binary)
    allowed = (layout.reports,) if kind == "audit" else (layout.wiki, layout.archive)

    content: list[_Change] = []
    for c in changes:
        paths = [p for p in (c.old_path, c.path) if p]
        if any(_within(p, layout.raw) for p in paths):
            out.refused.append(f"it changes {c.path} in {layout.raw}/, which must never be edited")
        elif CONFIG_FILE in paths:
            out.wait(
                f"it changes {CONFIG_FILE}; mcpedia never changes the vault's settings by itself", c.path, "settings"
            )
        elif not all(any(_within(p, folder) for folder in allowed) for p in paths):
            out.wait(f"it changes {c.path}, outside {' and '.join(allowed)}", c.path, "outside the allowed folders")
        elif any(p in binary for p in paths):
            out.wait(f"it adds or changes the binary file {c.path}", c.path, "binary file")
        else:
            # A flagged page waits anyway; its lines are still checked, so the owner sees what kind of change it is.
            if any(fnmatch.fnmatch(p, pattern) for p in paths for pattern in config.review.flag):
                out.wait(f"it changes {c.path}, which is flagged for review", c.path, "flagged for review")
            content.append(c)
    if out.refused:
        return out

    _check_areas(vault, git, base, content, out)
    _check_lines(content, out, before, after, config.review)
    _check_secrets(vault, content, out)
    if before is not None and after is not None and (new := sorted(after - before)):
        shown = "; ".join(f"{path}: {message}" for path, message in new[:EXAMPLES])
        out.waits.append(f"it adds {len(new)} check issue(s): {shown}")
    return out


# -- showing it to the owner ---------------------------------------------------------


def review_section(vault: Vault, branch: str, blocking: dict[str, str], why_all: str | None = None) -> str:
    """The diff of each file that needs the owner's OK, short enough to read in a chat, and the other files by name.
    A branch that waits for a reason no single file causes (`why_all`, e.g. the agent didn't mark it ready) shows
    the diff of every file."""
    git = Git(vault.root, vault.config)
    main = vault.config.git.branch
    base = git.run("merge-base", main, branch)
    changed = git.run("-c", "core.quotePath=false", "diff", "--name-only", base, branch).splitlines()
    if not blocking and why_all:
        blocking = dict.fromkeys(changed, why_all)
    if not blocking:
        return ""
    parts, left = [REVIEW_HEADING], DIFF_LINES_TOTAL
    for path, why in blocking.items():
        lines = _file_diff(git, base, branch, path)
        shown = lines[: max(0, min(DIFF_LINES_PER_FILE, left))]
        left -= len(shown)
        block = f"{path} ({why}):"
        if shown:
            block += "\n```diff\n" + "\n".join(shown) + "\n```"
        if cut := len(lines) - len(shown):
            block += f"\n{cut} more line(s) not shown: `git diff {main}...{branch} -- {path}`"
        parts.append(block)
    if rest := [p for p in changed if p not in blocking]:
        parts.append("Other changed files: " + ", ".join(rest) + ".")
    return "\n\n".join(parts)


def _file_diff(git: Git, base: str, branch: str, path: str) -> list[str]:
    """One file's diff with a line of context, without git's header lines."""
    out = git.run("-c", "core.quotePath=false", "diff", "-U1", "--no-color", base, branch, "--", path)
    return [line for line in out.splitlines() if not line.startswith(_DIFF_HEADERS)]


_DIFF_HEADERS = ("diff --git ", "index ", "--- ", "+++ ", "new file mode", "deleted file mode", "similarity index")


# -- reading the branch -----------------------------------------------------------


def _changes(git: Git, base: str, branch: str, binary: set[str]) -> list[_Change]:
    """The changed files with their text before and after; binary files without (they aren't text)."""
    listing = git.run("-c", "core.quotePath=false", "diff", "--name-status", "-M", base, branch)
    out = []
    for line in listing.splitlines():
        status, *paths = line.split("\t")
        kind, old_path, path = status[0], (paths[0] if len(paths) > 1 else None), paths[-1]
        old_rev = old_path or path
        text = not ({old_rev, path} & binary)
        out.append(
            _Change(
                status=status,
                path=path,
                old_path=old_path,
                old=_show(git, base, old_rev) if text and kind != "A" else None,
                new=_show(git, branch, path) if text and kind != "D" else None,
            )
        )
    return out


def _show(git: Git, rev: str, path: str) -> str | None:
    try:
        return git.run("show", f"{rev}:{path}")
    except GitError:
        return None


def _binary_paths(git: Git, base: str, branch: str) -> set[str]:
    listing = git.run("-c", "core.quotePath=false", "diff", "--numstat", "--no-renames", base, branch)
    return {line.split("\t", 2)[2] for line in listing.splitlines() if line.startswith("-\t-\t")}


# -- the checks -----------------------------------------------------------------------


def _check_areas(vault: Vault, git: Git, base: str, changes: list[_Change], out: Review) -> None:
    """A page in a wiki folder that doesn't exist yet makes a new area; that needs the owner's approval."""
    wiki = vault.config.layout.wiki
    approved = approved_areas(vault)
    new_areas: dict[str, list[str]] = {}
    for c in changes:
        parts = PurePosixPath(c.path).parts
        if c.status[0] in "AR" and _within(c.path, wiki) and len(parts) > len(PurePosixPath(wiki).parts) + 1:
            area = parts[len(PurePosixPath(wiki).parts)]
            if not git.run("ls-tree", base, "--", f"{wiki}/{area}"):
                new_areas.setdefault(area, []).append(c.path)
    for area in sorted(new_areas):
        if area in approved:
            out.verified.append(f"new area `{area}`, approved by the owner")
        else:
            out.waits.append(f"it creates the area `{area}`, which the owner hasn't approved")
            for path in new_areas[area]:
                out.block(path, f"new area `{area}`")


def approved_areas(vault: Vault) -> set[str]:
    """Area names the owner approved: `approves_areas` in the frontmatter of a capture."""
    names: set[str] = set()
    for path in vault.markdown_files(vault.captures_dir):
        data = parse_frontmatter(path.read_text(encoding="utf-8", errors="ignore")).data
        value = data.get("approves_areas")
        if isinstance(value, list):
            names |= {str(v) for v in value}
    return names


def _check_lines(
    changes: list[_Change],
    out: Review,
    before: CheckIssues | None,
    after: CheckIssues | None,
    settings: ReviewSettings | None = None,
) -> None:
    tag = _tag_pattern(settings.volatile_tags) if settings and settings.auto_merge == "volatile" else None
    volatile: list[tuple[str, str, str | None]] = []  # (path, old line, the line that replaced it, if one did)
    # Everything the branch's changed files say afterwards: where removed lines must reappear.
    corpus = {_norm(line) for c in changes if c.new for line in _body(c.new).splitlines()}
    # Pages that left the wiki (deleted, or moved to the archive): their index entries may go.
    moved_out = {PurePosixPath(c.old_path or c.path).stem.lower() for c in changes if _leaves_folder(c)}
    moved = links = extended = 0
    link_paths: list[str] = []
    for c in changes:
        if c.status.startswith("R100"):
            out.verified.append(f"moved {c.old_path} to {c.path} unchanged")
            continue
        for problem in _frontmatter_problems(c.old, c.new):
            out.wait(f"{c.path}: {problem}", c.path, "frontmatter changed")
        old_body = _body(c.old or "")
        removed, added = _diff_pairs(old_body, _body(c.new or ""))
        added_variants = {v for line in added for v in _variants(line)}
        is_index = PurePosixPath(c.path).name == "_index.md"
        provisional = _volatile_lines(old_body, tag) if tag is not None and c.old is not None else set()
        unverified, link_lines = [], 0
        for index, line, replaced_by in removed:
            if _trivial(line):
                continue
            if _norm(line) in corpus:
                moved += 1
            elif _variants(line) & added_variants:
                link_lines += 1
            elif is_index and _links_only_to(line, moved_out):
                moved += 1  # the index entry of a page moved out of the wiki
            elif is_index and any(_extends(new, line) for new in added):
                extended += 1
            elif index in provisional:
                volatile.append((c.path, line.strip(), replaced_by))
            else:
                unverified.append(line.strip())
        if unverified:
            examples = "; ".join(repr(line[:80]) for line in unverified[:EXAMPLES])
            what = "removed" if c.new is None else "removed or rewritten"
            out.wait(f"{c.path}: {len(unverified)} line(s) {what}, e.g. {examples}", c.path, f"lines {what}")
        if link_lines:
            links += link_lines
            link_paths.append(c.path)
    if volatile and settings is not None:
        _report_volatile(volatile, settings.volatile_max_lines, out)
    if moved:
        out.verified.append(f"{moved} line(s) moved unchanged")
    if extended:
        out.verified.append(f"{extended} index entr{'y' if extended == 1 else 'ies'} extended")
    if links:
        if before is None or after is None:
            out.waits.append(f"{links} line(s) with changed links (can't verify that they fix check issues)")
        elif len(after) >= len(before):
            out.waits.append(f"{links} line(s) with changed links, but they don't fix any check issue")
        if before is None or after is None or len(after) >= len(before):
            for path in link_paths:
                out.block(path, "links changed")
        else:
            out.verified.append(f"{links} link fix(es), check issues down from {len(before)} to {len(after)}")


def _report_volatile(volatile: list[tuple[str, str, str | None]], limit: int, out: Review) -> None:
    """Volatile lines merge by themselves, up to `limit` per branch, and the owner sees each one: old -> new."""
    paths = list(dict.fromkeys(path for path, _, _ in volatile))
    if len(volatile) > limit:
        out.waits.append(
            f"{len(volatile)} volatile line(s) rewritten or removed, more than review.volatile_max_lines ({limit})"
        )
        for path in paths:
            out.block(path, "many volatile lines changed")
        return
    for path in paths:
        lines = [(old, new) for p, old, new in volatile if p == path]
        shown = "; ".join(
            f"{old[:80]!r} -> {new.strip()[:80]!r}" if new and new.strip() else f"{old[:80]!r} (removed)"
            for old, new in lines
        )
        out.verified.append(f"{path}: {len(lines)} volatile line(s) updated: {shown}")


def _check_secrets(vault: Vault, changes: list[_Change], out: Review) -> None:
    """The server's own scan of what the branch adds, whatever the agent was told."""
    secrets = vault.config.secrets
    scanner = Scanner.for_config(secrets)
    for c in changes:
        if c.new is None:
            continue
        _, added = _diff(c.old or "", c.new)
        findings = scanner.scan("\n".join(added))
        findings = [f for f in findings if f.pii or secrets.policy != "allow"]
        if findings:
            kinds = ", ".join(sorted({f.description for f in findings}))
            out.wait(
                f"{c.path}: it adds what looks like secrets or personal data ({kinds})",
                c.path,
                "secrets or personal data",
            )


# -- lines -------------------------------------------------------------------------------


def _body(text: str) -> str:
    fm = parse_frontmatter(text)
    return fm.body if fm.present else text


def _frontmatter_problems(old: str | None, new: str | None) -> list[str]:
    """Frontmatter may grow: new keys, longer lists, a new `updated`. Anything else is a change to review."""
    if old is None or new is None:
        return []
    before, after = parse_frontmatter(old).data, parse_frontmatter(new).data
    problems = []
    for key, value in before.items():
        if key not in after:
            problems.append(f"removes `{key}` from the frontmatter")
        elif after[key] != value and key != "updated" and not _grows(value, after[key]):
            problems.append(f"changes `{key}` in the frontmatter")
    return problems


def _grows(old: Any, new: Any) -> bool:
    return isinstance(old, list) and isinstance(new, list) and all(item in new for item in old)


def _diff(old: str, new: str) -> tuple[list[str], list[str]]:
    """Lines removed from `old`, and lines added in `new`."""
    a, b = old.splitlines(), new.splitlines()
    removed, added = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("delete", "replace"):
            removed += a[i1:i2]
        if tag in ("insert", "replace"):
            added += b[j1:j2]
    return removed, added


def _diff_pairs(old: str, new: str) -> tuple[list[tuple[int, str, str | None]], list[str]]:
    """Like `_diff`, but each removed line comes with its index in `old` and, where a block of lines was replaced by
    one of the same length, the line that took its place."""
    a, b = old.splitlines(), new.splitlines()
    removed: list[tuple[int, str, str | None]] = []
    added: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("delete", "replace"):
            same_size = tag == "replace" and i2 - i1 == j2 - j1
            removed += [(i, a[i], b[j1 + i - i1] if same_size else None) for i in range(i1, i2)]
        if tag in ("insert", "replace"):
            added += b[j1:j2]
    return removed, added


def _tag_pattern(tags: tuple[str, ...]) -> re.Pattern[str] | None:
    """An Obsidian tag from `tags` (`#plan`, or nested `#plan/2027`), not part of a word, link or heading anchor."""
    if not tags:
        return None
    names = "|".join(re.escape(t) for t in tags)
    return re.compile(rf"(?<![\w#/\[&])#(?:{names})(?:/[\w/-]*)?(?![\w-])")


def _volatile_lines(body: str, tag: re.Pattern[str] | None) -> set[int]:
    """Indexes of the provisional lines in `body`: every line of a section whose heading carries a volatile tag
    (up to the next heading of the same or a higher level, subsections included), and lines carrying one themselves.
    Headings and tags inside code blocks don't count."""
    if tag is None:
        return set()
    found: set[int] = set()
    sections: list[tuple[int, bool]] = []  # open headings: (level, volatile)
    in_code = False
    for i, line in enumerate(body.splitlines()):
        inside = bool(sections) and sections[-1][1]
        if FENCE.match(line):
            in_code = not in_code
            if inside:
                found.add(i)
            continue
        heading = None if in_code else HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            while sections and sections[-1][0] >= level:
                sections.pop()
            covered = any(v for _, v in sections) or bool(tag.search(line))
            sections.append((level, covered))
            if covered:
                found.add(i)
        elif inside or (not in_code and tag.search(line)):
            found.add(i)
    return found


def _norm(line: str) -> str:
    """A line as it reads: heading level and spacing don't matter (a page merged under another becomes a section)."""
    return " ".join(re.sub(r"^#+\s*", "", line.strip()).split())


def _trivial(line: str) -> bool:
    return _norm(line) in ("", "---")


def _variants(line: str) -> set[str]:
    """A line without its links, and with each link replaced by its text: equal variants mean only links changed."""
    return {_norm(LINK.sub("", line)), _norm(LINK.sub(lambda m: m.group(2) or m.group(1), line))}


def _extends(new: str, old: str) -> bool:
    """`new` is `old` with text appended at the end (its final punctuation may change), and the same links."""
    return _norm(new).startswith(_norm(old).rstrip(".;,")) and [m.group(0) for m in LINK.finditer(new)] == [
        m.group(0) for m in LINK.finditer(old)
    ]


def _links_only_to(line: str, names: set[str]) -> bool:
    targets = [PurePosixPath(m.group(1).strip()).stem.lower() for m in LINK.finditer(line)]
    return bool(targets) and all(t in names for t in targets)


def _leaves_folder(c: _Change) -> bool:
    if c.status[0] == "D":
        return True
    return c.old_path is not None and PurePosixPath(c.old_path).parts[0] != PurePosixPath(c.path).parts[0]


def _within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")
