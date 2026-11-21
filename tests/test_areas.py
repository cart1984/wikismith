import subprocess
from pathlib import Path

import pytest

from conftest import make_vault
from wikismith.areas import InvalidArea, approve_areas
from wikismith.markdown import parse_frontmatter
from wikismith.merges import approved_areas
from wikismith.vault import Vault


@pytest.fixture
def vault(repo: Path) -> Vault:
    return make_vault(repo)


def test_approves_areas_with_a_committed_capture(vault: Vault) -> None:
    result = approve_areas(vault, {"home-network": "Router, Wi-Fi and the NAS", "pets": "The cat:  vet,  food"})
    assert result.committed
    text = (vault.root / result.path).read_text()
    fm = parse_frontmatter(text)
    assert fm.data["approves_areas"] == ["home-network", "pets"]
    assert "# New areas: home-network, pets" in fm.body
    assert "- `pets`: The cat: vet, food" in fm.body
    assert {"home-network", "pets"} <= approved_areas(vault)  # what the merge check accepts
    log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=vault.root, capture_output=True, text=True)
    assert log.stdout.strip() == "Add capture: New areas: home-network, pets"


@pytest.mark.parametrize(
    ("areas", "error"),
    [
        ({}, "at least one"),
        ({"Home Network": "x"}, "lowercase"),
        ({"-pets": "x"}, "lowercase"),
        ({"garden": "Vegetables"}, "exists already"),
        ({"pets": "  "}, "description"),
    ],
)
def test_refuses(vault: Vault, areas: dict[str, str], error: str) -> None:
    before = sorted(vault.captures_dir.glob("*.md"))
    with pytest.raises(InvalidArea, match=error):
        approve_areas(vault, areas)
    assert sorted(vault.captures_dir.glob("*.md")) == before  # nothing saved


def test_long_lists_get_a_short_title(vault: Vault) -> None:
    areas = {f"area-number-{i}": "x" for i in range(12)}
    result = approve_areas(vault, areas)
    assert "# New areas: 12 approved by the owner" in (vault.root / result.path).read_text()
