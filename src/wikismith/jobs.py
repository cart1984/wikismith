"""Forge and audit jobs: an agent working in its own git worktree, merged by the server.

The agent (headless Claude Code by default) never touches the vault's main clone. Each job gets a worktree on a fresh
branch, outside the vault, so captures keep committing to the main branch while it runs. When it's done, the server
decides whether to merge: only if the agent says it's ready, `review.auto_merge` allows it, and the server can verify
every change (`merges.py`). Everything else waits for the owner (`merge_forge`).

Jobs run in a process of their own (`python -m wikismith.jobs`), detached from the mcpedia that started them, so
closing a Claude Code session doesn't kill a job halfway. Their state is a JSON file in the vault's cache folder, so
every mcpedia of the vault sees it. One job at a time per vault: the job process holds a lock file in `.git/` while
it runs, and runs a queued job next before it lets go. Times are UTC; dates in the vault follow `timezone`.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, get_args

from wikismith.checks import Checker
from wikismith.config import JobKind
from wikismith.gitops import (
    FALLBACK_EMAIL,
    FALLBACK_NAME,
    NO_GIT,
    Git,
    GitError,
    git_dir,
    held,
    repo_lock,
    try_flock,
    with_wikismith_on_path,
)
from wikismith.instructions import job_prompt
from wikismith.logfile import logged_in
from wikismith.merges import REVIEW_HEADING, CheckIssues, review, review_section
from wikismith.questions import QuestionQueue, parse_questions, summary_counts
from wikismith.search import SHARED_EMBEDDINGS_ENV
from wikismith.secretscan import Scanner
from wikismith.vault import Vault

JobState = Literal["running", "merged", "waiting", "nothing", "failed"]
BRANCH_PREFIX: dict[JobKind, str] = {"forge": "forge/", "audit": "audit/"}
READY = re.compile(r"^##\s+\S.*:\s*ready to merge\s*$", re.IGNORECASE | re.MULTILINE)
KEEP_DAYS = 30  # finished jobs' state files


class JobError(RuntimeError):
    pass


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class Job:
    id: str
    kind: JobKind
    branch: str
    started: dt.datetime
    state: JobState = "running"
    finished: dt.datetime | None = None
    summary: str = ""
    notes: list[str] = field(default_factory=list)
    pid: int | None = None
    changes: str = ""  # a waiting branch: the diff of each file that needs the owner's OK

    def report(self, now: dt.datetime | None = None) -> str:
        head = f"Job {self.id} ({self.kind}, branch {self.branch}): {self.state}"
        if self.state == "running":
            minutes = int(((now or utcnow()) - self.started).total_seconds() // 60)
            return f"{head}, started {minutes} min ago. Check again with job_status."
        parts = [head]
        parts += [f"Note: {n}" for n in self.notes]
        if self.summary:
            # A waiting branch shows only what the owner has to look at: the additions are verified already.
            parts.append(_without_section(self.summary, "Added") if self.state == "waiting" else self.summary)
        if self.state == "waiting" and self.changes:
            parts.append(self.changes)
        return "\n\n".join(parts)

    def to_json(self) -> str:
        data = asdict(self)
        data["started"] = self.started.isoformat()
        data["finished"] = self.finished.isoformat() if self.finished else None
        return json.dumps(data, indent=2)

    @classmethod
    def from_json(cls, text: str) -> Job:
        data = json.loads(text)
        data["started"] = dt.datetime.fromisoformat(data["started"])
        data["finished"] = dt.datetime.fromisoformat(data["finished"]) if data["finished"] else None
        if data["kind"] not in get_args(JobKind):
            raise ValueError(f"unknown job kind {data['kind']!r}")
        return cls(**data)


class JobRunner:
    def __init__(self, vault: Vault, now: Callable[[], dt.datetime] = utcnow) -> None:
        self.vault = vault
        self.git = Git(vault.root, vault.config)
        self.now = now
        self.workdir = vault.cache_dir / "jobs"

    # -- files ------------------------------------------------------------------

    def _state_file(self, job_id: str) -> Path:
        return self.workdir / "state" / f"{job_id}.json"

    @property
    def _queue_file(self) -> Path:
        return self.workdir / "queued"

    def _job_lock(self) -> int | None:
        """The vault's job lock, if no job is running (in any process)."""
        folder = git_dir(self.vault.root)
        if folder is None:
            raise JobError(NO_GIT)
        return try_flock(folder / "wikismith-job.lock")

    def save(self, job: Job) -> None:
        path = self._state_file(job.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(job.to_json(), encoding="utf-8")
        tmp.replace(path)  # readers never see half a file

    def jobs(self) -> list[Job]:
        """All known jobs, oldest first."""
        out = []
        for path in (self.workdir / "state").glob("*.json"):
            with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
                out.append(Job.from_json(path.read_text(encoding="utf-8")))
        return sorted(out, key=lambda j: j.started)

    # -- starting and waiting -------------------------------------------------

    def start(self, kind: JobKind) -> tuple[Job, bool]:
        """Start a job in its own process. If one is running, queue this one behind it: `(running job, True)`."""
        self._check_repo()
        fd = self._job_lock()
        if fd is None:
            self._queue_file.parent.mkdir(parents=True, exist_ok=True)
            self._queue_file.write_text(kind, encoding="utf-8")
            fd = self._job_lock()  # the running job may have finished just now, without seeing the queue
            if fd is None:
                running = self.running
                if running is None:
                    raise JobError("Another job is just starting; try again in a moment.")
                return running, True
            self._queue_file.unlink(missing_ok=True)
        with held(fd):
            job = self._new_job(kind)
            log = (self.workdir / "logs" / f"{job.id}.log").resolve()
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("ab") as out:
                # The child inherits the locked descriptor, so the lock lasts as long as the job process.
                process = subprocess.Popen(
                    [sys.executable, "-m", "wikismith.jobs", str(self.vault.root), job.id, str(fd)],
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    pass_fds=(fd,),
                    start_new_session=True,  # survives the session that started it
                )
            job.pid = process.pid
            self.save(job)
        return job, False

    def run(self, kind: JobKind) -> Job:
        """Run a job in the foreground (for the CLI and cron), then any job queued meanwhile."""
        self._check_repo()
        fd = self._job_lock()
        if fd is None:
            running = self.running
            raise JobError(f"Job {running.id if running else '?'} is still running.")
        with held(fd):
            job = self._new_job(kind)
            job.pid = os.getpid()
            self.save(job)
            self.serve(job)
        return job

    def serve(self, job: Job) -> None:
        """Run a job, then whatever was queued meanwhile. The caller holds the job lock."""
        self._execute(job)
        while self._queue_file.is_file():
            kind = self._queue_file.read_text(encoding="utf-8").strip()
            self._queue_file.unlink(missing_ok=True)
            if kind in get_args(JobKind):
                queued = self._new_job(kind)  # type: ignore[arg-type]
                queued.pid = os.getpid()
                self.save(queued)
                self._execute(queued)

    @property
    def running(self) -> Job | None:
        return next((j for j in reversed(self.jobs()) if j.state == "running"), None)

    def get(self, job_id: str | None) -> Job:
        jobs = self.jobs()
        if job_id is None:
            if not jobs:
                raise JobError("No jobs have run yet.")
            return jobs[-1]
        found = next((j for j in jobs if j.id == job_id), None)
        if found is None:
            raise JobError(f"No job {job_id!r} (finished jobs are kept for {KEEP_DAYS} days; see list_pending).")
        return found

    def wait(self, job: Job, seconds: float) -> Job:
        """The job's latest state, waiting up to `seconds` for it to finish."""
        deadline = time.monotonic() + seconds
        while True:
            job = self.get(job.id)
            if job.state != "running" or time.monotonic() >= deadline:
                return job
            time.sleep(0.2)

    def recover(self) -> list[str]:
        """Clean up after jobs whose process died (a crash, a reboot): mark them failed, remove their worktrees and
        branches. Only when no job is running; mcpedia calls this at startup."""
        if not self.git.is_repo():
            return []
        fd = self._job_lock()
        if fd is None:
            return []
        notes = []
        with held(fd):
            trees = self.workdir / "trees"
            if trees.is_dir():  # first: git won't delete a branch a worktree has checked out
                for tree in trees.iterdir():
                    self._remove_worktree(tree)
            for job in self.jobs():
                if job.state == "running":
                    job.state, job.finished = "failed", self.now()
                    job.notes.append("Interrupted: the process running it stopped before it finished.")
                    self.save(job)
                    self._delete_branch(job.branch)
                    notes.append(f"job {job.id} was interrupted; cleaned up its branch {job.branch}")
                elif job.finished and (self.now() - job.finished).days > KEEP_DAYS:
                    self._state_file(job.id).unlink(missing_ok=True)
                    (self.workdir / "logs" / f"{job.id}.log").unlink(missing_ok=True)
            self._queue_file.unlink(missing_ok=True)
        return notes

    def _check_repo(self) -> None:
        if not self.git.is_repo():
            raise JobError(NO_GIT)

    def _new_job(self, kind: JobKind) -> Job:
        now, job_id = self.now(), uuid.uuid4().hex[:8]
        branch = f"{BRANCH_PREFIX[kind]}{now:%Y-%m-%d-%H%M}Z-{job_id[:4]}"
        job = Job(job_id, kind, branch, now)
        self.save(job)
        return job

    # -- doing the work ---------------------------------------------------------

    def _execute(self, job: Job) -> None:
        tree = self.workdir / "trees" / job.id
        try:
            self._add_worktree(job, tree)
            before = self._issues(tree)
            job.summary = self._run_agent(job, tree)
            self._finish(job, before, self._issues(tree))
        except (JobError, GitError, OSError) as e:
            job.state = "failed"
            job.notes.append(str(e))
        finally:
            self._remove_worktree(tree)
            if job.state in ("nothing", "merged", "failed"):
                self._delete_branch(job.branch)
            self._queue_questions(job)
            job.finished = self.now()
            self._save_summary(job)
            self.save(job)
            if job.state == "waiting" or any(n.startswith("Questions for the owner") for n in job.notes):
                self.notify()

    def _add_worktree(self, job: Job, tree: Path) -> None:
        main = self.vault.config.git.branch
        tree.parent.mkdir(parents=True, exist_ok=True)
        with repo_lock(self.vault.root):
            if problem := self.git.pull():  # start from the latest main, not a stale local copy
                job.notes.append(f"{problem}; working from the local state.")
            self.git.run("worktree", "add", "-q", "-b", job.branch, str(tree), main)

    def _run_agent(self, job: Job, tree: Path) -> str:
        jobs = self.vault.config.jobs
        command = list(jobs.agent)
        # `wikismith check` and `scan` in the worktree, not in the vault the server was started with.
        env = with_wikismith_on_path({**os.environ, "WIKISMITH_VAULT": str(tree)})
        # `wikismith search` in the worktree reuses the vault's embeddings instead of re-embedding everything.
        env[SHARED_EMBEDDINGS_ENV] = str(self.vault.cache_dir)
        if not self.git.has_identity():  # e.g. in a container without the owner's ~/.gitconfig
            env.setdefault("GIT_AUTHOR_NAME", FALLBACK_NAME)
            env.setdefault("GIT_AUTHOR_EMAIL", FALLBACK_EMAIL)
            env.setdefault("GIT_COMMITTER_NAME", FALLBACK_NAME)
            env.setdefault("GIT_COMMITTER_EMAIL", FALLBACK_EMAIL)
        if self.vault.config.timezone:  # the agent's "today" for `updated:`, log lines and report names
            env["TZ"] = self.vault.config.timezone
        try:
            out = subprocess.run(
                command,
                cwd=tree,
                env=env,
                input=job_prompt(self.vault, job.kind),
                capture_output=True,
                text=True,
                timeout=jobs.timeout_minutes * 60,
            )
        except FileNotFoundError as e:
            raise JobError(f"Can't run {command[0]!r}: not installed here. Set jobs.agent in wikismith.yaml.") from e
        except subprocess.TimeoutExpired as e:
            raise JobError(f"The job took longer than {jobs.timeout_minutes} minutes and was stopped.") from e
        if out.returncode != 0:
            detail = (out.stderr or out.stdout).strip().splitlines()[-5:]
            hint = ""
            if "log" in " ".join(detail).lower() and "claude" in command[0]:
                hint = " Log in with `claude`, or set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`)."
            raise JobError(f"The agent exited with code {out.returncode}: " + " / ".join(detail) + hint)
        return out.stdout.strip()

    def _issues(self, tree: Path) -> CheckIssues:
        """The worktree's check issues, to tell whether the agent's link changes fixed anything."""
        checker = Checker(Vault(tree, self.vault.config), Scanner.for_config(self.vault.config.secrets))
        results = checker.run(("links", "orphans", "frontmatter", "index", "names"))
        return frozenset((issue.path, issue.message) for issues in results.values() for issue in issues)

    def _finish(self, job: Job, before: CheckIssues, after: CheckIssues) -> None:
        main = self.vault.config.git.branch
        if self.git.run("rev-list", "--count", f"{main}..{job.branch}") == "0":
            job.state = "nothing"
            return
        verdict = review(self.vault, job.branch, job.kind, before, after)
        reasons = verdict.refused + verdict.waits
        why_all = None  # why every file waits, when no single one does
        if job.kind == "forge":
            if self.vault.config.review.auto_merge == "none":
                reasons.insert(0, "review.auto_merge is 'none'")
                why_all = "auto-merge is off"
            elif not READY.search(job.summary):
                reasons.insert(0, "the agent didn't mark it ready to merge")
                why_all = "not marked ready to merge"
        if verdict.verified:
            job.notes.append("Verified: " + "; ".join(verdict.verified) + ".")
        if reasons:
            job.state = "waiting"
            job.notes.insert(0, "Waiting for review: " + "; ".join(reasons) + ".")
            job.changes = review_section(self.vault, job.branch, verdict.blocking, why_all)
        else:
            job.notes += self.merge(job.branch, allow_review=False)
            job.state = "merged"

    def _queue_questions(self, job: Job) -> None:
        """A forge's questions go into the owner's queue; they don't hold up the rest of the forge."""
        if job.kind != "forge" or job.state == "failed":
            return
        asked = parse_questions(job.summary, job.id, self.vault.today())
        if asked:
            queue = QuestionQueue(self.vault)
            queue.add(asked)
            lines = "\n".join(q.line() for q in asked)
            job.notes.append(f"Questions for the owner (answer them with answer_questions):\n{lines}")

    def notify(self) -> None:
        """Run `jobs.notify` if something waits for the owner. Counts only; failures don't matter to the job."""
        command = self.vault.config.jobs.notify
        questions, waiting = len(QuestionQueue(self.vault).open()), len(self.pending())
        if not command or not (questions or waiting):
            return
        message = summary_counts(questions, waiting)
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run([part.replace("{message}", message) for part in command], capture_output=True, timeout=30)

    # -- merging ------------------------------------------------------------------

    def merge(self, branch: str, allow_review: bool = True) -> list[str]:
        """Merge a job branch into the main branch, push, and delete the branch."""
        if not branch.startswith(tuple(BRANCH_PREFIX.values())):
            raise JobError(f"{branch!r} is not a forge or audit branch.")
        kind: JobKind = "audit" if branch.startswith(BRANCH_PREFIX["audit"]) else "forge"
        if done := self.superseded(branch):
            raise JobError(_superseded_note(done))
        verdict = review(self.vault, branch, kind)
        if verdict.refused:
            raise JobError("Refusing to merge: " + "; ".join(verdict.refused) + ".")
        if verdict.waits and not allow_review:
            raise JobError("Not mergeable without review: " + "; ".join(verdict.waits) + ".")
        main, notes = self.vault.config.git.branch, []
        with repo_lock(self.vault.root):
            if self.git.branch() != main:
                raise JobError(f"The vault's main clone is on {self.git.branch()!r}, not {main!r}.")
            layout = self.vault.config.layout
            dirty = self.git.run("status", "--porcelain", "--", layout.wiki, layout.archive, layout.reports)
            if dirty:
                files = ", ".join(line.split(maxsplit=1)[1] for line in dirty.splitlines()[:5])
                raise JobError(
                    f"The vault folder has uncommitted changes in {files}. Only mcpedia should write to the wiki; "
                    "commit or discard those changes, then merge again."
                )
            if problem := self.git.pull():  # so the push after the merge isn't rejected
                notes.append(f"{problem}; merging into the local state.")
            # Always a merge commit, so first-parent history tells forge changes from direct commits (audits list them).
            self.git.run("merge", "--quiet", "--no-ff", "-m", f"Merge {branch}", branch)
            if self.vault.config.git.sync and self.git.remote_url():
                try:
                    self.git.run("push", "--quiet", self.vault.config.git.remote, main, timeout=60)
                except GitError as e:
                    notes.append(f"Merged locally, but the push failed ({str(e).splitlines()[0]}).")
            self._delete_branch(branch)
        return notes

    def discard(self, branch: str) -> None:
        if not branch.startswith(tuple(BRANCH_PREFIX.values())):
            raise JobError(f"{branch!r} is not a forge or audit branch.")
        with repo_lock(self.vault.root):
            self.git.run("branch", "-D", branch)
        self._summary_file(branch).unlink(missing_ok=True)

    def pending(self) -> list[tuple[str, str]]:
        """Job branches waiting for the owner, with their saved summaries."""
        names = self.git.run("branch", "--list", "forge/*", "audit/*", "--format=%(refname:short)")
        running = {j.branch for j in self.jobs() if j.state == "running"}
        out = []
        for branch in names.splitlines():
            if branch and branch not in running:
                summary_file = self._summary_file(branch)
                summary = summary_file.read_text(encoding="utf-8") if summary_file.is_file() else ""
                if done := self.superseded(branch):
                    summary = f"{_superseded_note(done)}\n\n{summary}".strip()
                elif REVIEW_HEADING not in summary:
                    summary = f"{summary}\n\n{self._changes(branch)}".strip()
                out.append((branch, summary))
        return out

    def report(self, job: Job) -> str:
        """`job.report()`, with the changes to review worked out from the branch for a job that finished without."""
        if job.state == "waiting" and not job.changes:
            job.changes = self._changes(job.branch)
        return job.report()

    def _changes(self, branch: str) -> str:
        """The changes that need the owner's OK, from the branch itself (without the job's before-and-after checks)."""
        kind: JobKind = "audit" if branch.startswith(BRANCH_PREFIX["audit"]) else "forge"
        try:
            return review_section(self.vault, branch, review(self.vault, branch, kind).blocking, "waiting for review")
        except GitError:
            return ""

    def superseded(self, branch: str) -> list[str]:
        """Raw files this branch forges that the main branch has forged since, on another branch."""
        main, log = self.vault.config.git.branch, self.vault.config.layout.log
        base = self.git.run("merge-base", main, branch)

        def logged(rev: str) -> set[str]:
            try:
                return logged_in(self.git.run("show", f"{rev}:{log}"))
            except GitError:
                return set()

        before = logged(base)
        return sorted((logged(branch) - before) & (logged(main) - before))

    # -- housekeeping ---------------------------------------------------------------

    def _remove_worktree(self, tree: Path) -> None:
        with repo_lock(self.vault.root):
            try:
                self.git.run("worktree", "remove", "--force", str(tree))
            except GitError:
                shutil.rmtree(tree, ignore_errors=True)
                self.git.run("worktree", "prune")

    def _delete_branch(self, branch: str) -> None:
        with contextlib.suppress(GitError):
            self.git.run("branch", "-D", branch)
        self._summary_file(branch).unlink(missing_ok=True)

    def _summary_file(self, branch: str) -> Path:
        return self.workdir / "summaries" / (branch.replace("/", "_") + ".md")

    def _save_summary(self, job: Job) -> None:
        if job.state == "waiting":
            path = self._summary_file(job.branch)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(job.report(), encoding="utf-8")


def _within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def _superseded_note(files: list[str]) -> str:
    shown = ", ".join(files[:3]) + (f" and {len(files) - 3} more" if len(files) > 3 else "")
    return (
        f"Superseded: a newer forge has already forged {shown}, so merging this branch would forge them twice. "
        "Discard it (discard_forge); whatever else it covered is still unforged and goes into the next forge."
    )


def _without_section(summary: str, title: str) -> str:
    """The summary without one `### <title>` section."""
    return re.sub(rf"^###\s+{title}\s*$.*?(?=^#{{1,3}}\s|\Z)", "", summary, flags=re.MULTILINE | re.DOTALL).strip()


def main(argv: list[str]) -> int:
    """The job process: `python -m wikismith.jobs <vault> <job id> <lock fd>`, started by `JobRunner.start`."""
    root, job_id, fd = Path(argv[0]), argv[1], int(argv[2])
    runner = JobRunner(Vault.open(root))
    job = runner.get(job_id)
    with held(fd):
        job.pid = os.getpid()
        runner.save(job)
        try:
            runner.serve(job)
        except Exception as e:  # never leave a job marked running
            job = runner.get(job_id)
            if job.state == "running":
                job.state, job.finished = "failed", runner.now()
                job.notes.append(f"The job process failed: {e}")
                runner.save(job)
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
