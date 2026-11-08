"""What mcpedia tells agents: short server instructions for every client, `vault_guide`, and job prompts."""

from __future__ import annotations

import re
from collections.abc import Callable
from importlib.resources import files

from wikismith.config import JobKind
from wikismith.gitops import Git
from wikismith.pages import Catalog
from wikismith.vault import Vault


def asset(name: str) -> str:
    return files("wikismith").joinpath("assets", name).read_text(encoding="utf-8")


def general_rules() -> str:
    return asset("rules.md")


def job_prompt(vault: Vault, kind: JobKind) -> str:
    """Everything a forge or audit agent needs: its instructions, this vault's settings, the general rules and the
    vault's own rules."""
    review = vault.config.review
    files = vault.config.files
    settings = _policies(vault) + f"- Auto-merge: `{review.auto_merge}`.\n"
    settings += (
        f"- File limits: images {files.image_max_px} px and {files.image_max_kb} KB, other files {files.max_kb} KB.\n"
    )
    if review.flag:
        settings += f"- Pages flagged for review: {', '.join(f'`{p}`' for p in review.flag)}.\n"
    parts = [
        asset(f"jobs/{kind}.md").strip(),
        "## This vault's settings\n\n" + settings.strip(),
        "## General vault rules\n\n" + _demote_headings(general_rules().strip()),
    ]
    if own := own_rules(vault):
        parts.append(f"## This vault's own rules ({vault.config.layout.instructions})\n\n{_demote_headings(own)}")
    return "\n\n".join(parts) + "\n"


def _policies(vault: Vault) -> str:
    secrets = vault.config.secrets
    return f"- Secret policy: `{secrets.policy}`.\n- PII policy: `{secrets.pii}`.\n"


def own_rules(vault: Vault) -> str:
    """The vault's `CLAUDE.md`, without `@path` import lines: mcpedia passes the general rules itself."""
    if not vault.instructions_file.is_file():
        return ""
    return _IMPORT.sub("", vault.instructions_file.read_text(encoding="utf-8")).strip()


# A Claude Code `@path` import on a line of its own.
_IMPORT = re.compile(r"^@\S+[ \t]*\n?", re.MULTILINE)

# Claude Code cuts server instructions off at 4,096 characters; stay below with a margin, so nothing is lost.
# Ten areas with one-line descriptions fit; more fall back to the area names (`_areas`).
MAX_INSTRUCTIONS = 3900


def build_instructions(vault: Vault, mask: Callable[[str], str] = lambda t: t) -> str:
    """The server instructions every client gets: the fixed general part, then name, policy and areas of this vault.

    Kept short (clients truncate them); the owner's full rules are in `vault_guide`.
    """
    config = vault.config
    description = f"{mask(config.description.strip())}\n\n" if config.description.strip() else ""
    text = asset("instructions.md").format(name=config.name, description=description)
    text += "\n## This vault\n\n" + _policies(vault)
    if not Git(vault.root, config).is_repo():
        text += (
            "- **Read-only:** the vault has no git repository, so you can search and read but not capture, forge or "
            "audit. Don't offer to save anything.\n"
        )
    end = "\nThe owner's full rules for this vault: call `vault_guide`.\n"
    return text + _areas(vault, mask, MAX_INSTRUCTIONS - len(text) - len(end)) + end


def _areas(vault: Vault, mask: Callable[[str], str], budget: int) -> str:
    """The areas with their descriptions; just their names if that's too long, and fewer names if even that is."""
    catalog = Catalog(vault)
    areas = catalog.areas()
    if not areas:
        return "- The wiki has no areas yet.\n"
    head = "- Areas (folders in `wiki/`; a forge never creates one without the owner's OK):\n"
    full = head + "".join(f"  - `{a}`: {mask(catalog.description(a)) or '(no description)'}\n" for a in areas)
    if len(full) <= budget:
        return full
    names = [f"`{a}`" for a in areas]
    while names:
        more = len(areas) - len(names)
        line = (
            "- Areas: "
            + ", ".join(names)
            + (f", and {more} more" if more else "")
            + " (`list_areas` describes them).\n"
        )
        if len(line) <= budget:
            return line
        names.pop()
    return "- Areas: see `list_areas`.\n"


def vault_guide(vault: Vault, mask: Callable[[str], str] = lambda t: t, include_conventions: bool = False) -> str:
    """The server instructions plus the owner's full rules, and optionally the general conventions jobs follow."""
    text = build_instructions(vault, mask)
    if own := own_rules(vault):
        text += (
            f"\n## This vault's own rules ({vault.config.layout.instructions})\n\n"
            "The owner's rules for this vault; they win over the general instructions. Parts about editing the wiki "
            "are for forge jobs; everything about facts, areas, people and secrets applies to you too.\n\n"
            f"{_demote_headings(mask(own))}\n"
        )
    if include_conventions:
        text += "\n## General vault rules\n\n" + _demote_headings(general_rules().strip()) + "\n"
    return text


def _demote_headings(text: str) -> str:
    """Nest the vault's headings under ours (`#` -> `###`), outside code blocks."""
    out, fenced = [], False
    for line in text.splitlines():
        if line.startswith(("```", "~~~")):
            fenced = not fenced
        if not fenced and line.startswith("#"):
            line = "##" + line
        out.append(line)
    return "\n".join(out)
