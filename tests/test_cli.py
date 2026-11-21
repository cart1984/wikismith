import subprocess
from pathlib import Path

import pytest

from conftest import write_files
from wikismith import __version__
from wikismith.cli import main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 1
    assert "usage: wikismith" in capsys.readouterr().out


def test_missing_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "--vault", str(tmp_path)]) == 2
    assert "wikismith init" in capsys.readouterr().err


def test_check(vault_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "--vault", str(vault_root), "--check", "orphans"]) == 1
    assert "knife-care.md: no page links here" in capsys.readouterr().out
    (vault_root / "wiki/kitchen/knife-care.md").unlink()
    assert main(["check", "--vault", str(vault_root), "--check", "orphans"]) == 0


def test_search(vault_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (vault_root / "wikismith.yaml").write_text("name: test-vault\nsearch:\n  semantic: false\n")
    assert main(["search", "--vault", str(vault_root), "San", "Marzano"]) == 0
    assert "wiki/garden/tomatoes.md" in capsys.readouterr().out


def test_scan_files(vault_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    secret = vault_root / "raw/notes/2026-01-05_router-setup.md"
    clean = vault_root / "wiki/garden/tomatoes.md"
    assert main(["scan", "--vault", str(vault_root), str(clean)]) == 0
    assert main(["scan", "--vault", str(vault_root), str(clean), str(secret)]) == 1
    out = capsys.readouterr()
    assert f"{secret}:1: password in a sentence (password-sentence)" in out.out
    assert "hunter2" not in out.out + out.err


def test_scan_uses_vault_patterns(vault_root: Path) -> None:
    (vault_root / "wikismith.yaml").write_text(
        "name: test-vault\nsecrets:\n  patterns:\n    - id: shed-code\n      regex: 'SHED-\\d{4}'\n"
    )
    write_files(vault_root, {"wiki/garden/shed.md": "Code SHED-1234\n"})
    assert main(["scan", "--vault", str(vault_root), str(vault_root / "wiki/garden/shed.md")]) == 1


def test_scan_staged_reads_the_index(vault_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=vault_root, check=True, capture_output=True)

    git("init", "-q")
    write_files(vault_root, {"wiki/garden/shed.md": "password: shed-2026-x\n"})
    git("add", "wiki/garden/shed.md")
    (vault_root / "wiki/garden/shed.md").write_text("cleaned up, but not staged\n")
    assert main(["scan", "--vault", str(vault_root), "--staged"]) == 1  # the staged version counts
    assert "wiki/garden/shed.md:1: password or key assignment" in capsys.readouterr().out
    git("add", "wiki/garden/shed.md")
    assert main(["scan", "--vault", str(vault_root), "--staged"]) == 0


@pytest.mark.anyio
async def test_serve_stdio_subprocess(vault_root: Path, tmp_path: Path) -> None:
    import sys

    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    (vault_root / "wikismith.yaml").write_text("name: test-vault\nsearch:\n  semantic: false\n")
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "wikismith.cli", "serve", "--vault", str(vault_root)]
    )
    errlog = (tmp_path / "server.log").open("w")  # a real file: pytest's captured stderr has no fd
    async with Client(stdio_client(params, errlog=errlog), mode="legacy") as client:
        assert client.instructions is not None and "test-vault" in client.instructions
        result = await client.call_tool("read_page", {"page": "sourdough"})
    errlog.close()
    assert "Dutch oven" in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.parametrize(("pii", "code"), [("block", 1), ("redact", 1), ("warn", 0), ("allow", 0)])
def test_scan_personal_data(vault_root: Path, pii: str, code: int, capsys: pytest.CaptureFixture[str]) -> None:
    (vault_root / "wikismith.yaml").write_text(
        f"name: t\nsecrets:\n  pii: {pii}\n  patterns:\n"
        "    - {id: parcel-id, regex: '\\bPCL-\\d{4}-\\d{6}\\b', description: Parcel ID, pii: true}\n"
    )
    note = vault_root / "raw/notes/lot.md"
    note.write_text("Lot PCL-2026-000123\n")
    assert main(["scan", "--vault", str(vault_root), str(note)]) == code
    out = capsys.readouterr()
    assert ("Parcel ID" in out.out) is (pii != "allow")
    assert ("warning only" in out.out) is (pii == "warn")


def test_port_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from wikismith.cli import build_parser

    monkeypatch.setenv("WIKISMITH_PORT", "9123")
    assert build_parser().parse_args(["serve"]).port == 9123
    monkeypatch.delenv("WIKISMITH_PORT")
    assert build_parser().parse_args(["serve"]).port == 8765


def test_import(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "review.md").write_text("# Review\n\nShipped it.\n")
    args = ["import", "--vault", str(repo), "--to", "import/x", str(tmp_path / "review.md")]
    assert main(args) == 0
    assert "Committed 1 file as " in capsys.readouterr().out
    assert main(args) == 1  # a second time: the file is in raw/ already
    assert "never overwritten" in capsys.readouterr().err


def test_area_add(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["area", "add", "--vault", str(repo), "pets", "The cat", "trips"]) == 2  # no description for trips
    assert "a name and a description" in capsys.readouterr().err
    assert main(["area", "add", "--vault", str(repo), "pets", "The cat", "trips", "Holidays"]) == 0
    assert "Captured to raw/captures/" in capsys.readouterr().out
    assert main(["area", "add", "--vault", str(repo), "pets", "Again"]) == 1  # approved, not created yet
    assert "approved already" in capsys.readouterr().err
    assert main(["area", "add", "--vault", str(repo), "garden", "Vegetables"]) == 1
    assert "exists already" in capsys.readouterr().err
