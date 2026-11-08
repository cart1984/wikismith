"""The question queue: parsing forge summaries, replacing repeats, answering."""

import datetime as dt

import pytest

from wikismith.questions import QuestionError, QuestionQueue, answer_capture, parse_questions, summary_counts
from wikismith.vault import Vault

TODAY = dt.date(2026, 2, 1)
SUMMARY = """## Forge forge/x: ready to merge

### Added
- wiki/garden/tomatoes.md: pepper seeds (raw/captures/a.md)

### Questions
- Which bed gets the peppers? — files: raw/captures/b.md
- New area `pets` for the cat's vet visits? [new area: pets] — files: `raw/captures/c.md`, raw/captures/d.md
- A line without files is not a question for the queue
"""


def test_parse_questions() -> None:
    first, second = parse_questions(SUMMARY, "job1", TODAY)
    assert (first.question, first.files, first.area) == ("Which bed gets the peppers?", ("raw/captures/b.md",), None)
    assert second.question == "New area `pets` for the cat's vet visits?" and second.area == "pets"
    assert second.files == ("raw/captures/c.md", "raw/captures/d.md")
    assert second.line().endswith("[proposes the new area `pets`]")
    assert parse_questions("## Forge x: ready to merge\n\n### Added\n- a\n", "j", TODAY) == []


def test_a_new_question_about_the_same_files_replaces_the_old_one(vault: Vault) -> None:
    queue = QuestionQueue(vault)
    queue.add(parse_questions(SUMMARY, "job1", TODAY))
    again = parse_questions(SUMMARY.replace("Which bed", "Which raised bed"), "job2", TODAY)
    queue.add(again[:1])
    questions = queue.open()
    assert [q.question for q in questions] == [
        "New area `pets` for the cat's vet visits?",
        "Which raised bed gets the peppers?",
    ]


def test_take(vault: Vault) -> None:
    queue = QuestionQueue(vault)
    queue.add(parse_questions(SUMMARY, "job1", TODAY))
    first = queue.open()[0]
    assert queue.take([first.id]) == [first] and len(queue.open()) == 1
    with pytest.raises(QuestionError, match="No open question"):
        queue.take([first.id])


def test_answer_capture() -> None:
    questions = parse_questions(SUMMARY, "job1", TODAY)
    content, frontmatter = answer_capture(questions, {q.id: "Yes." for q in questions}, ["pets"])
    assert "## Which bed gets the peppers?\n\nFiles: raw/captures/b.md\n\nYes." in content
    assert frontmatter == {
        "answers": [q.id for q in questions],
        "files": ["raw/captures/b.md", "raw/captures/c.md", "raw/captures/d.md"],
        "approves_areas": ["pets"],
    }


def test_notification_holds_counts_only() -> None:
    assert summary_counts(2, 1) == "forge: 2 questions, 1 branch waiting"
    assert summary_counts(1, 0) == "forge: 1 question"
