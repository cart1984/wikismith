"""Command line entry point: `wikismith <command>`."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import get_args

from wikismith import __version__
from wikismith.config import CONFIG_FILE, ConfigError, JobKind, Search, parse_config
from wikismith.secretscan import Scanner
from wikismith.vault import Vault, default_cache_dir


def _vault(args: argparse.Namespace) -> Vault:
    root = Path(args.vault or os.environ.get("WIKISMITH_VAULT") or Path.cwd())
    return Vault.open(root)


def cmd_serve(args: argparse.Namespace) -> int:
    from wikismith.server import build_server, make_embedder

    vault = _vault(args)
    server = build_server(vault, make_embedder(vault))
    if args.transport == "stdio":
        server.run("stdio")
        return 0
    from wikismith.httpserve import check_token, serve_http

    token = os.environ.get("WIKISMITH_TOKEN") or None
    check_token(args.host, token, args.no_auth)
    serve_http(server, args.host, args.port, token)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from wikismith.checks import CHECKS, Checker, format_report

    vault = _vault(args)
    results = Checker(vault, Scanner.for_config(vault.config.secrets)).run(tuple(args.check or CHECKS))
    print(format_report(results, limit=args.limit))
    return 1 if any(results.values()) else 0


def cmd_scan(args: argparse.Namespace) -> int:
    """Scan files (or the staged changes) for secrets and personal data. Exit 1 if any are found, except personal
    data under `secrets.pii: warn`, which is only reported."""
    from wikismith.precommit import scan, scan_staged

    root = Path(args.vault or os.environ.get("WIKISMITH_VAULT") or Path.cwd())
    scanner = Scanner.for_config(Vault.open(root).config.secrets) if (root / CONFIG_FILE).is_file() else Scanner()
    if args.staged:
        return scan_staged(root, scanner)
    return scan(
        root, scanner, [(f, lambda f=f: Path(f).read_text(encoding="utf-8", errors="ignore")) for f in args.files]
    )


def cmd_precommit(args: argparse.Namespace) -> int:
    """What the git hook runs: shrink or ask about large files, then scan for secrets."""
    from wikismith.precommit import run, tty_ask

    return run(Path(args.vault or Path.cwd()), tty_ask())


def cmd_import(args: argparse.Namespace) -> int:
    """Copy files into raw/ word for word, run the hook's checks and commit them as one commit."""
    from wikismith.gitops import GitError
    from wikismith.importer import ImportRefused, import_files
    from wikismith.precommit import tty_ask

    vault = _vault(args)
    try:
        result = import_files(vault, [Path(f) for f in args.files], args.to, tty_ask())
    except (ImportRefused, GitError) as e:
        print(f"wikismith import: {e}", file=sys.stderr)
        return 1
    n = len(result.files)
    print(f"Committed {n} file{'' if n == 1 else 's'} as {result.commit}{', pushed' if result.pushed else ''}:")
    for name in result.files:
        print(f"  {name}")
    for name in result.left_out:
        print(f"  left out (removed again): {name}")
    for warning in result.warnings:
        print(warning, file=sys.stderr)
    print("The next forge picks them up (or start one now: `wikismith run forge`, or run_forge in mcpedia).")
    return 0


def cmd_area_add(args: argparse.Namespace) -> int:
    """Approve new areas: a capture with `approves_areas`, committed; the next forge creates them."""
    from wikismith.areas import InvalidArea, approve_areas
    from wikismith.captures import InvalidCapture
    from wikismith.gitops import GitError

    pairs = args.areas
    if len(pairs) % 2:
        print("wikismith area add: give each area a name and a description: <name> <description> ...", file=sys.stderr)
        return 2
    areas = dict(zip(pairs[::2], pairs[1::2], strict=True))
    if len(areas) * 2 != len(pairs):
        print("wikismith area add: an area is named twice", file=sys.stderr)
        return 2
    try:
        result = approve_areas(_vault(args), areas)
    except (InvalidArea, InvalidCapture, GitError) as e:
        print(f"wikismith area add: {e}", file=sys.stderr)
        return 1
    print(result.summary())
    print("The next forge creates the area folders (or start one now: `wikismith run forge`, or run_forge in mcpedia).")
    return 0


