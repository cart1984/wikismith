from pathlib import Path

import pytest
from mcp.client import Client

from conftest import git, make_vault, write_files
from wikismith.checks import ALL_CHECKS, CHECKS, Checker, format_report
from wikismith.secretscan import Scanner
from wikismith.server import build_server
from wikismith.vault import Vault


def issues(vault: Vault, check: str) -> list[tuple[str, str]]:
    result = Checker(vault, Scanner()).run((check,))  # type: ignore[arg-type]
    return [(i.path, i.message) for i in result[check]]  # type: ignore[index]


def test_links(vault: Vault) -> None:
    assert issues(vault, "links") == [
        ("wiki/garden/tomatoes.md", "[[compost-bin]]: no such page"),
        ("wiki/garden/tomatoes.md", "[[old-hose]]: points to an archived page"),
    ]


def test_links_resolve_paths_aliases_and_files(vault: Vault) -> None:
    write_files(
        vault.root,
        {
            "wiki/garden/photos.md": (
                "---\ntitle: Photos\narea: garden\ntags: []\nupdated: 2026-01-02\nsources: []\n---\n"
                "[[garden/tomatoes|Tomatoes]] [[wiki/kitchen/sourdough.md]] [[Tomatoes#Watering]] "
                "![[beds.jpg]] ![[missing.png]]\n"
            ),
            "wiki/garden/beds.jpg": "",
        },
    )
    assert [m for p, m in issues(vault, "links") if p == "wiki/garden/photos.md"] == ["[[missing.png]]: file not found"]


def test_links_resolve_files_in_raw(vault: Vault) -> None:
    write_files(
        vault.root,
        {
            "wiki/garden/photos.md": (
                "---\ntitle: Photos\narea: garden\ntags: []\nupdated: 2026-01-02\nsources: []\n---\n"
                "![[plug.jpg]] ![[raw/captures/tester.jpg]] ![[gone.jpg]]\n"
            ),
            "raw/captures/plug.jpg": "",
            "raw/captures/tester.jpg": "",
        },
    )
    assert [m for p, m in issues(vault, "links") if p == "wiki/garden/photos.md"] == ["[[gone.jpg]]: file not found"]


def test_orphans(vault: Vault) -> None:
    assert issues(vault, "orphans") == [("wiki/kitchen/knife-care.md", "no page links here")]


def test_frontmatter(vault: Vault) -> None:
    write_files(
        vault.root,
        {
            "wiki/garden/bad-area.md": ("---\ntitle: X\narea: kitchen\ntags: x\nupdated: soon\nsources: []\n---\n"),
            "wiki/garden/no-fm.md": "# No frontmatter\n",
            "wiki/garden/broken.md": "---\ntitle: [oops\n---\n",
        },
    )
    assert issues(vault, "frontmatter") == [
        ("wiki/garden/bad-area.md", "area is 'kitchen' but the page is in 'garden'"),
        ("wiki/garden/bad-area.md", "updated is not a YYYY-MM-DD date: 'soon'"),
        ("wiki/garden/bad-area.md", "tags should be a list"),
        ("wiki/garden/broken.md", issues(vault, "frontmatter")[3][1]),
        ("wiki/garden/no-fm.md", "no frontmatter"),
        ("wiki/kitchen/knife-care.md", "frontmatter is missing: sources"),
    ]
    assert issues(vault, "frontmatter")[3][1].startswith("frontmatter: invalid YAML")


def test_index(vault: Vault) -> None:
    write_files(vault.root, {"wiki/garage/tools.md": "# Tools\n"})
    assert issues(vault, "index") == [
        ("wiki/garage", "area has no _index.md"),
        ("wiki/kitchen/knife-care.md", "not listed in wiki/kitchen/_index.md"),
    ]


def test_names(vault: Vault) -> None:
    assert issues(vault, "names") == []
    write_files(
        vault.root,
        {
            "wiki/garden/Decisions.md": "# Decisions\n",
            "archive/kitchen/decisions.md": "# Decisions\n",
            "wiki/garden/_index.md": "# Garden\n",  # every area has one: not a clash
        },
    )
    assert issues(vault, "names") == [
        (
            "wiki/garden/Decisions.md",
            "same page name as archive/kitchen/decisions.md: rename one, e.g. with its area as a prefix",
        ),
        (
            "archive/kitchen/decisions.md",
            "same page name as wiki/garden/Decisions.md: rename one, e.g. with its area as a prefix",
        ),
    ]


SHARED = "The south bed gets drip irrigation on a timer at six every morning from May to September. "


