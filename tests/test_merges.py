"""Merge verification on hand-made branches of the test vault: one case per rule."""

from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import git, make_vault
from wikismith.merges import DIFF_LINES_PER_FILE, REVIEW_HEADING, Review, review, review_section

Edit = Callable[[Path], None]


def branch(repo: Path, edit: Edit, name: str = "forge/test") -> str:
    git(repo, "switch", "-q", "-c", name)
    edit(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "job")
    git(repo, "switch", "-q", "main")
    return name


def verdict(repo: Path, edit: Edit, kind: str = "forge", **config: object) -> Review:
    vault = make_vault(repo, search={"semantic": False}, **config)
    return review(vault, branch(repo, edit, f"{kind}/test"), kind)  # type: ignore[arg-type]


def append(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((path.read_text() if path.exists() else "") + text)


def replace(repo: Path, rel: str, old: str, new: str) -> None:
    path = repo / rel
    assert old in path.read_text()
    path.write_text(path.read_text().replace(old, new))


def test_additive_changes_merge(repo: Path) -> None:
    def edit(r: Path) -> None:
        append(r, "wiki/garden/tomatoes.md", "\nStart pepper seeds eight weeks before frost.\n")
        replace(r, "wiki/garden/tomatoes.md", "updated: 2026-01-02", "updated: 2026-02-01")
        replace(
            r,
            "wiki/garden/tomatoes.md",
            "sources: [raw/notes/2026-01-01_tomato-thread.md]",
            "sources: [raw/notes/2026-01-01_tomato-thread.md, raw/captures/2026-01-06_pepper-seeds.md]",
        )
        append(r, "wiki/garden/peppers.md", "---\ntitle: Peppers\narea: garden\n---\n\n# Peppers\n\nChili.\n")
        append(r, "wiki/garden/_index.md", "- [[peppers]] — chili.\n")

    result = verdict(repo, edit)
    assert result.mergeable, result.waits


def test_rewritten_line_waits(repo: Path) -> None:
    result = verdict(repo, lambda r: replace(r, "wiki/garden/tomatoes.md", "twice a week", "three times a week"))
    [reason] = result.waits
    assert reason.startswith("wiki/garden/tomatoes.md: 1 line(s) removed or rewritten, e.g. 'Deep watering")


def test_deleted_page_waits(repo: Path) -> None:
    result = verdict(repo, lambda r: (r / "wiki/kitchen/knife-care.md").unlink())
    assert result.waits == [
        "wiki/kitchen/knife-care.md: 2 line(s) removed, e.g. '# Knife care'; 'Hone weekly, sharpen twice a year.'"
    ]


def test_merged_pages_are_verified_moves(repo: Path) -> None:
    def edit(r: Path) -> None:
        (r / "wiki/kitchen/knife-care.md").unlink()
        append(r, "wiki/kitchen/sourdough.md", "\n## Knife care\n\nHone weekly, sharpen twice a year.\n")

    result = verdict(repo, edit)
    assert result.mergeable, result.waits
    assert "2 line(s) moved unchanged" in result.verified


def test_a_merge_with_rewording_waits(repo: Path) -> None:
    def edit(r: Path) -> None:
        (r / "wiki/kitchen/knife-care.md").unlink()
        append(r, "wiki/kitchen/sourdough.md", "\n## Knife care\n\nHone the knives every week.\n")

    [reason] = verdict(repo, edit).waits
    assert "1 line(s) removed" in reason and "Hone weekly" in reason


def test_unchanged_archive_move_merges(repo: Path) -> None:
    def edit(r: Path) -> None:
        (r / "archive/kitchen").mkdir(parents=True)
        git(r, "mv", "wiki/kitchen/sourdough.md", "archive/kitchen/sourdough.md")
        replace(r, "wiki/kitchen/_index.md", "- [[sourdough|Sourdough bread]] — starter and schedule.\n", "")

    result = verdict(repo, edit)
    assert result.mergeable, result.waits
    assert "moved wiki/kitchen/sourdough.md to archive/kitchen/sourdough.md unchanged" in result.verified


def test_archive_move_with_edits_waits(repo: Path) -> None:
    def edit(r: Path) -> None:
        (r / "archive/kitchen").mkdir(parents=True)
        git(r, "mv", "wiki/kitchen/sourdough.md", "archive/kitchen/sourdough.md")
        replace(r, "archive/kitchen/sourdough.md", "Feed the starter 1:1:1", "Feed the starter 1:2:2")

    assert any("archive/kitchen/sourdough.md: 1 line(s) removed or rewritten" in w for w in verdict(repo, edit).waits)


def test_changed_frontmatter_values_wait(repo: Path) -> None:
    result = verdict(repo, lambda r: replace(r, "wiki/garden/tomatoes.md", "title: Tomatoes", "title: Tomato plants"))
    assert result.waits == ["wiki/garden/tomatoes.md: changes `title` in the frontmatter"]


def test_link_fixes_need_fewer_check_issues(repo: Path) -> None:
    vault = make_vault(repo)
    name = branch(repo, lambda r: replace(r, "wiki/garden/tomatoes.md", "[[compost-bin]]", "[[my-garden|compost-bin]]"))
    broken = ("wiki/garden/tomatoes.md", "[[compost-bin]]: no such page")
    other = ("wiki/kitchen/knife-care.md", "not listed in wiki/kitchen/_index.md")
    fixed = review(vault, name, "forge", frozenset({broken, other}), frozenset({other}))
    assert fixed.mergeable and "1 link fix(es), check issues down from 2 to 1" in fixed.verified
    unfixed = review(vault, name, "forge", frozenset({other}), frozenset({other}))
    assert unfixed.waits == ["1 line(s) with changed links, but they don't fix any check issue"]
    assert "can't verify" in review(vault, name, "forge").waits[0]  # reviewed later, without the job's check runs


def test_new_check_issues_wait(repo: Path) -> None:
    vault = make_vault(repo)
    name = branch(repo, lambda r: append(r, "wiki/garden/tomatoes.md", "\nSee [[nowhere]].\n"))
    new = ("wiki/garden/tomatoes.md", "[[nowhere]]: no such page")
    result = review(vault, name, "forge", frozenset(), frozenset({new}))
    assert result.waits == ["it adds 1 check issue(s): wiki/garden/tomatoes.md: [[nowhere]]: no such page"]


def test_new_areas_need_the_owners_approval(repo: Path) -> None:
    def edit(r: Path) -> None:
        append(r, "wiki/pets/_index.md", "---\ntitle: Pets\narea: pets\ndescription: The cat.\n---\n\n# Pets\n")

    assert verdict(repo, edit).waits == ["it creates the area `pets`, which the owner hasn't approved"]
    git(repo, "branch", "-D", "forge/test")
    append(repo, "raw/captures/2026-02-02_answers.md", "---\napproves_areas: [pets]\n---\n\n# Answers\n\nYes, pets.\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "Add capture: Answers")
    result = verdict(repo, edit)
    assert result.mergeable and "new area `pets`, approved by the owner" in result.verified


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        (lambda r: (r / "wiki/garden/photo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00"), "binary file"),
        (lambda r: append(r, "wikismith.yaml", "description: x\n"), "mcpedia never changes the vault's settings"),
        (lambda r: append(r, "CLAUDE.md", "New rule.\n"), "it changes CLAUDE.md, outside wiki and archive"),
        (lambda r: append(r, "wiki/garden/tomatoes.md", "password: hunter2-not-real\n"), "looks like secrets"),
    ],
)
def test_waits(repo: Path, edit: Edit, reason: str) -> None:
    result = verdict(repo, edit)
    assert not result.mergeable and any(reason in w for w in result.waits), result.waits


