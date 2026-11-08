from pathlib import Path

import pytest

from conftest import make_vault, write_files
from wikismith.pages import Ambiguous, NotFound
from wikismith.reader import Reader
from wikismith.vault import AccessDenied, Vault


@pytest.mark.parametrize("name", ["tomatoes", "Tomatoes", "garden/tomatoes", "wiki/garden/tomatoes.md"])
def test_read_page_by_name_or_path(vault: Vault, name: str) -> None:
    out = Reader(vault).read_page(name)
    assert out.startswith("Path: wiki/garden/tomatoes.md\n")
    assert "San Marzano" in out


def test_read_capture_by_path(vault: Vault) -> None:
    out = Reader(vault).read_page("raw/captures/2026-01-06_pepper-seeds.md")
    assert "chili pepper seeds" in out


def test_read_page_refuses_raw(vault: Vault) -> None:
    with pytest.raises(AccessDenied):
        Reader(vault).read_page("raw/notes/2026-01-05_router-setup.md")


def test_archived_pages_are_readable_and_marked(vault: Vault, vault_root: Path) -> None:
    text = Reader(vault).read_page("old-hose")
    assert text.startswith("Path: archive/garden/old-hose.md [archived: outdated]\n") and "Replaced in 2025" in text
    assert "[archived" in Reader(vault).read_page("archive/garden/old-hose.md")
    assert "[archived" not in Reader(vault).read_page("tomatoes")
    wiki_only = make_vault(vault_root, access={"read": ["wiki"]})
    with pytest.raises(NotFound):
        Reader(wiki_only).read_page("old-hose")


def test_ambiguous_name(vault: Vault) -> None:
    write_files(vault.root, {"wiki/kitchen/tomatoes.md": "# Tomato sauce\n"})
    with pytest.raises(Ambiguous) as exc:
        Reader(vault).read_page("tomatoes")
    assert exc.value.candidates == ["wiki/garden/tomatoes.md", "wiki/kitchen/tomatoes.md"]
    assert "Tomato sauce" in Reader(vault).read_page("kitchen/tomatoes")


def test_unknown_page(vault: Vault) -> None:
    with pytest.raises(NotFound):
        Reader(vault).read_page("does-not-exist")


def test_mask_applies_to_reads(vault: Vault) -> None:
    reader = Reader(vault, mask=lambda text: text.replace("San Marzano", "***"))
    assert "San Marzano" not in reader.read_page("tomatoes")


def test_list_areas(vault: Vault) -> None:
    out = Reader(vault).list_areas()
    assert "- **garden** (2 pages): Beds, vegetables and watering." in out
    assert "- **kitchen** (2 pages)" in out


def test_list_area_marks_pages_missing_from_index(vault: Vault) -> None:
    out = Reader(vault).list_area("kitchen")
    assert "Index (wiki/kitchen/_index.md):" in out
    assert "- wiki/kitchen/sourdough.md — Sourdough\n" in out + "\n"
    assert "- wiki/kitchen/knife-care.md — Knife care (not in the index)" in out


def test_list_unknown_area(vault: Vault) -> None:
    with pytest.raises(NotFound, match="Areas: garden, kitchen"):
        Reader(vault).list_area("garage")