def cmd_shrink(args: argparse.Namespace) -> int:
    """Shrink images in place, keeping the originals in the vault's cache folder for 30 days."""
    from wikismith.media import MediaError, shrink

    root = Path(args.vault or os.environ.get("WIKISMITH_VAULT") or Path.cwd())
    vault = Vault.open(root) if (root / CONFIG_FILE).is_file() else None
    limits = vault.config.files if vault else parse_config({"name": "x"}).files
    originals = (vault.cache_dir if vault else default_cache_dir(None)) / "originals"
    failed = 0
    for name in args.files:
        try:
            print(shrink(Path(name), limits, originals).describe(root))
        except (MediaError, OSError) as e:
            print(f"{name}: {e}", file=sys.stderr)
            failed += 1
    if not failed:
        print(f"originals kept in {originals} for 30 days")
    return 1 if failed else 0


def cmd_search(args: argparse.Namespace) -> int:
    from wikismith.reader import Reader
    from wikismith.search import SearchIndex
    from wikismith.server import make_embedder

    vault = _vault(args)
    scanner = Scanner.for_config(vault.config.secrets)
    masked = vault.config.secrets.mask_reads
    index = SearchIndex(vault, make_embedder(vault), scanner.placeholder if masked else None)
    reader = Reader(vault, index, scanner.redact if masked else None)
    print(reader.search(" ".join(args.query), top_k=args.top_k, area=args.area))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    from wikismith.scaffold import init

    root = Path(args.path)
    git = args.git if args.git is not None else _ask_git(root)
    hook = args.hook if args.hook is not None else _ask_hook()
    for line in init(root, args.name, hook=hook, git=git):
        print(line)
    print(
        f"\nVault ready in {root}. Next: review wikismith.yaml and CLAUDE.md (this vault's own rules), "
        "then run `wikismith check`: it lists areas without a description."
    )
    return 0


def _ask_git(root: Path) -> bool:
    """Ask before setting up git, unless there's a repository already or nobody to ask (then: yes)."""
    from wikismith.scaffold import is_repo

    if is_repo(root) or not sys.stdin.isatty():
        return True
    answer = input("Set up git? It's needed for captures, forges and audits. [Y/n] ")
    return not answer.strip().lower().startswith("n")


def _ask_hook() -> bool:
    """Ask before changing the global git config; with nobody to ask, leave it alone."""
    if not sys.stdin.isatty():
        print("skipped the secret hook (no terminal to ask); add it later with `wikismith hook`")
        return False
    answer = input("Scan your own commits for secrets? This adds an includeIf to your global git config. [Y/n] ")
    return not answer.strip().lower().startswith("n")


def cmd_hook(args: argparse.Namespace) -> int:
    from wikismith.scaffold import install_hook, remove_hook

    root = _vault(args).root
    log = remove_hook(root) if args.remove else install_hook(root)
    for line in log or ["the secret hook is enabled for this vault"]:
        print(line)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run a forge or audit job in the foreground, e.g. nightly from cron."""
    from wikismith.jobs import JobRunner

    runner = JobRunner(_vault(args))
    for note in runner.recover():
        print(note)
    job = runner.run(args.job)
    print(job.report())
    return 1 if job.state == "failed" else 0


def cmd_fetch_model(args: argparse.Namespace) -> int:
    """Download the embedding model ahead of time (e.g. while building a container image)."""
    from wikismith.search import FastEmbedder
    from wikismith.vault import model_dir

    embedder = FastEmbedder(args.model, model_dir(None))
    dims = embedder.embed_passages(["warm up"]).shape[1]
    print(f"{args.model} ready ({dims} dimensions) in {model_dir(None)}")
    return 0


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wikismith", description="Forge a wiki from raw notes; serve it over MCP.")
    parser.add_argument("--version", action="version", version=f"wikismith {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    vault_help = "vault folder (default: $WIKISMITH_VAULT or the current folder)"

    serve = sub.add_parser("serve", help="run mcpedia, the MCP server")
    serve.add_argument("--vault", help=vault_help)
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1", help="HTTP bind address (default: loopback only)")
    serve.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("WIKISMITH_PORT") or 8765),
        help="default: $WIKISMITH_PORT or 8765",
    )
    serve.add_argument("--no-auth", action="store_true", help="allow HTTP without a token, on loopback only")
    serve.set_defaults(func=cmd_serve)

    check = sub.add_parser("check", help="run the mechanical vault checks")
    check.add_argument("--vault", help=vault_help)
    check.add_argument("--check", action="append", help="run only this check (repeatable)")
    check.add_argument("--limit", type=int, default=50, help="issues shown per check")
    check.set_defaults(func=cmd_check)

    scan = sub.add_parser("scan", help="scan files for secrets (for a pre-commit hook: --staged)")
    scan.add_argument("--vault", help=vault_help)
    scan.add_argument("--staged", action="store_true", help="scan the staged versions of changed files")
    scan.add_argument("files", nargs="*")
    scan.set_defaults(func=cmd_scan)

    pre = sub.add_parser("pre-commit", help="what the git hook runs: large files, then the secret scan")
    pre.add_argument("--vault", help="repository folder (default: the current folder)")
    pre.set_defaults(func=cmd_precommit)

    imp = sub.add_parser(
        "import", help="copy files into raw/ word for word and commit them (secret and large-file checks first)"
    )
    imp.add_argument("--vault", help=vault_help)
    imp.add_argument("--to", required=True, help="folder inside raw/, e.g. import/2026-11-work")
    imp.add_argument("files", nargs="+", help="files or folders (a folder is copied with its layout)")
    imp.set_defaults(func=cmd_import)

    area = sub.add_parser("area", help="manage areas: `area add` approves new ones")
    area_sub = area.add_subparsers(dest="area_command", metavar="<command>", required=True)
    area_add = area_sub.add_parser(
        "add", help="approve new areas without waiting for a forge to propose them; the next forge creates them"
    )
    area_add.add_argument("--vault", help=vault_help)
    area_add.add_argument(
        "areas", nargs="+", metavar="<name> <description>", help='e.g. home-network "Router, Wi-Fi and the NAS"'
    )
    area_add.set_defaults(func=cmd_area_add)

    shrink = sub.add_parser("shrink", help="shrink images to the vault's limits (originals kept for 30 days)")
    shrink.add_argument("--vault", help=vault_help)
    shrink.add_argument("files", nargs="+")
    shrink.set_defaults(func=cmd_shrink)

    search = sub.add_parser("search", help="search the wiki from the command line")
    search.add_argument("--vault", help=vault_help)
    search.add_argument("--top-k", type=int, default=5)
    search.add_argument("--area")
    search.add_argument("query", nargs="+")
    search.set_defaults(func=cmd_search)

    init = sub.add_parser("init", help="create a vault, or add what's missing to an existing one")
    init.add_argument("path", nargs="?", default=".")
    init.add_argument("--name", help="vault name (default: the folder name)")
    init.add_argument(
        "--hook",
        action=argparse.BooleanOptionalAction,
        help="scan your own commits in the vault for secrets (default: ask; see `wikismith hook`)",
    )
    init.add_argument(
        "--git",
        action=argparse.BooleanOptionalAction,
        help="set up a git repository if there isn't one (default: ask; without git, mcpedia is read-only)",
    )
    init.set_defaults(func=cmd_init)

    hook = sub.add_parser(
        "hook", help="check your own commits in the vault for secrets and large files (pre-commit hook)"
    )
    hook.add_argument("--vault", help=vault_help)
    hook.add_argument("--remove", action="store_true", help="stop scanning this vault's commits")
    hook.set_defaults(func=cmd_hook)

    run = sub.add_parser("run", help="run a forge or audit job now (e.g. from cron)")
    run.add_argument("job", choices=get_args(JobKind))
    run.add_argument("--vault", help=vault_help)
    run.set_defaults(func=cmd_run)

    fetch = sub.add_parser("fetch-model", help="download the embedding model ahead of time")
    fetch.add_argument("--model", default=Search().model)
    fetch.set_defaults(func=cmd_fetch_model)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except (ConfigError, ValueError) as e:
        print(f"wikismith: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
