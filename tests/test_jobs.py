import datetime as dt
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from mcp.client import Client

from conftest import git, make_vault
from wikismith.instructions import asset
from wikismith.jobs import JobError, JobRunner
from wikismith.server import build_server
from wikismith.vault import Vault

AGENT = str(Path(__file__).parent / "helpers" / "fake_agent.py")


def configure(repo: Path, mode: str = "additive", **config: object) -> Vault:
    """The test vault with a fake agent, also written to wikismith.yaml: job processes read it from there."""
    settings = {"name": "test-vault", "jobs": {"agent": [sys.executable, AGENT, mode]}, "search": {"semantic": False}}
    settings |= config
    (repo / "wikismith.yaml").write_text(yaml.safe_dump(settings))
    return Vault.open(repo)


def runner(repo: Path, mode: str = "additive", **config: object) -> JobRunner:
    return JobRunner(configure(repo, mode, **config))


def on_main(repo: Path, path: str) -> str:
    return git(repo, "show", f"main:{path}")


def test_additive_forge_is_merged_and_pushed(repo: Path, tmp_path: Path) -> None:
    job = runner(repo).run("forge")
    assert job.state == "merged", job.notes
    assert "pepper seeds eight weeks" in on_main(repo, "wiki/garden/tomatoes.md")
    assert "pepper seeds eight weeks" in (repo / "wiki/garden/tomatoes.md").read_text()  # main clone updated
    remote = tmp_path / "remote.git"
    assert git(remote, "log", "-1", "--format=%s", "main").startswith("Merge forge/")  # always a merge commit
    assert git(remote, "log", "-1", "--format=%s", "main^2") == "Forge 1 raw file(s): garden"
    assert "forge/" not in git(repo, "branch", "--list")  # branch cleaned up
    assert git(repo, "worktree", "list").count("\n") == 0  # worktree removed
    assert "ready to merge" in job.report()


@pytest.mark.parametrize(
    ("mode", "config", "reason"),
    [
        ("waiting", {}, "the agent didn't mark it ready to merge"),
        ("additive", {"review": {"auto_merge": "none"}}, "review.auto_merge is 'none'"),
        ("delete", {}, "wiki/kitchen/knife-care.md: 2 line(s) removed"),
        ("raw", {}, "in raw/, which must never be edited"),
        ("outside", {}, "it changes CLAUDE.md, outside wiki and archive"),
        ("flagged", {"review": {"flag": ["wiki/*/my-*.md"]}}, "wiki/garden/my-garden.md, which is flagged"),
    ],
)
def test_waits_for_review(repo: Path, mode: str, config: dict[str, object], reason: str) -> None:
    jobs = runner(repo, mode, **config)
    job = jobs.run("forge")
    assert job.state == "waiting"
    assert reason in job.notes[0]
    assert "pepper seeds eight weeks" not in on_main(repo, "wiki/garden/tomatoes.md")
    [(branch, summary)] = jobs.pending()
    assert branch == job.branch and reason in summary


def test_a_waiting_report_shows_the_changes_to_review(repo: Path) -> None:
    jobs = runner(repo, "flagged", review={"flag": ["wiki/*/my-*.md"]})
    job = jobs.run("forge")
    assert job.state == "waiting"
    assert "### Changes that need your OK" in job.report() and "+A fourth bed is planned." in job.report()
    [(_, summary)] = jobs.pending()
    assert "+A fourth bed is planned." in summary


def test_a_branch_not_marked_ready_shows_all_its_changes(repo: Path) -> None:
    """No single file keeps it from merging, so the owner sees the diff of every file."""
    job = runner(repo, "waiting").run("forge")
    assert job.state == "waiting"
    assert "### Changes that need your OK" in job.changes
    assert "wiki/garden/tomatoes.md (not marked ready to merge):" in job.changes
    assert "pepper seeds eight weeks" in job.changes


def test_an_older_waiting_job_gets_its_changes_from_the_branch(repo: Path) -> None:
    """A job that finished before the changes were saved with it: worked out from the branch when shown."""
    jobs = runner(repo, "flagged", review={"flag": ["wiki/*/my-*.md"]})
    job = jobs.run("forge")
    job.changes = ""
    jobs.save(job)
    jobs._summary_file(job.branch).write_text(job.report())  # as saved before
    assert "+A fourth bed is planned." in jobs.report(jobs.get(job.id))
    [(_, summary)] = jobs.pending()
    assert summary.count("### Changes that need your OK") == 1 and "+A fourth bed is planned." in summary