def test_secrets_under_allow_merge(repo: Path) -> None:
    result = verdict(
        repo,
        lambda r: append(r, "wiki/garden/tomatoes.md", "password: hunter2-not-real\n"),
        secrets={"policy": "allow"},
    )
    assert result.mergeable, result.waits


def test_flagged_pages_wait(repo: Path) -> None:
    result = verdict(
        repo, lambda r: append(r, "wiki/garden/my-garden.md", "A fourth bed.\n"), review={"flag": ["wiki/*/my-*.md"]}
    )
    assert result.waits == ["it changes wiki/garden/my-garden.md, which is flagged for review"]


def test_raw_changes_are_refused(repo: Path) -> None:
    result = verdict(repo, lambda r: append(r, "raw/notes/2026-01-01_tomato-thread.md", "edited\n"))
    assert result.refused and not result.mergeable


def test_audits_may_only_add_reports(repo: Path) -> None:
    assert verdict(repo, lambda r: append(r, "reports/audit-2026-02-01.md", "# Audit\n"), kind="audit").mergeable
    git(repo, "branch", "-D", "audit/test")
    result = verdict(repo, lambda r: append(r, "wiki/garden/tomatoes.md", "x\n"), kind="audit")
    assert result.waits == ["it changes wiki/garden/tomatoes.md, outside reports"]


