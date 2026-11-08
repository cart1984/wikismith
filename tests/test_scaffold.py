import os
import subprocess
import sys
from pathlib import Path

import pytest

from wikismith.cli import main
from wikismith.config import load_config
from wikismith.instructions import build_instructions
from wikismith.scaffold import config_home, hook_installed, init, install_hook, remove_hook
from wikismith.vault import Vault


def test_init_new_vault(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    log = init(root)
    assert load_config(root).name == "notes"
    for rel in ("raw/captures", "wiki", "archive", "reports"):
        assert (root / rel).is_dir()
    claude = (root / "CLAUDE.md").read_text()
    assert claude.startswith("# notes\n\nThis is a wikismith vault") and "@" not in claude
    # Nothing generated in the vault: rules and prompts come from mcpedia, the hook from the global git config.
    assert not (root / ".wikismith").exists() and not (root / ".claude").exists()
    assert not (root / ".git/hooks/pre-commit").exists()
    hook = config_home() / "hooks/pre-commit"
    assert os.access(hook, os.X_OK) and "wikismith pre-commit" in hook.read_text()
    assert hook_installed(root)
    assert "created wikismith.yaml" in log and any("enabled the secret hook" in line for line in log)


def test_init_never_overwrites(vault_root: Path) -> None:
    before = (vault_root / "CLAUDE.md").read_text()
    log = init(vault_root, git=False)
    assert (vault_root / "CLAUDE.md").read_text() == before
    assert "kept existing CLAUDE.md" in log and "kept existing wikismith.yaml" in log
    assert any("skipped the secret hook" in line for line in log)
    assert any("mcpedia can search and read, but not capture" in line for line in log)
    assert not (vault_root / ".git").exists()


@pytest.fixture
def identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "Owner")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "owner@example.com")


def test_init_sets_up_git_and_commits(tmp_path: Path, identity: None) -> None:
    root = tmp_path / "fresh"
    log = init(root)
    assert "created a git repository (branch main)" in log and "made the first commit" in log
    git = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=True).stdout  # noqa: E731
    assert git("log", "--format=%s") == "Set up vault with wikismith\n"
    assert git("status", "--porcelain") == ""


def test_init_leaves_an_existing_repository_alone(tmp_path: Path, identity: None) -> None:
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(tmp_path)], check=True)
    log = init(tmp_path)
    assert "existing git repository: review the new files and commit them yourself" in log
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=tmp_path, capture_output=True)
    assert head.returncode != 0  # no commit made
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=tmp_path, capture_output=True, text=True)
    assert branch.stdout.strip() == "trunk"


