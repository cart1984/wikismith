"""mcpedia: the MCP server. A thin layer that exposes the vault's tools to MCP clients."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from functools import wraps

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from wikismith import __version__
from wikismith.captures import Captures, InvalidCapture
from wikismith.checks import CHECKS, Checker, CheckName, format_report
from wikismith.config import CONFIG_FILE
from wikismith.gitops import Git, GitError, check_remote_policy
from wikismith.httpserve import HEALTH
from wikismith.instructions import asset, build_instructions
from wikismith.instructions import vault_guide as guide
from wikismith.jobs import JobError, JobKind, JobRunner
from wikismith.pages import Ambiguous, NotFound
from wikismith.questions import ANSWER_TITLE, QuestionError, QuestionQueue, answer_capture
from wikismith.reader import Reader
from wikismith.search import Embedder, FastEmbedder, SearchIndex
from wikismith.secretscan import Scanner, SecretsBlocked
from wikismith.vault import AccessDenied, Vault, model_dir

SERVER_NAME = "mcpedia"

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
CAPTURE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
JOB = CAPTURE
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)
MAX_WAIT_SECONDS = 120

# Failures an agent can act on (wrong name, no access). Anything else is a crash.
_EXPECTED = (AccessDenied, NotFound, Ambiguous, InvalidCapture, SecretsBlocked, JobError, GitError, QuestionError)


def _expected_errors[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except _EXPECTED as e:
            raise ToolError(str(e)) from e

    return wrapper


def make_embedder(vault: Vault) -> Embedder | None:
    search = vault.config.search
    if not search.semantic:
        return None
    return FastEmbedder(search.model, model_dir(search.cache_dir))


def health(vault: Vault, index: SearchIndex) -> dict[str, str]:
    """What /healthz reports: states only, never paths or content (the endpoint has no token)."""
    readable = vault.root.is_dir() and (vault.root / CONFIG_FILE).is_file() and os.access(vault.root, os.R_OK)
    state = {"vault": "readable" if readable else "unreadable", "index": index.status}
    state["status"] = "ok" if readable and index.status != "failed" else "failing"
    return state


def build_server(vault: Vault, embedder: Embedder | None = None, warm: bool = True) -> MCPServer:
    """Build mcpedia for one vault. Pass `make_embedder(vault)` for semantic search. The search index is built in the
    background right away; `warm=False` waits for the first search instead (for tests)."""
    check_remote_policy(Git(vault.root, vault.config), vault.config)
    secrets = vault.config.secrets
    scanner = Scanner.for_config(secrets)
    captures = Captures(vault, scanner)
    mask = scanner.redact if secrets.mask_reads else None
    index_mask = scanner.placeholder if secrets.mask_reads else None
    reader = Reader(vault, SearchIndex(vault, embedder, index_mask), mask)
    mask_text: Callable[[str], str] = reader.mask
    if warm:
        threading.Thread(target=reader.index.refresh, name="warm-index", daemon=True).start()
    server = MCPServer(name=SERVER_NAME, version=__version__, instructions=build_instructions(vault, reader.mask))
    HEALTH[server] = lambda: health(vault, reader.index)

    if secrets.policy == "allow" and secrets.mask_reads:
        unmasked = Reader(vault, reader.index)

        @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
        @_expected_errors
        def reveal(page: str) -> str:
            """Read a page WITHOUT masking secrets. Only when the user explicitly asks for a specific secret value;
            prefer `read_page`. Never copy revealed values into code, commits, logs or other files."""
            return unmasked.read_page(page)

    @server.prompt(title="Forge new material")
    def forge() -> str:
        """Forge the vault's new material into the wiki: show waiting branches, run a forge job, show its summary, and
        save answers to its questions."""
        return asset("prompts/forge.md")

    @server.prompt(title="Audit the vault")
    def audit() -> str:
        """Run an audit job and show its report's summary."""
        return asset("prompts/audit.md")

    @server.prompt(title="Capture into the vault")
    def capture() -> str:
        """Save something from this conversation into the vault as a new capture."""
        return asset("prompts/capture.md")

    @server.tool(annotations=READ_ONLY)
    def vault_guide(include_conventions: bool = False) -> str:
        """How to use this vault, with the owner's full rules. Call it once before your first capture, or first if you
        haven't seen the server's instructions. `include_conventions` adds the general vault conventions (layout, pages,
        citations) that forge jobs follow."""
        return guide(vault, reader.mask, include_conventions)

    @server.tool(annotations=READ_ONLY)
    @_expected_errors
    def search(query: str, top_k: int = 5, area: str | None = None, include_archive: bool = False) -> str:
        """Search the wiki by meaning and keywords. Returns the best-matching pages with a snippet each; read a page
        with `read_page` for the full text. Results marked as unforged captures are newer notes not yet merged into
        the wiki. `area` limits results to one area. `include_archive` adds archived (outdated) pages, for questions
        about the past."""
        return reader.search(query, top_k, area, include_archive)

    @server.tool(annotations=READ_ONLY)
    @_expected_errors
    def read_page(page: str) -> str:
        """Read one wiki page. `page` is a name (`tomatoes`), an area path (`garden/tomatoes`) or a vault path
        (`wiki/garden/tomatoes.md`, `raw/captures/2026-01-06_pepper-seeds.md`). Archived pages are marked as
        outdated."""
        return reader.read_page(page)

    @server.tool(annotations=READ_ONLY)
    @_expected_errors
    def list_areas() -> str:
        """List the wiki's areas, each with its page count and what it covers."""
        return reader.list_areas()

    @server.tool(annotations=READ_ONLY)
    @_expected_errors
    def list_area(area: str) -> str:
        """Show an area's index page and all pages in it."""
        return reader.list_area(area)

    checker = Checker(vault, scanner)
    jobs = JobRunner(vault)
    questions = QuestionQueue(vault)
    if warm:  # clean up after jobs a crash or reboot interrupted
        threading.Thread(target=jobs.recover, name="recover-jobs", daemon=True).start()

    def start(kind: JobKind) -> str:
        job, queued = jobs.start(kind)
        if queued:
            return f"Job {job.id} ({job.kind}) is still running; a {kind} will start when it's done."
        return f"Started job {job.id} on branch {job.branch}. Check with job_status (it can wait)."

    @server.tool(annotations=READ_ONLY)
    def check_vault(checks: list[CheckName] | None = None) -> str:
        """Mechanical checks: broken links and links into the archive, orphan pages, frontmatter problems, pages
        missing from their area index, page names used twice, unforged raw files, secrets in the wiki (values never
        shown), and files over the vault's size limits. Runs all of these unless `checks` names some. `similar` (only
        when named) lists pairs of pages that share much of their text: possible duplicates to judge."""
        return format_report(checker.run(tuple(checks) if checks else CHECKS))

    @server.tool(annotations=CAPTURE)
    @_expected_errors
    def add_capture(title: str, content: str, source: str | None = None, forge_now: bool = False) -> str:
        """Save a new note into the vault's inbox (`raw/captures/`); it's merged into the wiki at the next forge. Only
        when the user asks or agrees. One topic per capture, self-contained: what, when, decisions and why, open
        questions. `source` says where it came from (e.g. "chat about router setup in repo X"). Never include secrets.
        `forge_now` starts a forge right away (on the server) instead of waiting for the next one."""
        result = captures.add(title, content, source)
        out = result.summary()
        if forge_now:
            out += "\n" + (start("forge") if result.committed else "Not forging: the capture isn't committed.")
        return out

    @server.tool(annotations=JOB)
    @_expected_errors
    def run_forge() -> str:
        """Forge new raw material into the wiki, as a job on the server (an agent working on a branch). Returns a job
        id at once; use job_status to wait for the summary. Changes that only add things are merged automatically; the
        rest waits for the owner (list_pending)."""
        return start("forge")

    @server.tool(annotations=JOB)
    @_expected_errors
    def run_audit() -> str:
        """Run a vault audit as a job on the server; it writes a report to reports/. Returns a job id at once;
        use job_status to wait for the report's summary."""
        return start("audit")

    @server.tool(annotations=READ_ONLY)
    @_expected_errors
    def job_status(job_id: str | None = None, wait_seconds: int = 0) -> str:
        """Status of a job (default: the latest). `wait_seconds` (up to 120) waits for it to finish first. A finished
        forge shows its summary; show that to the user."""
        job = jobs.wait(jobs.get(job_id), max(0, min(wait_seconds, MAX_WAIT_SECONDS)))
        return mask_text(jobs.report(job))

    @server.tool(annotations=READ_ONLY)
    @_expected_errors
    def list_pending() -> str:
        """What waits for the owner: open questions from forges (answer them with answer_questions), then forge and
        audit branches waiting for review, with what couldn't be verified."""
        parts = []
        if open_questions := questions.open():
            lines = "\n".join(q.line() for q in open_questions)
            parts.append(f"Open questions ({len(open_questions)}):\n{lines}")
        parts += [mask_text(summary) or f"{branch} (no summary saved)" for branch, summary in jobs.pending()]
        return "\n\n---\n\n".join(mask_text(p) for p in parts) if parts else "Nothing is waiting for the owner."

    @server.tool(annotations=CAPTURE)
    @_expected_errors
    def answer_questions(answers: dict[str, str], approve_areas: list[str] | None = None) -> str:
        """Save the owner's answers to open forge questions (`answers`: question id -> answer, in the owner's words)
        as one capture, and start a forge that uses them. `approve_areas`: the proposed new areas the owner
        explicitly agreed to; only those may be created. Only with the owner's answers, never your own guesses."""
        open_by_id = {q.id: q for q in questions.open()}
        approve = sorted(set(approve_areas or []))
        proposed = {q.area for i, q in open_by_id.items() if i in answers and q.area}
        if not answers:
            raise QuestionError("No answers given.")
        if unknown := [a for a in approve if a not in proposed]:
            raise QuestionError(f"Not proposed in the answered questions: {', '.join(unknown)}.")
        if missing := [i for i in answers if i not in open_by_id]:
            raise QuestionError(f"No open question {', '.join(missing)}. Open: {', '.join(open_by_id) or 'none'}.")
        content, frontmatter = answer_capture([open_by_id[i] for i in answers], answers, approve)
        result = captures.add(ANSWER_TITLE, content, "the owner, answering forge questions", frontmatter)
        if not result.committed:
            return result.summary() + "\nThe questions stay open until the answers are committed."
        questions.take(list(answers))
        return result.summary() + "\n" + start("forge")

    @server.tool(annotations=DESTRUCTIVE)
    @_expected_errors
    def merge_forge(branch: str) -> str:
        """Merge a reviewed forge or audit branch into the wiki. Only after the owner has read its summary and
        said to merge it. Never merges a branch that changes raw/."""
        notes = jobs.merge(branch)
        return "\n".join([f"Merged {branch}.", *notes])

    @server.tool(annotations=DESTRUCTIVE)
    @_expected_errors
    def discard_forge(branch: str) -> str:
        """Throw away a forge or audit branch without merging. Only when the owner says so; its raw files stay
        unforged and are picked up by the next forge."""
        jobs.discard(branch)
        return f"Discarded {branch}."

    return server
