import subprocess
from pathlib import Path

import pytest

from conftest import make_vault
from wikismith.importer import ImportRefused, import_files, target_folder
from wikismith.vault import Vault


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def vault(repo: Path) -> Vault:
    return make_vault(repo)


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    src = tmp_path / "outside"
    (src / "digests/march").mkdir(parents=True)
    (src / "review.md").write_text("# Annual review\n\nShipped the annotation tool.\n")
    (src / "digests/march/week-1.md").write_text("# Week 1\n\nDecided on the new schema.\n")
    (src / "digests/.DS_Store").write_text("x")
    return src


def test_imports_files_and_folders_as_one_commit(vault: Vault, outside: Path) -> None:
    head = git(vault.root, "rev-parse", "HEAD")
    result = import_files(vault, [outside / "review.md", outside / "digests"], "raw/import/2026-11", None)
    assert sorted(result.files) == ["raw/import/2026-11/digests/march/week-1.md", "raw/import/2026-11/review.md"]
    assert result.pushed and not result.left_out
    assert (vault.root / "raw/import/2026-11/review.md").read_text() == (outside / "review.md").read_text()
    assert git(vault.root, "rev-list", "--count", f"{head}..HEAD") == "1"
    assert git(vault.root, "log", "-1", "--format=%s") == "Import 2 raw files into raw/import/2026-11"
    assert git(vault.root, "status", "--porcelain") == ""
    assert git(vault.root, "rev-parse", "HEAD") == git(vault.root, "rev-parse", "origin/main")


def test_never_overwrites_raw_files(vault: Vault, outside: Path) -> None:
    import_files(vault, [outside / "review.md"], "import/a", None)
    (outside / "review.md").write_text("changed\n")
    with pytest.raises(ImportRefused, match="never overwritten"):
        import_files(vault, [outside / "review.md"], "import/a", None)
    assert "Shipped" in (vault.root / "raw/import/a/review.md").read_text()


def test_a_secret_stops_the_import_and_leaves_nothing_behind(
    vault: Vault, outside: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (outside / "digests/march/week-2.md").write_text("# Week 2\n\nalarm\npassword: shed-2026-x\n")
    head = git(vault.root, "rev-parse", "HEAD")
    with pytest.raises(ImportRefused, match="checks stopped"):
        import_files(vault, [outside / "digests"], "import/b", None)
    assert "week-2.md:4" in capsys.readouterr().out
    assert not (vault.raw_dir / "import").exists()
    assert git(vault.root, "rev-parse", "HEAD") == head
    assert git(vault.root, "status", "--porcelain") == ""


def test_refuses_with_staged_changes(vault: Vault, outside: Path) -> None:
    (vault.root / "notes.md").write_text("mine\n")
    git(vault.root, "add", "notes.md")
    with pytest.raises(ImportRefused, match="staged changes"):
        import_files(vault, [outside / "review.md"], "import/c", None)
    assert not (vault.raw_dir / "import").exists()


@pytest.mark.parametrize(
    ("folder", "expected"),
    [("import/2026-11", "raw/import/2026-11"), ("raw/import/2026-11/", "raw/import/2026-11"), ("x", "raw/x")],
)
def test_target_folder(vault: Vault, folder: str, expected: str) -> None:
    assert vault.rel(target_folder(vault, folder)) == expected


@pytest.mark.parametrize("folder", ["", "raw", "../wiki", "import/../../wiki", ".hidden", "captures", "raw/captures/x"])
def test_target_folder_refuses(vault: Vault, folder: str) -> None:
    with pytest.raises(ImportRefused):
        target_folder(vault, folder)