def test_cli_init_without_git(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init", str(tmp_path / "v"), "--no-git"]) == 0
    assert "mcpedia can search and read" in capsys.readouterr().out
    assert not (tmp_path / "v/.git").exists()


def test_hook_is_idempotent_and_removable(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert install_hook(tmp_path)
    assert install_hook(tmp_path) == []  # already enabled: no second includeIf
    gitconfig = Path(os.environ["GIT_CONFIG_GLOBAL"]).read_text()
    assert gitconfig.count("includeIf") == 1 and f"gitdir:{tmp_path.resolve()}/" in gitconfig
    hooks_path = subprocess.run(["git", "config", "core.hooksPath"], cwd=tmp_path, capture_output=True, text=True)
    assert hooks_path.stdout.strip() == str(config_home() / "hooks")
    other = tmp_path.parent / "other"  # another repository isn't affected
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    assert subprocess.run(["git", "config", "core.hooksPath"], cwd=other, capture_output=True).returncode != 0
    assert remove_hook(tmp_path) == ["disabled the secret hook for this vault (the script stays, for other vaults)"]
    assert not hook_installed(tmp_path)
    assert remove_hook(tmp_path) == ["the secret hook isn't enabled for this vault"]


def test_other_hooks_are_reported(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".git/hooks/post-commit").write_text("#!/bin/sh\n")
    log = install_hook(tmp_path)
    assert any("no longer run: post-commit" in line for line in log)


def test_initialized_vault_works_with_the_server(tmp_path: Path) -> None:
    init(tmp_path / "v", name="Fresh")
    vault = Vault.open(tmp_path / "v")
    assert '"Fresh" knowledge vault' in build_instructions(vault)


def _commit_env() -> dict[str, str]:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com"}
    env |= {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    env["PATH"] = f"{Path(sys.executable).parent}:{env['PATH']}"  # this venv's wikismith
    return env


def test_hook_blocks_secrets(tmp_path: Path) -> None:
    root = tmp_path / "v"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    init(root)
    env = _commit_env()
    (root / "raw/captures/2026-02-01_router.md").write_text("password: hunter2-not-real\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    blocked = subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=root, env=env, capture_output=True, text=True)
    assert blocked.returncode != 0 and "password or key assignment" in blocked.stderr  # git routes hook output
    (root / "raw/captures/2026-02-01_router.md").write_text("password is in the password manager\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=root, env=env, check=True)


def test_hook_runs_the_repositorys_own_pre_commit(tmp_path: Path) -> None:
    root = tmp_path / "v"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    init(root)
    own = root / ".git/hooks/pre-commit"
    own.write_text("#!/bin/sh\necho own hook >&2\nexit 1\n")
    own.chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    blocked = subprocess.run(
        ["git", "commit", "-q", "-m", "x"], cwd=root, env=_commit_env(), capture_output=True, text=True
    )
    assert blocked.returncode != 0 and "own hook" in blocked.stderr


def test_cli_init(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init", str(tmp_path / "v"), "--name", "Mine", "--no-git", "--no-hook"]) == 0
    assert "Vault ready" in capsys.readouterr().out
    assert load_config(tmp_path / "v").name == "Mine"


def test_cli_init_without_a_terminal_leaves_the_global_config_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init", str(tmp_path / "v"), "--no-git"]) == 0  # pytest's stdin isn't a terminal
    assert "add it later with `wikismith hook`" in capsys.readouterr().out
    assert not Path(os.environ["GIT_CONFIG_GLOBAL"]).exists()


def test_cli_hook(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "v"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    init(root, hook=False)
    assert main(["hook", "--vault", str(root)]) == 0
    assert "enabled the secret hook" in capsys.readouterr().out and hook_installed(root)
    assert main(["hook", "--vault", str(root)]) == 0
    assert "the secret hook is enabled for this vault" in capsys.readouterr().out
    assert main(["hook", "--vault", str(root), "--remove"]) == 0
    assert not hook_installed(root)


def test_hook_commits_the_shrunk_image(tmp_path: Path) -> None:
    from test_media import photo

    root = tmp_path / "v"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    init(root)
    env = _commit_env()
    photo(root / "raw/shed.jpg")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    done = subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=root, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "shrunk raw/shed.jpg" in done.stderr
    size = subprocess.run(["git", "cat-file", "-s", "HEAD:raw/shed.jpg"], cwd=root, capture_output=True, text=True)
    assert int(size.stdout) <= 500 * 1024  # git committed what the hook staged


def test_every_setting_in_the_template_is_valid() -> None:
    """Uncommenting the template's settings must give a valid config: no stale or misspelled keys."""
    import re

    import yaml

    from wikismith.config import parse_config
    from wikismith.scaffold import _asset

    lines = []
    for line in _asset("templates", "wikismith.yaml").replace("{name}", "x").splitlines():
        m = re.match(r"^# (\s*(?:[a-z_]+:|- ).*)$", line)  # a commented setting, not a prose comment
        lines.append(m.group(1) if m else line)
    data = yaml.safe_load("\n".join(lines))
    assert {"files", "access", "secrets", "git", "jobs"} <= set(data)
    parse_config(data)
