"""Per-vault configuration, read from `wikismith.yaml` at the vault root.

Every key has a default, so a minimal file is just `name: personal`. Unknown keys are errors: a typo in a security
setting must not silently fall back to a default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from wikismith.markdown import yaml_error

CONFIG_FILE = "wikismith.yaml"

Policy = Literal["allow", "warn", "redact", "block"]  # for secrets and for personal data
AutoMerge = Literal["none", "additive", "volatile"]
ImageMode = Literal["shrink", "ask"]
JobKind = Literal["forge", "audit"]


class ConfigError(ValueError):
    """The vault config is missing, malformed or invalid."""


@dataclass(frozen=True)
class Layout:
    """Where things live, relative to the vault root."""

    raw: str = "raw"
    wiki: str = "wiki"
    archive: str = "archive"
    captures: str = "raw/captures"
    reports: str = "reports"
    log: str = "wiki/_log.md"
    instructions: str = "CLAUDE.md"


@dataclass(frozen=True)
class Files:
    """Limits for files committed to the vault, checked by the pre-commit hook (`wikismith hook`)."""

    image_max_px: int = 1600  # long edge; agents read images at about this size
    image_max_kb: int = 500
    images: ImageMode = "shrink"  # images over the limits: shrink them, or ask (in a terminal)
    max_kb: int = 1024  # other files (PDFs, videos, ...) over this: always ask


@dataclass(frozen=True)
class Access:
    """What agents may read through mcpedia. Writing is always captures-only."""

    read: tuple[str, ...] = ("wiki", "archive")  # `search` skips the archive unless asked
    search_captures: bool = True


@dataclass(frozen=True)
class SecretPattern:
    id: str
    regex: str
    description: str = ""
    pii: bool = False  # personal data (follows secrets.pii), not a secret


@dataclass(frozen=True)
class Secrets:
    policy: Policy = "redact"
    pii: Policy = "redact"  # the same for personal data: patterns marked `pii: true`
    mask_reads: bool = True
    allow_cloud_remote: bool = False
    patterns: tuple[SecretPattern, ...] = ()


@dataclass(frozen=True)
class Git:
    commit_captures: bool = True  # false: mcpedia saves captures, the owner commits them
    sync: bool = True  # pull before and push after each commit and merge, if there's a remote
    remote: str = "origin"
    branch: str = "main"


@dataclass(frozen=True)
class Search:
    semantic: bool = True
    model: str = "BAAI/bge-small-en-v1.5"
    cache_dir: str | None = None


@dataclass(frozen=True)
class Review:
    auto_merge: AutoMerge = "volatile"
    flag: tuple[str, ...] = ()
    # Tags that mark provisional text (open questions, plans, status): a heading with one covers its section, a line
    # with one covers that line. With auto_merge `volatile`, such lines may be rewritten or removed without review.
    volatile_tags: tuple[str, ...] = ("open", "plan", "status")
    volatile_max_lines: int = 10  # more volatile lines rewritten or removed in one branch: it waits


# Headless Claude Code, allowed to edit files and run only git and wikismith commands. mcpedia passes the job's
# instructions on standard input.
_AGENT_FLAGS = (
    "--permission-mode",
    "acceptEdits",
    "--allowedTools",
    "Bash(git *),Bash(wikismith *),Read,Write,Edit,Glob,Grep",
)


@dataclass(frozen=True)
class Jobs:
    agent: tuple[str, ...] = ("claude", "-p", *_AGENT_FLAGS)
    timeout_minutes: int = 30
    # Run when something waits for the owner, e.g. ["curl", "-s", "-d", "{message}", "https://ntfy.sh/<topic>"].
    # {message} holds counts only ("forge: 2 questions, 1 branch waiting"), never vault content.
    notify: tuple[str, ...] = ()


@dataclass(frozen=True)
class VaultConfig:
    name: str
    description: str = ""
    timezone: str | None = None  # for dates in the vault (captures, jobs); unset: the machine's (TZ, else system)
    layout: Layout = field(default_factory=Layout)
    files: Files = field(default_factory=Files)
    access: Access = field(default_factory=Access)
    secrets: Secrets = field(default_factory=Secrets)
    git: Git = field(default_factory=Git)
    search: Search = field(default_factory=Search)
    review: Review = field(default_factory=Review)
    jobs: Jobs = field(default_factory=Jobs)


def load_config(vault_root: Path) -> VaultConfig:
    path = vault_root / CONFIG_FILE
    if not path.is_file():
        raise ConfigError(f"No {CONFIG_FILE} in {vault_root}. Run `wikismith init` to create one.")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: {yaml_error(e, first_line=1)}") from e
    return parse_config(data if data is not None else {})


def parse_config(data: Any) -> VaultConfig:
    if not isinstance(data, dict):
        raise ConfigError("config must be a mapping")
    data = dict(data)
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        raise ConfigError("`name` is required")
    sections: dict[str, Any] = {
        "layout": _section(Layout, data.pop("layout", None), "layout"),
        "files": _section(Files, data.pop("files", None), "files"),
        "access": _section(Access, data.pop("access", None), "access"),
        "secrets": _secrets(data.pop("secrets", None)),
        "git": _section(Git, data.pop("git", None), "git"),
        "search": _section(Search, data.pop("search", None), "search"),
        "review": _section(Review, data.pop("review", None), "review"),
        "jobs": _section(Jobs, data.pop("jobs", None), "jobs"),
    }
    config = _section(VaultConfig, {**data, **sections}, "config", nested=set(sections))
    _validate(config)
    return config


def _secrets(raw: Any) -> Secrets:
    raw = dict(raw or {})
    patterns = []
    for i, p in enumerate(raw.pop("patterns", None) or []):
        pattern = _section(SecretPattern, p, f"secrets.patterns[{i}]")
        try:
            re.compile(pattern.regex)
        except re.error as e:
            raise ConfigError(f"secrets.patterns[{i}] ({pattern.id}): invalid regex: {e}") from e
        patterns.append(pattern)
    return replace(_section(Secrets, raw, "secrets"), patterns=tuple(patterns))


def _section[T](cls: type[T], raw: Any, where: str, nested: set[str] | None = None) -> T:
    """Build a dataclass from a mapping, rejecting unknown keys and wrong scalar types."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"`{where}` must be a mapping")
    known = {f.name: f for f in fields(cls)}  # type: ignore[arg-type]
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ConfigError(f"`{where}`: unknown key(s): {', '.join(unknown)}")
    values: dict[str, Any] = {}
    for key, value in raw.items():
        if nested and key in nested:
            values[key] = value
            continue
        values[key] = _coerce(value, known[key].type, f"{where}.{key}")
    return cls(**values)


