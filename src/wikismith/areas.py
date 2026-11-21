"""`wikismith area add`: the owner approves new areas directly, without waiting for a forge to propose them.

An area is approved by a capture with `approves_areas` in its frontmatter. `answer_questions` writes one for areas a
forge proposed; this command writes one for areas the owner already knows, e.g. a planned structure before a large
import. Agents can't set capture frontmatter, so this is a command for the owner, run by hand.
"""

from __future__ import annotations

import re

from wikismith.captures import MAX_TITLE, CaptureResult, Captures
from wikismith.merges import approved_areas
from wikismith.secretscan import Scanner
from wikismith.vault import Vault

AREA_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class InvalidArea(ValueError):
    pass


def approve_areas(vault: Vault, areas: dict[str, str]) -> CaptureResult:
    """Save and commit a capture approving these areas (name: one-line description). The next forge creates them."""
    if not areas:
        raise InvalidArea("name at least one area and its description")
    approved = approved_areas(vault)
    for name, description in areas.items():
        if name in approved and not (vault.wiki_dir / name).exists():
            raise InvalidArea(f"the area {name!r} is approved already; the next forge creates it")
        if not AREA_NAME.fullmatch(name):
            raise InvalidArea(f"{name!r}: area names are lowercase letters, digits and hyphens, e.g. home-network")
        if (vault.wiki_dir / name).exists():
            raise InvalidArea(f"the area {name!r} exists already")
        if not " ".join(description.split()):
            raise InvalidArea(f"{name!r} needs a one-line description of what it holds")
    lines = [
        "The owner approves these new areas. Create each one now: its folder and an `_index.md` with this "
        "`description:`. Material for them arrives with later captures and imports.",
        "",
    ]
    lines += [f"- `{name}`: {' '.join(description.split())}" for name, description in areas.items()]
    title = f"New area{'s' if len(areas) > 1 else ''}: {', '.join(areas)}"
    if len(title) > MAX_TITLE:
        title = f"New areas: {len(areas)} approved by the owner"
    captures = Captures(vault, Scanner.for_config(vault.config.secrets))
    approval = {"approves_areas": list(areas)}
    return captures.add(title, "\n".join(lines), source="wikismith area add", frontmatter=approval)
