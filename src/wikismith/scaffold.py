"""`wikismith init` and `wikismith hook`: set up a vault, and the secret scan for the owner's own commits.

Nothing generated goes into the vault: mcpedia brings the rules and prompts, and the pre-commit hook lives in the
owner's config folder, enabled for the vault by an `includeIf` in their global git config.
"""

from __future__ import annotations

import os
import stat
import subprocess
from importlib.resources import files
from pathlib import Path

from wikismith.config import CONFIG_FILE, Layout, load_config

LOG_TEMPLATE = "# Forge log\n\nOne line per forged raw file: `- <raw path> — <date> — <area(s)>`.\n\n"


def _asset(*parts: str) -> str:
    return files("wikismith").joinpath("assets", *parts).read_text(encoding="utf-8")


def is_repo(root: Path) -> bool:
    try:
        out = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root, capture_output=True, text=True)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def init(root: Path, name: str | None = None, hook: bool = True, git: bool = True) -> list[str]:
    """Create a vault, or add what's missing to an existing one. Never overwrites anything.

    `git`: set up a repository if there isn't one, and make a first commit in it. An existing repository is left alone
    (no commit: the owner reviews the new files). Without git the vault is read-only for mcpedia.
    `hook`: enable the pre-commit secret scan for this vault (changes the global git config; see `install_hook`).
    """
    root.mkdir(parents=True, exist_ok=True)
    name = name or root.resolve().name
    log: list[str] = []
    config = load_config(root) if (root / CONFIG_FILE).is_file() else None
    layout = config.layout if config else Layout()
    had_repo = is_repo(root)
    if git and not had_repo:
        branch = config.git.branch if config else "main"
        subprocess.run(["git", "init", "-q", "-b", branch], cwd=root, check=True)
        log.append(f"created a git repository (branch {branch})")

    for folder in (layout.captures, layout.wiki, layout.archive, layout.reports):
        path = root / folder
        if not path.exists():
            path.mkdir(parents=True)
            (path / ".gitkeep").touch()
            log.append(f"created {folder}/")

    starters = {
        CONFIG_FILE: _asset("templates", "wikismith.yaml").replace("{name}", name),
        layout.instructions: _asset("templates", "CLAUDE.md").replace("{name}", name),
        layout.log: LOG_TEMPLATE,
    }
    for rel, text in starters.items():
        log.append(_write(root, rel, text))
    if hook:
        log += install_hook(root)
    if git and not had_repo:
        log.append(_first_commit(root))
    elif had_repo:
        log.append("existing git repository: review the new files and commit them yourself")
    else:
        log.append("no git repository: mcpedia can search and read, but not capture, forge or audit")
    return [line for line in log if line]


def _first_commit(root: Path) -> str:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    out = subprocess.run(
        ["git", "commit", "-q", "-m", "Set up vault with wikismith"], cwd=root, capture_output=True, text=True
    )
    if out.returncode != 0:
        reason = (out.stderr or out.stdout).strip().splitlines()[:1]
        return f"couldn't make the first commit ({' '.join(reason)}); set git user.name and user.email, then commit"
    return "made the first commit"


def _write(root: Path, rel: str, text: str) -> str:
    path = root / rel
    if path.exists():
        return "" if path.read_text(encoding="utf-8") == text else f"kept existing {rel}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return f"created {rel}"


# --- The pre-commit secret scan, for the owner's own commits (mcpedia scans what agents write itself).
#
# ~/.config/wikismith/hooks/pre-commit     the hook script
# ~/.config/wikismith/hooks.gitconfig      [core] hooksPath = ~/.config/wikismith/hooks
# ~/.gitconfig                             [includeIf "gitdir:<vault>/"] path = ~/.config/wikismith/hooks.gitconfig
#
# One script and include file for all vaults; one includeIf per vault. The script is kept outside the installed package
# so the path stays valid when wikismith is reinstalled.


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "wikismith"


def _include_key(root: Path) -> str:
    return f"includeIf.gitdir:{root.resolve()}/.path"


def _global(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "config", "--global", *args], capture_output=True, text=True)


def hook_installed(root: Path) -> bool:
    include = str(config_home() / "hooks.gitconfig")
    return include in _global("--get-all", _include_key(root)).stdout.splitlines()


def install_hook(root: Path) -> list[str]:
    """Enable the secret scan for commits in this vault. Idempotent; also refreshes the hook script."""
    if not is_repo(root):
        return ["no git repository: skipped the secret hook (run `git init`, then `wikismith hook`)"]
    home = config_home()
    hooks = home / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    script = hooks / "pre-commit"
    script.write_text(_asset("templates", "pre-commit"), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    include = home / "hooks.gitconfig"
    include.write_text(f"# Written by `wikismith hook`.\n[core]\n\thooksPath = {hooks}\n", encoding="utf-8")
    if hook_installed(root):
        return []
    _global("--add", _include_key(root), str(include)).check_returncode()
    log = [f"enabled the secret hook for this vault (an includeIf in your global git config, script in {hooks})"]
    common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=root, capture_output=True, text=True)
    own = (root / common.stdout.strip() / "hooks").resolve()
    active = sorted(p.name for p in own.glob("*") if p.is_file() and not p.name.endswith(".sample"))
    others = [name for name in active if name != "pre-commit"]
    if others:
        log.append(f"note: core.hooksPath replaces {own}, so these hooks there no longer run: {', '.join(others)}")
    return log


def remove_hook(root: Path) -> list[str]:
    if not hook_installed(root):
        return ["the secret hook isn't enabled for this vault"]
    _global("--unset-all", _include_key(root)).check_returncode()
    return ["disabled the secret hook for this vault (the script stays, for other vaults)"]