def test_index_entries_may_grow_at_the_end(repo: Path) -> None:
    entry = "- [[tomatoes]] — varieties and watering."
    grown = verdict(repo, lambda r: replace(r, "wiki/garden/_index.md", entry, entry[:-1] + ", pepper seeds."))
    assert grown.mergeable and "1 index entry extended" in grown.verified
    git(repo, "branch", "-D", "forge/test")
    reworded = verdict(
        repo, lambda r: replace(r, "wiki/garden/_index.md", entry, "- [[tomatoes]] — all about watering.")
    )
    assert not reworded.mergeable
    git(repo, "branch", "-D", "forge/test")
    relinked = verdict(repo, lambda r: replace(r, "wiki/garden/_index.md", entry, entry + " See [[sourdough]]."))
    assert not relinked.mergeable  # a new link is more than an extension


def test_fact_lines_may_not_grow(repo: Path) -> None:
    line = "Three raised beds, 4 x 8 ft each."
    result = verdict(repo, lambda r: replace(r, "wiki/garden/my-garden.md", line, line + " One was removed in March."))
    assert not result.mergeable


# -- what the owner is shown ------------------------------------------------------


def section(repo: Path, edit: Edit, **config: object) -> tuple[Review, str]:
    vault = make_vault(repo, search={"semantic": False}, **config)
    name = branch(repo, edit)
    result = review(vault, name, "forge")
    return result, review_section(vault, name, result.blocking)


def test_the_owner_sees_the_diff_of_a_flagged_page(repo: Path) -> None:
    def edit(r: Path) -> None:
        append(r, "wiki/garden/my-garden.md", "A fourth bed.\n")
        append(r, "wiki/garden/peppers.md", "---\ntitle: Peppers\narea: garden\n---\n\n# Peppers\n\nChili.\n")

    result, text = section(repo, edit, review={"flag": ["wiki/*/my-*.md"]})
    assert result.blocking == {"wiki/garden/my-garden.md": "flagged for review"}
    assert text.startswith(REVIEW_HEADING)
    assert "wiki/garden/my-garden.md (flagged for review):\n```diff\n@@" in text
    assert "+A fourth bed." in text and "diff --git" not in text and "+++ " not in text
    assert text.endswith("Other changed files: wiki/garden/peppers.md.")


def test_the_owner_sees_rewritten_lines(repo: Path) -> None:
    result, text = section(repo, lambda r: replace(r, "wiki/garden/tomatoes.md", "twice a week", "three times a week"))
    assert result.blocking == {"wiki/garden/tomatoes.md": "lines removed or rewritten"}
    assert "\n-" in text and "twice a week" in text and "three times a week" in text


def test_long_diffs_are_cut(repo: Path) -> None:
    lines = "".join(f"Bed {i}.\n" for i in range(100))
    _, text = section(repo, lambda r: append(r, "wiki/garden/my-garden.md", lines), review={"flag": ["wiki/*/my-*.md"]})
    shown = text.split("```diff\n")[1].split("\n```")[0].splitlines()
    assert len(shown) == DIFF_LINES_PER_FILE
    assert "more line(s) not shown: `git diff main...forge/test -- wiki/garden/my-garden.md`" in text


def test_nothing_to_show_for_a_mergeable_branch(repo: Path) -> None:
    result, text = section(repo, lambda r: append(r, "wiki/garden/tomatoes.md", "\nMulch in June.\n"))
    assert result.mergeable and text == ""


def test_a_flagged_page_is_labelled_with_what_changed(repo: Path) -> None:
    result = verdict(
        repo,
        lambda r: replace(r, "wiki/garden/my-garden.md", "Three raised beds", "Four raised beds"),
        review={"flag": ["wiki/*/my-*.md"]},
    )
    assert result.blocking == {"wiki/garden/my-garden.md": "flagged for review; lines removed or rewritten"}


def test_every_file_is_shown_when_no_single_one_blocks(repo: Path) -> None:
    vault = make_vault(repo, search={"semantic": False})
    name = branch(repo, lambda r: append(r, "wiki/garden/tomatoes.md", "\nMulch in June.\n"))
    text = review_section(vault, name, {}, "not marked ready to merge")
    assert "wiki/garden/tomatoes.md (not marked ready to merge):" in text and "+Mulch in June." in text
    assert "Other changed files" not in text
