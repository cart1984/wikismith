"""The few git operations the server needs, via the git CLI."""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from wikismith.config import ConfigError, VaultConfig

# Hosts where a remote means "my vault is in someone else's cloud".
CLOUD_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "dev.azure.com", "ghe.com")


def with_wikismith_on_path(env: dict[str, str] | None = None) -> dict[str, str]:
    """The secret hook and job agents call `wikismith`; make sure they find this one, even when run via uvx."""
    env = dict(os.environ if env is None else env)
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    return env


NO_GIT = (
    "This vault has no git repository, so mcpedia is read-only: captures, forges and audits need git. "
    "Run `git init` in the vault, or `wikismith init --git`."
)

FALLBACK_NAME = "mcpedia"
FALLBACK_EMAIL = "mcpedia@localhost"


class GitError(RuntimeError):
    pass


LOCK_TIMEOUT = 120  # seconds; a merge with a slow push holds the lock longest


def git_dir(root: Path) -> Path | None:
    """The repository's shared `.git` folder (also from a worktree), or None outside a repository."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=root, capture_output=True, text=True
        )
    except (FileNotFoundError, NotADirectoryError):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 else None


def try_flock(path: Path) -> int | None:
    """Take an exclusive lock on a file without waiting: its file descriptor, or None if another holder has it.

    The lock lasts until the descriptor is closed, also in a child process that inherited it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


class RepoLock:
    """One lock per repository for captures, job worktrees and merges, across threads and processes.

    Every Claude Code session runs its own mcpedia, and jobs run in processes of their own, so a thread lock isn't
    enough: the lock is a file in `.git/`. Reentrant within a thread.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._fd: int | None = None

    def __enter__(self) -> RepoLock:
        self._thread_lock.acquire()
        try:
            if self._depth == 0:
                self._fd = self._acquire()
        except BaseException:
            self._thread_lock.release()
            raise
        self._depth += 1
        return self

    def __exit__(self, *exc: object) -> None:
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self._thread_lock.release()

    def _acquire(self) -> int | None:
        folder = git_dir(self.root)
        if folder is None:
            return None
        deadline = time.monotonic() + LOCK_TIMEOUT
        while (fd := try_flock(folder / "wikismith.lock")) is None:
            if time.monotonic() > deadline:
                raise GitError(f"The vault stayed busy for {LOCK_TIMEOUT} s (another mcpedia holds its lock).")
            time.sleep(0.05)
        return fd


_LOCKS: dict[Path, RepoLock] = {}
_LOCKS_GUARD = threading.Lock()


def repo_lock(root: Path) -> RepoLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(root.resolve(), RepoLock(root.resolve()))


@contextlib.contextmanager
def held(fd: int | None) -> Iterator[int | None]:
    try:
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


@dataclass(frozen=True)
class CommitResult:
    committed: bool
    pushed: bool
    warnings: tuple[str, ...] = ()


class Git:
    def __init__(self, root: Path, config: VaultConfig) -> None:
        self.root = root
        self.cfg = config.git

    def run(self, *args: str, timeout: float = 30) -> str:
        try:
            out = subprocess.run(
                ["git", *self._identity(args), *args],
                cwd=self.root,
                env=with_wikismith_on_path(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            raise GitError((e.stderr or e.stdout or str(e)).strip()) from e
        except subprocess.TimeoutExpired as e:
            raise GitError(f"git {args[0]} timed out after {timeout:.0f}s") from e
        return out.stdout.strip()

    def _identity(self, args: tuple[str, ...]) -> list[str]:
        """Commits need an identity; fall back to mcpedia's if the clone has none."""
        if args[:1] not in (("commit",), ("merge",)) or self.has_identity():
            return []
        return ["-c", f"user.name={FALLBACK_NAME}", "-c", f"user.email={FALLBACK_EMAIL}"]

    def has_identity(self) -> bool:
        try:
            subprocess.run(["git", "config", "user.email"], cwd=self.root, capture_output=True, check=True)
        except subprocess.CalledProcessError:
            return False
        return True

    def is_repo(self) -> bool:
        try:
            return self.run("rev-parse", "--is-inside-work-tree") == "true"
        except (GitError, FileNotFoundError):
            return False

    def branch(self) -> str:
        return self.run("rev-parse", "--abbrev-ref", "HEAD")

    def remote_url(self) -> str | None:
        try:
            return self.run("remote", "get-url", self.cfg.remote) or None
        except GitError:
            return None

    def pull(self) -> str | None:
        """Bring the main branch up to date with the remote (fast-forward only), if `git.sync` is on and there's a
        remote. The caller holds the repository lock. Returns what went wrong, or None."""
        if not self.cfg.sync or not self.remote_url():
            return None
        if self.branch() != self.cfg.branch:
            return f"Could not pull first: the vault folder is on {self.branch()!r}, not {self.cfg.branch!r}"
        try:
            self.run("pull", "--ff-only", "--quiet", self.cfg.remote, self.cfg.branch, timeout=60)
        except GitError as e:
            return f"Could not pull first ({_first_line(e)})"
        return None

    def commit(self, paths: list[str], message: str) -> CommitResult:
        """Commit exactly these paths (never whatever else is staged), then pull and push."""
        with repo_lock(self.root):
            return self._commit(paths, message)

    def _commit(self, paths: list[str], message: str) -> CommitResult:
        if not self.is_repo():
            return CommitResult(False, False, (NO_GIT,))
        if not self.cfg.commit_captures:
            return CommitResult(False, False, ("git.commit_captures is false: saved, for the owner to commit.",))
        warnings: list[str] = []
        branch = self.branch()
        if branch != self.cfg.branch:
            return CommitResult(
                False,
                False,
                (f"The vault is on branch {branch!r}, not {self.cfg.branch!r}; saved but not committed.",),
            )
        remote = self.remote_url() if self.cfg.sync else None
        if remote and (problem := self.pull()):
            warnings.append(f"{problem}; committing on top of the local state.")
        self.run("add", "--", *paths)
        self.run("commit", "--quiet", "-m", message, "--", *paths)
        pushed = False
        if remote:
            try:
                self.run("push", "--quiet", self.cfg.remote, self.cfg.branch, timeout=60)
                pushed = True
            except GitError as e:
                warnings.append(f"Committed locally, but the push failed ({_first_line(e)}).")
        return CommitResult(True, pushed, tuple(warnings))


def is_cloud_remote(url: str) -> bool:
    host = _host(url)
    return host is not None and any(host == h or host.endswith("." + h) for h in CLOUD_HOSTS)


def _host(url: str) -> str | None:
    if m := re.match(r"^[a-z][a-z0-9+.-]*://(?:[^@/]*@)?([^/:]+)", url, re.IGNORECASE):
        return m.group(1).lower()
    if m := re.match(r"^(?:[^@/]+@)?([^/:]+):(?!/)", url):  # scp-like: git@github.com:me/vault.git
        return m.group(1).lower()
    return None  # a local path


def check_remote_policy(git: Git, config: VaultConfig) -> None:
    """Refuse to serve a vault that may hold secrets while it syncs to a cloud host."""
    secrets = config.secrets
    if secrets.policy not in ("allow", "warn") or secrets.allow_cloud_remote:
        return
    url = git.remote_url()
    if url and is_cloud_remote(url):
        raise ConfigError(
            f"secrets.policy is {secrets.policy!r}, but the git remote {config.git.remote!r} is a cloud host "
            f"({_host(url)}). Use policy 'redact' or 'block', move the remote, or set "
            "secrets.allow_cloud_remote: true if you really mean it."
        )


def _first_line(e: Exception) -> str:
    return str(e).splitlines()[0] if str(e) else type(e).__name__