def test_similar(vault: Vault) -> None:
    fm = "---\ntitle: T\narea: garden\ntags: []\nupdated: 2026-01-01\nsources: []\n---\n"
    cite = "(source: raw/notes/2026-01-05_long-import-file-name.md) " * 3  # citations don't count as shared text
    write_files(
        vault.root,
        {
            "wiki/garden/watering.md": fm + "# Watering\n\n" + SHARED + "Mulch keeps the soil moist.\n",
            "wiki/garden/digest-march.md": fm + "# March digest\n\n" + SHARED + "Ordered seed potatoes.\n" * 2,
            "wiki/garden/compost.md": fm + "# Compost\n\nTurn the pile every two weeks in summer. " + cite,
            "wiki/garden/leaves.md": fm + "# Leaves\n\nShred the leaves before adding them to the bin. " + cite,
        },
    )
    assert issues(vault, "similar") == [
        ("wiki/garden/watering.md", "62% of its text also appears in wiki/garden/digest-march.md: the same topic?")
    ]


def test_similar_only_runs_when_asked(vault: Vault) -> None:
    assert "similar" not in Checker(vault, Scanner()).run()
    assert "similar" in ALL_CHECKS and "similar" not in CHECKS


def test_unforged(vault: Vault) -> None:
    assert issues(vault, "unforged") == [
        ("raw/captures/2026-01-06_pepper-seeds.md", "not in the forge log yet (5 lines)"),
        ("raw/notes/2026-01-05_router-setup.md", "not in the forge log yet (1 line)"),
    ]


def test_unforged_reports_long_lines_and_binary_sizes(vault: Vault) -> None:
    (vault.root / "raw/import").mkdir(parents=True)
    (vault.root / "raw/import/digest.md").write_text("short\n" + "x" * 2500 + "\n")
    (vault.root / "raw/import/scan.pdf").write_bytes(b"%PDF\xff\xfe" + bytes(3 * 1024))
    found = dict(issues(vault, "unforged"))
    assert found["raw/import/digest.md"] == "not in the forge log yet (2 lines, longest line 2500 characters)"
    assert found["raw/import/scan.pdf"] == "not in the forge log yet (3 KB)"


def test_secrets_in_wiki(vault: Vault) -> None:
    assert issues(vault, "secrets") == []  # the router note is in raw/, not the wiki
    write_files(vault.root, {"wiki/garden/shed.md": "# Shed\n\nalarm\npassword: shed-2026-x\n"})
    assert issues(vault, "secrets") == [("wiki/garden/shed.md", "line 4: looks like a password or key assignment")]


def test_report() -> None:
    from wikismith.checks import Issue

    report = format_report({"links": [Issue("a.md", "x")] * 3, "orphans": []}, limit=2)
    assert report == "## links: 3 issue(s)\n- a.md: x\n- a.md: x\n- ... and 1 more\n## orphans: OK"


@pytest.mark.anyio
async def test_check_vault_tool(vault: Vault) -> None:
    async with Client(build_server(vault, warm=False), mode="legacy") as client:
        some = (await client.call_tool("check_vault", {"checks": ["orphans"]})).content[0].text  # type: ignore[union-attr]
        everything = (await client.call_tool("check_vault", {})).content[0].text  # type: ignore[union-attr]
    assert some == "## orphans: 1 issue(s)\n- wiki/kitchen/knife-care.md: no page links here"
    assert everything.count("## ") == len(CHECKS)


def test_large_files_without_git(vault: Vault) -> None:
    from PIL import Image

    (vault.root / "raw/export.json").write_bytes(b"x" * 1100 * 1024)
    (vault.root / "raw/small.json").write_bytes(b"x" * 10)
    Image.new("RGB", (2400, 100)).save(vault.root / "raw/wide.png")
    assert issues(vault, "large") == [
        ("raw/export.json", "1100 KB, over the 1024 KB limit"),
        (
            "raw/wide.png",
            "image, 0 KB (limits: 500 KB, 1600 px long edge): `wikismith shrink raw/wide.png` makes a smaller copy",
        ),
    ]


def test_large_files_only_counts_what_git_tracks(repo: Path) -> None:
    (repo / "raw/export.json").write_bytes(b"x" * 1100 * 1024)
    (repo / "raw/untracked.json").write_bytes(b"x" * 1100 * 1024)
    git(repo, "add", "raw/export.json")
    git(repo, "commit", "-qm", "export")
    vault = make_vault(repo, search={"semantic": False})
    assert issues(vault, "large") == [("raw/export.json", "1100 KB, over the 1024 KB limit")]
