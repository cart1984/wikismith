"""What the pre-commit hook runs (`wikismith pre-commit`): large files first, then the secret scan.

Large files are cheap to stop now and expensive later: git keeps every committed version forever. Images over the
limits are shrunk (`files.images: shrink`) or asked about (`ask`); other files over `files.max_kb` are always asked
about. Asking needs a terminal; without one (Obsidian Git, GUI clients, cron) the commit is refused with a hint.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from wikismith.config import CONFIG_FILE, Files, VaultConfig, parse_config
from wikismith.media import KEEP, MediaError, is_image, shrink, too_large
from wikismith.secretscan import Scanner
from wikismith.vault import Vault, default_cache_dir

# Asks the owner a question with one-letter choices; returns the letter. None: there's no terminal to ask.
Ask = Callable[[str, str], str]


def run(root: Path, ask: Ask | None) -> int:
    """The hook: 0 to let the commit go ahead, 1 to stop it."""
    config, originals = _config(root)
    if not _large_files(root, config.files, originals, ask):
        return 1
    return scan_staged(root, Scanner.for_config(config.secrets))


def _config(root: Path) -> tuple[VaultConfig, Path]:
    if (root / CONFIG_FILE).is_file():
        vault = Vault.open(root)
        return vault.config, vault.cache_dir / "originals"
    return parse_config({"name": "repository"}), default_cache_dir(None) / "originals"


def _large_files(root: Path, limits: Files, originals: Path, ask: Ask | None) -> bool:
    refused: list[str] = []
    for name in _staged(root, "AM"):
        path = root / name
        size = int(_git(root, "cat-file", "-s", f":{name}"))
        if not path.is_file() or not too_large(path, size, limits):
            continue
        if is_image(path) and path.suffix.lower() not in KEEP:
            choice = "s" if limits.images == "shrink" else _choose(ask, f"{name} is a large image", "skla")
        else:
            choice = _choose(ask, f"{name} is {size // 1024} KB (limit {limits.max_kb} KB)", "kla")
        if choice == "s":
            try:
                result = shrink(path, limits, originals)
            except MediaError as e:
                refused.append(f"{name}: {e}")
                continue
            if result.new != path:
                _git(root, "rm", "--cached", "-q", "--", name)
            _git(root, "add", "--", str(result.new.relative_to(root)))
            print(f"shrunk {result.describe(root)} (original kept in {originals})", file=sys.stderr)
        elif choice == "l":
            _git(root, "reset", "-q", "--", name)
            print(f"left out {name}: it stays in the folder, uncommitted", file=sys.stderr)
        elif choice == "a":
            print("commit aborted", file=sys.stderr)
            return False
        elif choice is None:  # nobody to ask
            fix = f"run `wikismith shrink {name}`" if is_image(path) else "commit from a terminal to decide"
            refused.append(f"{name}: over the size limit; {fix}, or bypass once with `git commit --no-verify`")
    for line in refused:
        print(line, file=sys.stderr)
    return not refused


def _choose(ask: Ask | None, what: str, choices: str) -> str | None:
    if ask is None:
        return None
    labels = {"s": "[s]hrink", "k": "[k]eep", "l": "[l]eave out", "a": "[a]bort"}
    return ask(f"{what}: {', '.join(labels[c] for c in choices)}? ", choices)


def tty_ask() -> Ask | None:
    """Ask on the terminal, if there is one (git hooks don't get stdin, so it's /dev/tty)."""
    try:
        tty = open("/dev/tty", "r+", encoding="utf-8")  # noqa: SIM115 (open for the hook's lifetime)
    except OSError:
        return None

    def ask(question: str, choices: str) -> str:
        while True:
            tty.write(question)
            tty.flush()
            answer = tty.readline()
            if not answer:
                return "a"
            if answer.strip().lower()[:1] in choices:
                return answer.strip().lower()[0]

    return ask


def scan_staged(root: Path, scanner: Scanner) -> int:
    return scan(root, scanner, [(n, lambda n=n: _staged_text(root, n)) for n in _staged(root, "ACMR")])


def _staged_text(root: Path, name: str) -> str:
    """The staged version of a file as text; empty for binary files (images, PDFs), which hold no text to scan."""
    data = subprocess.run(["git", "show", f":{name}"], cwd=root, capture_output=True, check=True).stdout
    return "" if b"\0" in data[:8192] else data.decode("utf-8", errors="replace")


def scan(root: Path, scanner: Scanner, sources: list[tuple[str, Callable[[], str]]]) -> int:
    """Scan for secrets and personal data. 1 if any are found, except personal data under `secrets.pii: warn`."""
    found = warned = 0
    for name, read in sources:
        try:
            text = read()
        except (OSError, UnicodeDecodeError, subprocess.CalledProcessError) as e:
            print(f"{name}: could not read ({e})", file=sys.stderr)
            continue
        for f in scanner.scan(text):
            if f.pii and scanner.pii == "warn":
                warned += 1
                print(f"{name}:{f.line}: {f.description} ({f.rule}, personal data: warning only)")
            else:
                found += 1
                print(f"{name}:{f.line}: {f.description} ({f.rule})")
    if found:
        print(
            f"\n{found} possible secret(s) or personal data. Remove or redact them; values aren't shown.",
            file=sys.stderr,
        )
    elif warned:
        print(f"\n{warned} possible personal data (secrets.pii is warn): check before you push.", file=sys.stderr)
    return 1 if found else 0


def _staged(root: Path, filter: str) -> list[str]:
    out = _git(root, "-c", "core.quotePath=false", "diff", "--cached", "--name-only", f"--diff-filter={filter}", "-z")
    return [n for n in out.split("\0") if n]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout
