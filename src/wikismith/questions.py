"""Questions from forges, waiting for the owner, and their answers.

A forge doesn't stop for questions: it leaves the files it can't decide unforged and lists a question for each
under `### Questions` in its summary, one line per question:

    - <question> — files: <raw path>[, <raw path>]
    - New area `pets` for the cat's vet visits and food? [new area: pets] — files: raw/captures/...

mcpedia collects them in a queue (a file in the vault's cache folder) that any session can answer. The answers become
one capture, citing the files, with the areas the owner approved in its frontmatter (`approves_areas`). The next
forge picks the capture up together with the files. If the queue is ever lost, nothing is: the files are still
unforged, so the next forge asks again.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from dataclasses import asdict, dataclass

from wikismith.gitops import repo_lock
from wikismith.vault import Vault

LINE = re.compile(r"^\s*[-*]\s+(?P<question>.+?)\s+—\s+files?:\s*(?P<files>.+?)\s*$")
AREA = re.compile(r"\[new area:\s*(?P<name>[a-z0-9][a-z0-9-]*)\]", re.IGNORECASE)
ANSWER_TITLE = "Answers to forge questions"


class QuestionError(ValueError):
    pass


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    files: tuple[str, ...]
    job: str
    asked: str  # date
    area: str | None = None  # a proposed new area

    def line(self) -> str:
        area = f" [proposes the new area `{self.area}`]" if self.area else ""
        return f"- [{self.id}] {self.question} (about {', '.join(self.files)}){area}"


def parse_questions(summary: str, job_id: str, today: dt.date) -> list[Question]:
    """The questions listed under `### Questions` in a forge summary."""
    section = re.search(r"^###\s+Questions\s*$(?P<body>.*?)(?=^#{1,3}\s|\Z)", summary, re.MULTILINE | re.DOTALL)
    if not section:
        return []
    out = []
    for raw in section.group("body").splitlines():
        if m := LINE.match(raw):
            files = tuple(f.strip().strip("`") for f in re.split(r",\s*", m.group("files")) if f.strip())
            area = m2.group("name").lower() if (m2 := AREA.search(m.group("question"))) else None
            question = AREA.sub("", m.group("question")).strip()
            out.append(Question(uuid.uuid4().hex[:6], question, files, job_id, today.isoformat(), area))
    return out


class QuestionQueue:
    def __init__(self, vault: Vault) -> None:
        self.vault = vault
        self.path = vault.cache_dir / "questions.json"

    def open(self) -> list[Question]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [Question(**{**q, "files": tuple(q["files"])}) for q in data]

    def add(self, questions: list[Question]) -> None:
        """Queue new questions. A question about the same files as an open one replaces it: a forge asks again
        about whatever is still unforged, so the newest wording wins and nothing piles up."""
        if not questions:
            return
        with repo_lock(self.vault.root):
            asked = {f for q in questions for f in q.files}
            kept = [q for q in self.open() if not (set(q.files) & asked)]
            self._save(kept + questions)

    def take(self, ids: list[str]) -> list[Question]:
        """Remove answered questions from the queue and return them."""
        with repo_lock(self.vault.root):
            current = self.open()
            known = {q.id: q for q in current}
            unknown = [i for i in ids if i not in known]
            if unknown:
                raise QuestionError(f"No open question {', '.join(unknown)}. Open: {', '.join(known) or 'none'}.")
            self._save([q for q in current if q.id not in ids])
            return [known[i] for i in ids]

    def _save(self, questions: list[Question]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(q) for q in questions], indent=2), encoding="utf-8")
        tmp.replace(self.path)


def answer_capture(questions: list[Question], answers: dict[str, str], approve: list[str]) -> tuple[str, dict]:
    """The text and frontmatter of the capture that records the owner's answers."""
    lines = ["The owner's answers to questions from earlier forges. Forge them together with the files they name.", ""]
    for q in questions:
        lines += [f"## {q.question}", "", f"Files: {', '.join(q.files)}", "", answers[q.id].strip(), ""]
    if approve:
        lines.append("New areas approved: " + ", ".join(f"`{a}`" for a in approve) + ".")
    frontmatter = {"answers": [q.id for q in questions], "files": sorted({f for q in questions for f in q.files})}
    if approve:
        frontmatter["approves_areas"] = approve
    return "\n".join(lines).strip() + "\n", frontmatter


def summary_counts(open_questions: int, waiting: int) -> str:
    """For notifications: counts only, never vault content."""
    parts = []
    if open_questions:
        parts.append(f"{open_questions} question{'s' if open_questions != 1 else ''}")
    if waiting:
        parts.append(f"{waiting} branch{'es' if waiting != 1 else ''} waiting")
    return "forge: " + ", ".join(parts)