def test_agent_commits_without_a_git_identity(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git(repo, "config", "--unset", "user.email")
    git(repo, "config", "--unset", "user.name")
    monkeypatch.setenv("HOME", str(repo.parent))  # no ~/.gitconfig either, like in a container
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    job = runner(repo).run("forge")
    assert job.state == "merged", job.notes
    assert git(repo, "log", "-1", "--format=%an <%ae>", "main") == "mcpedia <mcpedia@localhost>"


def test_owner_merges_a_waiting_branch(repo: Path) -> None:
    jobs = runner(repo, "flagged", review={"flag": ["wiki/*/my-*.md"]})
    job = jobs.run("forge")
    jobs.merge(job.branch)
    assert "fourth bed" in on_main(repo, "wiki/garden/my-garden.md")
    assert jobs.pending() == []


def test_merge_refuses_uncommitted_wiki_edits(repo: Path) -> None:
    jobs = runner(repo, "flagged", review={"flag": ["wiki/*/my-*.md"]})
    job = jobs.run("forge")
    (repo / "wiki/garden/tomatoes.md").write_text("edited by hand\n")  # not committed
    with pytest.raises(JobError, match=r"uncommitted changes in wiki/garden/tomatoes\.md"):
        jobs.merge(job.branch)


def test_never_merges_raw_changes(repo: Path) -> None:
    jobs = runner(repo, "raw")
    job = jobs.run("forge")
    with pytest.raises(JobError, match="Refusing to merge"):
        jobs.merge(job.branch)


def test_discard(repo: Path) -> None:
    jobs = runner(repo, "waiting")
    job = jobs.run("forge")
    jobs.discard(job.branch)
    assert jobs.pending() == []
    with pytest.raises(JobError, match="not a forge"):
        jobs.discard("main")


def test_nothing_to_forge(repo: Path) -> None:
    job = runner(repo, "nothing").run("forge")
    assert job.state == "nothing"
    assert "forge/" not in git(repo, "branch", "--list")


def test_failure_is_reported_and_cleaned_up(repo: Path) -> None:
    job = runner(repo, "fail").run("forge")
    assert job.state == "failed"
    assert "exited with code 3: something broke" in job.notes[0]
    assert "forge/" not in git(repo, "branch", "--list")


def test_missing_agent(repo: Path) -> None:
    jobs = JobRunner(make_vault(repo, jobs={"agent": ["no-such-agent-binary"]}))
    job = jobs.run("forge")
    assert job.state == "failed" and "not installed here" in job.notes[0]


def test_audit_report_is_merged(repo: Path) -> None:
    job = runner(repo).run("audit")
    assert job.state == "merged", job.notes
    assert "All good" in on_main(repo, "reports/audit-2026-02-01.md")


def test_needs_git(vault: Vault) -> None:
    with pytest.raises(JobError, match="no git repository"):
        JobRunner(vault).run("forge")


def test_background_start_and_queue(repo: Path) -> None:
    jobs = runner(repo, "slow")
    first, queued = jobs.start("forge")
    assert not queued and first.pid
    again, queued = jobs.start("forge")  # queued behind the first
    assert queued and again.id == first.id
    first = jobs.wait(first, 30)
    assert first.state == "merged", first.notes
    for _ in range(300):  # the job process runs the queued forge next
        if len(all_jobs := jobs.jobs()) == 2 and all_jobs[1].state != "running":
            break
        time.sleep(0.1)
    second = jobs.jobs()[1]
    assert second.state in ("merged", "waiting", "nothing"), second.notes
    assert second.pid == first.pid  # same process, still holding the job lock


def test_a_job_outlives_the_runner_that_started_it(repo: Path) -> None:
    job, _ = runner(repo, "slow").start("forge")
    # A new runner, as in another session's mcpedia, or after the first one's session closed.
    other = JobRunner(Vault.open(repo))
    assert other.running is not None and other.running.id == job.id
    with pytest.raises(JobError, match="still running"):
        other.run("audit")  # one job at a time per vault, across processes
    assert other.recover() == []  # nothing to clean up while the job's process runs
    assert other.wait(job, 30).state == "merged"


def test_recover_cleans_up_an_interrupted_job(repo: Path) -> None:
    jobs = runner(repo)
    job = jobs._new_job("forge")  # marked running, but no process runs it
    tree = jobs.workdir / "trees" / job.id
    git(repo, "worktree", "add", "-q", "-b", job.branch, str(tree), "main")
    (jobs.workdir / "queued").write_text("audit")
    assert jobs.recover() == [f"job {job.id} was interrupted; cleaned up its branch {job.branch}"]
    job = jobs.get(job.id)
    assert job.state == "failed" and "Interrupted" in job.notes[0]
    assert not tree.exists() and "forge/" not in git(repo, "branch", "--list")
    assert not (jobs.workdir / "queued").exists()


def test_times_are_utc_and_dates_follow_the_timezone(repo: Path) -> None:
    jobs = runner(repo, timezone="Pacific/Kiritimati")  # UTC+14: a different date than UTC most of the day
    job = jobs.run("forge")
    assert job.started.utcoffset() == dt.timedelta(0) and job.branch.endswith(f"Z-{job.id[:4]}")
    assert job.branch.startswith(f"forge/{job.started:%Y-%m-%d-%H%M}Z")
    assert jobs.vault.today() == dt.datetime.now(ZoneInfo("Pacific/Kiritimati")).date()
    assert "TZ=Pacific/Kiritimati" in job.summary  # the fake agent reports the TZ it got


def test_captures_commit_on_main_while_a_job_runs(repo: Path) -> None:
    from wikismith.captures import Captures
    from wikismith.secretscan import Scanner

    vault = configure(repo, "slow")
    job, _ = JobRunner(vault).start("forge")
    result = Captures(vault, Scanner()).add("During the job", "Captured mid-forge.")
    assert result.committed and git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    job = JobRunner(vault).wait(job, 30)
    assert job.state == "merged", job.notes
    log = git(repo, "log", "--format=%s", "main")
    assert "Add capture: During the job" in log and "Forge 1 raw file(s): garden" in log


@pytest.mark.anyio
async def test_tools(repo: Path) -> None:
    vault = configure(repo, "waiting")
    async with Client(build_server(vault, warm=False), mode="legacy") as client:

        async def text(tool: str, **args: object) -> str:
            result = await client.call_tool(tool, args)
            return result.content[0].text  # type: ignore[union-attr]

        started = await text("add_capture", title="Basil", content="Sow basil in May.", forge_now=True)
        assert "committed and pushed" in started and "Started job" in started
        status = await text("job_status", wait_seconds=30)
        assert ": waiting" in status and "the agent didn't mark it ready" in status
        pending = await text("list_pending")
        branch = pending.split("branch ")[1].split(")")[0]
        assert "Merged" in await text("merge_forge", branch=branch)
        assert await text("list_pending") == "Nothing is waiting for the owner."
        assert "Started job" in await text("run_audit")
        assert ": merged" in await text("job_status", wait_seconds=30)


def test_audit_lists_only_direct_edits(repo: Path) -> None:
    """The command in the audit instructions, against a history with forge merges, an audit and direct commits."""
    text = asset("jobs/audit.md")
    command = text[text.index("```\nsince=") + 4 : text.index("```", text.index("```\nsince=") + 4)]

    def commit(message: str, path: str, branch: str | None = None) -> None:
        if branch:
            git(repo, "switch", "-q", "-c", branch)
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(message + "\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", message)
        if branch:
            git(repo, "switch", "-q", "main")
            git(repo, "merge", "-q", "--no-ff", "-m", f"Merge {branch}", branch)

    commit("Old hand edit", "wiki/garden/a.md")
    commit("Audit 2026-02-01", "reports/audit-2026-02-01.md", branch="audit/2026-02-01")
    commit("Forge 1 raw file(s): garden", "wiki/garden/b.md", branch="forge/2026-02-02")
    commit("Fixed a typo in Obsidian", "wiki/garden/c.md")
    commit("Add capture: x", "raw/captures/2026-02-03_x.md")
    out = subprocess.run(["sh", "-c", command], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert [line.split(": ", 1)[1] for line in out.splitlines()] == ["Fixed a typo in Obsidian"]


def test_a_job_survives_its_starter_exiting(repo: Path) -> None:
    """Like closing the Claude Code session whose mcpedia started the job."""
    configure(repo, "slow")
    starter = (
        "import sys; from pathlib import Path\nfrom wikismith.jobs import JobRunner\n"
        "from wikismith.vault import Vault\n"
        "job, _ = JobRunner(Vault.open(Path(sys.argv[1]))).start('forge'); print(job.id)"
    )
    out = subprocess.run([sys.executable, "-c", starter, str(repo)], capture_output=True, text=True, check=True)
    jobs = JobRunner(Vault.open(repo))
    job = jobs.wait(jobs.get(out.stdout.strip()), 30)  # the starter is gone; the job finishes anyway
    assert job.state == "merged", job.notes


def test_questions_dont_block_and_go_to_the_queue(repo: Path, tmp_path: Path) -> None:
    from wikismith.questions import QuestionQueue

    message = tmp_path / "notified.txt"
    notify = [sys.executable, "-c", "import sys; open(sys.argv[1], 'w').write(sys.argv[2])", str(message), "{message}"]
    jobs = runner(repo, "questions", jobs={"agent": [sys.executable, AGENT, "questions"], "notify": notify})
    job = jobs.run("forge")
    assert job.state == "merged", job.notes  # the additions merged; the questions wait on their own
    assert "pepper seeds eight weeks" in on_main(repo, "wiki/garden/tomatoes.md")
    questions = QuestionQueue(jobs.vault).open()
    assert [q.area for q in questions] == [None, "pets"]
    assert questions[0].line() in job.report()
    assert message.read_text() == "forge: 2 questions"  # counts only


@pytest.mark.anyio
async def test_answering_questions(repo: Path) -> None:
    import yaml as pyyaml

    vault = configure(repo, "questions")
    JobRunner(vault).run("forge")
    async with Client(build_server(vault, warm=False), mode="legacy") as client:

        async def text(tool: str, **args: object) -> str:
            result = await client.call_tool(tool, args)
            return result.content[0].text  # type: ignore[union-attr]

        pending = await text("list_pending")
        assert pending.startswith("Open questions (2):")
        ids = [line.split("[")[1].split("]")[0] for line in pending.splitlines() if line.startswith("- [")]
        refused = await text("answer_questions", answers={ids[0]: "Yes."}, approve_areas=["pets"])
        assert "Not proposed in the answered questions: pets" in refused  # the area question wasn't answered
        done = await text("answer_questions", answers={ids[0]: "Yes, in town.", ids[1]: "Yes."}, approve_areas=["pets"])
        assert "Captured to raw/captures/" in done and "Started job" in done
        assert await text("list_pending") == "Nothing is waiting for the owner."
        await text("job_status", wait_seconds=30)
    capture = next(p for p in (repo / "raw/captures").glob("*answers-to-forge-questions.md"))
    frontmatter = pyyaml.safe_load(capture.read_text().split("---")[1])
    assert frontmatter["approves_areas"] == ["pets"] and frontmatter["answers"] == ids
    assert "Yes, in town." in capture.read_text()


def test_a_branch_superseded_by_a_newer_forge(repo: Path) -> None:
    jobs = runner(repo, "flagged", review={"flag": ["wiki/*/my-*.md"]})
    old = jobs.run("forge")  # waits: it touches a flagged page
    assert old.state == "waiting"
    jobs = runner(repo)  # the owner unflags it; a new forge does the same capture again and merges
    assert jobs.run("forge").state == "merged"
    assert jobs.superseded(old.branch) == ["raw/captures/2026-01-06_pepper-seeds.md"]
    [(branch, summary)] = jobs.pending()
    assert branch == old.branch and summary.startswith("Superseded: a newer forge has already forged")
    with pytest.raises(JobError, match="would forge them twice"):
        jobs.merge(old.branch)
    jobs.discard(old.branch)
    assert jobs.pending() == []


def test_jobs_start_from_and_merge_into_the_latest_main(repo: Path, tmp_path: Path) -> None:
    """Someone else pushed meanwhile (another machine, the owner by hand): the job pulls before it starts and before
    it merges, so it works on the latest main and its push isn't rejected."""
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(tmp_path / "remote.git"), str(other))
    git(other, "config", "user.email", "o@example.com")
    git(other, "config", "user.name", "Other")
    (other / "raw/notes/2026-01-07_elsewhere.md").write_text("Written on another machine.\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "From elsewhere")
    git(other, "push", "-q")
    job = runner(repo).run("forge")
    assert job.state == "merged", job.notes
    assert not any("push failed" in n or "Could not pull" in n for n in job.notes), job.notes
    remote_log = git(tmp_path / "remote.git", "log", "--format=%s", "main")
    assert "From elsewhere" in remote_log and remote_log.startswith("Merge forge/")
