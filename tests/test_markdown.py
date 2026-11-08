import dataclasses

import pytest

from wikismith.markdown import Link, blank_code, headings, parse, parse_frontmatter, wikilinks


def test_frontmatter() -> None:
    fm = parse_frontmatter("---\ntitle: A\ntags: [x]\n---\n\n# A\n")
    assert fm.present and fm.error is None
    assert fm.data == {"title": "A", "tags": ["x"]}
    assert fm.body == "\n# A\n"


def test_no_frontmatter() -> None:
    fm = parse_frontmatter("# Just a page\n")
    assert not fm.present
    assert fm.body == "# Just a page\n"


def test_broken_frontmatter_is_reported() -> None:
    assert parse_frontmatter("---\ntitle: [unclosed\n---\nbody").error is not None
    colon = parse_frontmatter("---\narea: x\ntitle: Power: usage and supplier\n---\nbody").error
    assert colon == "invalid YAML on line 3: mapping values are not allowed here"
    assert parse_frontmatter("---\n- a list\n---\nbody").error == "frontmatter is not a mapping"
    assert parse_frontmatter("---\ntitle: A\nbody").error == "frontmatter is not closed with ---"


def test_wikilinks() -> None:
    text = "See [[tomatoes]], [[garden/my-garden#Beds|my beds]] and ![[photo.png]]. [[ ]] is empty."
    assert wikilinks(text) == (
        Link("tomatoes"),
        Link("garden/my-garden", "Beds", "my beds"),
        Link("photo.png"),
    )


def test_wikilinks_ignore_code() -> None:
    text = "Real [[a]].\n\n```\n[[not-a-link]]\n```\n\nInline `[[nope]]` too.\n"
    assert [link.target for link in wikilinks(text)] == ["a"]


def test_headings_skip_code_and_keep_offsets() -> None:
    text = "# Title\n\n```\n# not a heading\n```\n\n## Section ##\n"
    found = headings(text)
    assert [(h.level, h.text) for h in found] == [(1, "Title"), (2, "Section")]
    assert text[found[1].offset :].startswith("## Section")


def test_headings_keep_inline_code() -> None:
    assert [h.text for h in headings("## Using `git worktree`\n")] == ["Using `git worktree`"]


def test_blank_code_keeps_positions() -> None:
    text = "a `[[x]]` b\n```\n[[y]]\n```\nc [[z]]\n"
    blanked = blank_code(text)
    assert len(blanked) == len(text)
    assert blanked.count("\n") == text.count("\n")
    assert "[[x]]" not in blanked and "[[y]]" not in blanked
    assert blanked.index("[[z]]") == text.index("[[z]]")
    assert "`[[x]]`" in blank_code(text, inline=False)


def test_documents_are_read_only() -> None:
    doc = parse("# T\n\n[[x]]\n")
    assert isinstance(doc.links, tuple) and isinstance(doc.headings, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        doc.links = ()  # type: ignore[misc]


def test_parse() -> None:
    doc = parse("---\ntitle: T\n---\n# T\n\n[[x]]\n")
    assert doc.frontmatter.data["title"] == "T"
    assert doc.links == (Link("x"),)
    assert doc.headings[0].text == "T"