def _coerce(value: Any, annotation: Any, where: str) -> Any:
    text = str(annotation)
    if text.startswith("tuple"):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"`{where}` must be a list of strings")
        return tuple(value)
    literals = {"Policy": Policy, "AutoMerge": AutoMerge, "ImageMode": ImageMode}
    if text in literals:
        allowed = get_args(literals[text])
        if value not in allowed:
            raise ConfigError(f"`{where}` must be one of: {', '.join(allowed)}")
        return value
    expected = {"bool": bool, "int": int, "str": str, "str | None": (str, type(None))}.get(text)
    if expected is not None and (not isinstance(value, expected) or (expected is int and isinstance(value, bool))):
        raise ConfigError(f"`{where}` has the wrong type ({type(value).__name__})")
    return value


def _validate(config: VaultConfig) -> None:
    for key in ("image_max_px", "image_max_kb", "max_kb"):
        if getattr(config.files, key) < 1:
            raise ConfigError(f"files.{key} must be at least 1")
    if config.timezone is not None:
        try:
            ZoneInfo(config.timezone)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ConfigError(f"timezone: unknown time zone {config.timezone!r} (use a name like Europe/Berlin)") from e
    layout = config.layout
    for name in ("raw", "wiki", "archive", "captures", "reports", "log", "instructions"):
        _check_relative(getattr(layout, name), f"layout.{name}")
    if not _is_within(layout.captures, layout.raw):
        raise ConfigError("layout.captures must be inside layout.raw")
    allowed_read = {layout.wiki, layout.archive}
    for folder in config.access.read:
        if folder not in allowed_read:
            raise ConfigError(
                f"access.read: {folder!r} is not allowed; agents may read only the wiki and archive "
                "folders (unforged captures are covered by access.search_captures)"
            )
    if config.review.volatile_max_lines < 0:
        raise ConfigError("review.volatile_max_lines must not be negative")
    for tag in config.review.volatile_tags:
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_/-]*", tag):
            raise ConfigError(f"review.volatile_tags: {tag!r} is not a tag (write it without '#', e.g. open)")
    if config.jobs.timeout_minutes <= 0:
        raise ConfigError("jobs.timeout_minutes must be positive")
    if not config.jobs.agent:
        raise ConfigError("jobs.agent must not be empty")


def _check_relative(value: str, where: str) -> None:
    p = Path(value)
    if p.is_absolute() or ".." in p.parts or not value.strip():
        raise ConfigError(f"{where} must be a relative path inside the vault")


def _is_within(child: str, parent: str) -> bool:
    return Path(child).parts[: len(Path(parent).parts)] == Path(parent).parts
