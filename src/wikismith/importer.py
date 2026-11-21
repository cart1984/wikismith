"""`wikismith import`: copy files into `raw/` word for word and commit them as one owner-approved commit.

Captures are short notes that pass through an agent; long material (a document, prepared digests, a submitted review)
should go into `raw/` unchanged. Only mcpedia writes to the vault, so this is a command for the owner, run by hand:
it copies the files, runs the same checks as the pre-commit hook (large files, then the secret scan) and commits.
Nothing is ever overwritten: `raw/` only grows. If a check fails, the copies are removed again.
"""

from __future__ import annotations

import contextlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from wikismith import precommit
from wikismith.gitops import Git, GitError, repo_lock
from wikismith.vault import Vault


class ImportRefused(Exception):
    """Why the import didn't happen; nothing was changed."""


@dataclass
class ImportResult:
    files: list[str]
    commit: str
    pushed: bool
    left_out: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def target_folder(vault: Vault, folder: str) -> Path:
    """`--to` as a folder inside `raw/`: `import/2026-11` and `raw/import/2026-11` mean the same."""
    rel = PurePosixPath(folder.strip().strip("/"))
    raw = PurePosixPath(vault.rel(vault.raw_dir))
    if rel.parts[: len(raw.parts)] == raw.parts:
        rel = PurePosixPath(*rel.parts[len(raw.parts) :])
    if not rel.parts or ".." in rel.parts or any(p.startswith(".") for p in rel.parts):
        raise ImportRefused(f"--to must name a folder inside {raw}/, such as import/2026-11 (got {folder!r})")
    dest = vault.raw_dir / Path(*rel.parts)
    captures = vault.captures_dir
    if dest == captures or captures in dest.parents:
        raise ImportRefused(f"{vault.rel(captures)}/ is for captures from mcpedia; choose another folder")
    return dest


def plan(sources: list[Path], dest: Path) -> list[tuple[Path, Path]]:
    """(source, target) for every file: a folder is copied with its layout, hidden files left out."""
    pairs: list[tuple[Path, Path]] = []
    for src in sources:
        if src.is_dir():
            for f in sorted(src.rglob("*")):
                rel = f.relative_to(src)
                if f.is_file() and not any(p.startswith(".") for p in rel.parts):
                    pairs.append((f, dest / src.name / rel))
        elif src.is_file():
            pairs.append((src, dest / src.name))
        else:
            raise ImportRefused(f"{src}: no such file or folder")
    if not pairs:
        raise ImportRefused("nothing to import")
    targets = [t for _, t in pairs]
    if len(set(targets)) != len(targets):
        raise ImportRefused("two of the files would get the same name in the target folder")
    if existing := [t for t in targets if t.exists()]:
        names = ", ".join(str(t) for t in existing[:5])
        raise ImportRefused(f"already in the vault, and raw files are never overwritten: {names}")
    return pairs


def import_files(vault: Vault, sources: list[Path], folder: str, ask: precommit.Ask | None) -> ImportResult:
    dest = target_folder(vault, folder)
    git = Git(vault.root, vault.config)
    if not git.is_repo():
        raise ImportRefused("the vault isn't a git repository; copy the files into raw/ yourself")
    with repo_lock(vault.root):
        if (branch := git.branch()) != vault.config.git.branch:
            raise ImportRefused(f"the vault is on branch {branch!r}, not {vault.config.git.branch!r}")
        if git.run("diff", "--cached", "--name-only"):
            raise ImportRefused("the vault has staged changes; commit or unstage them first")
        warnings = []
        if problem := git.pull():
            warnings.append(f"{problem}; committing on top of the local state.")
        pairs = plan(sources, dest)  # after the pull: a file pulled in counts as existing
        before = _files_in(dest)
        try:
            for src, target in pairs:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
            git.run("add", "--", *(vault.rel(t) for _, t in pairs))
            if precommit.run(vault.root, ask) != 0:  # prints what it found
                raise ImportRefused("the checks stopped the import (see above); nothing was committed")
            staged = git.run("-c", "core.quotePath=false", "diff", "--cached", "--name-only", "--relative").splitlines()
            if not staged:
                raise ImportRefused("every file was left out; nothing was committed")
            git.run("commit", "--quiet", "--no-verify", "-m", _message(vault, dest, len(staged)))
        except BaseException:
            with contextlib.suppress(GitError):
                git.run("reset", "-q")  # nothing else was staged (checked above)
            _remove(vault, _files_in(dest) - before)
            raise
        left_out = sorted(_files_in(dest) - before - {vault.root / s for s in staged})
        _remove(vault, set(left_out))  # left out at the hook's question: not part of the vault
        commit = git.run("rev-parse", "--short", "HEAD")
        pushed = False
        if vault.config.git.sync and git.remote_url():
            try:
                git.run("push", "--quiet", vault.config.git.remote, vault.config.git.branch, timeout=60)
                pushed = True
            except GitError as e:
                warnings.append(f"Committed locally, but the push failed ({str(e).splitlines()[0]}).")
        return ImportResult(staged, commit, pushed, [vault.rel(p) for p in left_out], warnings)


def _message(vault: Vault, dest: Path, n: int) -> str:
    return f"Import {n} raw file{'' if n == 1 else 's'} into {vault.rel(dest)}"


def _files_in(folder: Path) -> set[Path]:
    return {p for p in folder.rglob("*") if p.is_file()} if folder.is_dir() else set()


def _remove(vault: Vault, files: set[Path]) -> None:
    """Delete files this import added (copies, and shrunk versions the hook made of them), and folders left empty."""
    for path in sorted(files):
        path.unlink(missing_ok=True)
        for parent in path.parents:
            if parent == vault.raw_dir or not parent.is_dir() or any(parent.iterdir()):
                break
            parent.rmdir()
