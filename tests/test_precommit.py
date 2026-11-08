"""The pre-commit hook's large-file step, with and without a terminal to ask."""

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from conftest import git
from test_media import photo
from wikismith.precommit import Ask, run


def stage(repo: Path, rel: str, write) -> Path:  # type: ignore[no-untyped-def]
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    write(path)
    git(repo, "add", "--", rel)
    return path


def staged(repo: Path) -> list[str]:
    return git(repo, "diff", "--cached", "--name-only").splitlines()


def answer(letter: str) -> Ask:
    return lambda question, choices: letter


def test_large_images_are_shrunk_and_restaged(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    stage(repo, "raw/photos/shed.jpg", photo)
    assert run(repo, ask=None) == 0  # `shrink` needs nobody to answer
    assert "shrunk raw/photos/shed.jpg" in capsys.readouterr().err
    blob = subprocess.run(["git", "cat-file", "-s", ":raw/photos/shed.jpg"], cwd=repo, capture_output=True, text=True)
    assert int(blob.stdout) <= 500 * 1024  # the index holds the small version


def test_converted_images_replace_the_original_in_the_index(repo: Path) -> None:
    stage(repo, "raw/scan.bmp", lambda p: Image.new("RGB", (300, 200), "white").save(p, "BMP"))
    assert run(repo, ask=None) == 0
    assert staged(repo) == ["raw/scan.png"]


def test_ask_mode(repo: Path) -> None:
    (repo / "wikismith.yaml").write_text("name: test-vault\nfiles:\n  images: ask\n")
    stage(repo, "raw/shed.jpg", photo)
    assert run(repo, ask=None) == 1  # no terminal: refused
    assert run(repo, ask=answer("l")) == 0 and "raw/shed.jpg" not in staged(repo)  # left out
    assert (repo / "raw/shed.jpg").is_file()  # still in the folder
    git(repo, "add", "raw/shed.jpg")
    assert run(repo, ask=answer("k")) == 0 and "raw/shed.jpg" in staged(repo)  # kept as it is
    assert run(repo, ask=answer("a")) == 1


def test_large_documents_are_always_asked_about(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    stage(repo, "raw/manual.pdf", lambda p: p.write_bytes(b"%PDF-1.7\n" + b"\0" * 2 * 1024 * 1024))
    assert run(repo, ask=None) == 1
    assert "commit from a terminal to decide" in capsys.readouterr().err
    assert run(repo, ask=answer("k")) == 0


def test_secrets_are_still_scanned(repo: Path) -> None:
    stage(repo, "raw/notes/router.md", lambda p: p.write_text("password: hunter2-not-real\n"))
    stage(repo, "raw/photos/shed.jpg", photo)  # a binary file doesn't break the scan
    assert run(repo, ask=None) == 1
