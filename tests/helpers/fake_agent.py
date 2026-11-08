"""Stands in for headless Claude Code in job tests. Runs in the job's worktree and reads its instructions from stdin,
like the real agent; argv[1] says what to do in a forge."""

import os
import subprocess
import sys
import time
from pathlib import Path

prompt = sys.stdin.read()
assert "## General vault rules" in prompt and "## This vault's own rules" in prompt, prompt[:200]
mode = "audit" if prompt.startswith("# Audit the vault") else sys.argv[1]
branch = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True, check=True).stdout.strip()
here = Path.cwd()
assert os.environ["WIKISMITH_VAULT"] == str(here)  # `wikismith check` must look at the worktree


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def commit(message: str) -> None:
    git("add", "-A")
    git("commit", "-q", "-m", message)  # identity comes from the repo, or the job's fallback


def append(rel: str, text: str) -> None:
    path = here / rel
    path.write_text(path.read_text() + text)


# The real agent's skills call `wikismith`; the job's PATH must provide it.
subprocess.run(["wikismith", "--version"], check=True, capture_output=True)

if mode == "slow":
    time.sleep(1.5)
    mode = "additive"

if mode == "fail":
    print("something broke", file=sys.stderr)
    sys.exit(3)
elif mode == "nothing":
    print("Nothing to forge.")
elif mode == "audit":
    (here / "reports").mkdir(exist_ok=True)
    (here / "reports/audit-2026-02-01.md").write_text("# Audit\n\nAll good.\n")
    commit("Audit 2026-02-01")
    print(f"## Audit 2026-02-01: ready to merge\n\nAll good. ({branch})")
else:
    if mode == "delete":
        (here / "wiki/kitchen/knife-care.md").unlink()
    elif mode == "raw":
        append("raw/notes/2026-01-01_tomato-thread.md", "edited\n")
    elif mode == "flagged":
        append("wiki/garden/my-garden.md", "\nA fourth bed is planned.\n")
    elif mode == "outside":
        append("CLAUDE.md", "\nNew rule.\n")
    append("wiki/garden/tomatoes.md", "\nStart pepper seeds eight weeks before frost.\n")
    append("wiki/_log.md", "- raw/captures/2026-01-06_pepper-seeds.md — 2026-02-01 — garden\n")
    commit("Forge 1 raw file(s): garden")
    verdict = "waiting for review" if mode == "waiting" else "ready to merge"
    print(f"## Forge {branch}: {verdict}\n\n### Added\n- wiki/garden/tomatoes.md: pepper seed timing")
    print(f"\n(TZ={os.environ.get('TZ', '')})")  # so tests can check the job passes the vault's time zone
    if mode == "questions":
        print("\n### Questions\n- Is the cat's vet in town? — files: raw/notes/2026-01-05_router-setup.md")
        print("- New area `pets` for the cat? [new area: pets] — files: raw/notes/2026-01-05_router-setup.md")
