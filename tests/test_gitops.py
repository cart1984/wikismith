from pathlib import Path

import pytest

from conftest import git, make_vault, write_files
from wikismith.config import ConfigError
from wikismith.gitops import Git, check_remote_policy, is_cloud_remote


@pytest.mark.parametrize(
    ("url", "cloud"),
    [
        ("git@github.com:someone/vault.git", True),
        ("https://github.com/someone/vault.git", True),
        ("ssh://git@gitlab.com/someone/vault.git", True),
        ("https://user@my.ghe.com/org/vault", True),
        ("ssh://me@nas.local/volume1/git/vault.git", False),
        ("me@192.168.1.10:git/vault.git", False),
        ("/Volumes/backup/vault.git", False),
        ("file:///srv/git/vault.git", False),
    ],
)
def test_cloud_remote_detection(url: str, cloud: bool) -> None:
    assert is_cloud_remote(url) is cloud


@pytest.mark.parametrize(
    ("policy", "allow_cloud", "refused"),
    [("allow", False, True), ("warn", False, True), ("redact", False, False), ("allow", True, False)],
)
def test_remote_policy(repo: Path, policy: str, allow_cloud: bool, refused: bool) -> None:
    git(repo, "remote", "set-url", "origin", "git@github.com:someone/vault.git")
    vault = make_vault(repo, secrets={"policy": policy, "allow_cloud_remote": allow_cloud})
    if refused:
        with pytest.raises(ConfigError, match="cloud host"):
            check_remote_policy(Git(repo, vault.config), vault.config)
    else:
        check_remote_policy(Git(repo, vault.config), vault.config)


def test_commit_only_the_given_paths(repo: Path) -> None:
    write_files(repo, {"wiki/garden/new.md": "# New\n", "wiki/garden/other.md": "# Other\n"})
    git(repo, "add", "wiki/garden/other.md")  # staged by someone else: must stay out of the commit
    result = Git(repo, make_vault(repo).config).commit(["wiki/garden/new.md"], "Add new page")
    assert (result.committed, result.pushed) == (True, True)
    assert git(repo, "show", "--name-only", "--format=", "HEAD") == "wiki/garden/new.md"
    assert git(repo, "diff", "--cached", "--name-only") == "wiki/garden/other.md"


def test_sync_off_commits_locally_only(repo: Path, tmp_path: Path) -> None:
    write_files(repo, {"wiki/garden/new.md": "# New\n"})
    result = Git(repo, make_vault(repo, git={"sync": False}).config).commit(["wiki/garden/new.md"], "Local only")
    assert (result.committed, result.pushed) == (True, False)
    assert git(tmp_path / "remote.git", "log", "-1", "--format=%s", "main") == "init"


def test_no_remote_commits_without_warning(repo: Path) -> None:
    git(repo, "remote", "remove", "origin")
    write_files(repo, {"wiki/garden/new.md": "# New\n"})
    result = Git(repo, make_vault(repo).config).commit(["wiki/garden/new.md"], "No remote")
    assert (result.committed, result.pushed, result.warnings) == (True, False, ())


def test_repo_lock_holds_across_processes(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Another mcpedia (another process) holding the lock makes this one wait, then give up."""
    import subprocess
    import sys

    from wikismith import gitops

    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD_LOCK, str(repo / ".git" / "wikismith.lock")],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None and holder.stdout.readline() == "locked\n"
    monkeypatch.setattr(gitops, "LOCK_TIMEOUT", 0.3)
    lock = gitops.RepoLock(repo)
    with pytest.raises(gitops.GitError, match="stayed busy"), lock:
        pass
    holder.wait(timeout=10)
    with lock, lock:  # free again, and reentrant
        pass


HOLD_LOCK = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)
fcntl.flock(fd, fcntl.LOCK_EX)
print("locked", flush=True)
time.sleep(1)
"""
