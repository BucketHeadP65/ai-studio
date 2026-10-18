"""`NoteWriter` refuses a page whose claims do not all read back from the markdown it renders."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from engram.authoring.contracts import NoteWriter
from engram.authoring.exceptions import NoteRefusedError
from engram.kb.contracts import parse
from engram.testing.specs import research_spec
from tests.unit.engram.retrieval.test_note_writer import TALK, _target

if TYPE_CHECKING:
    from pathlib import Path


def _with_claims(*claims: dict[str, str]) -> dict[str, object]:
    return {**TALK, "claims": list(claims)}


_ONE_PARAGRAPH = {"text": "Planning agents keep a task list.", "loc": "§1", "quote": "agents keep a task list", "kind": "observation"}


def test_a_claim_whose_text_holds_one_paragraph_is_written(tmp_path: Path) -> None:
    written = NoteWriter(_target(tmp_path)).write(_with_claims(_ONE_PARAGRAPH))

    assert "- Planning agents keep a task list. ^c1" in written.path.read_text(encoding="utf-8")


def test_a_claim_whose_text_holds_two_paragraphs_is_refused_naming_the_claim(tmp_path: Path) -> None:
    two_paragraphs = {**_ONE_PARAGRAPH, "text": "The list survives compaction.\n\nIt is re-read every turn.", "loc": "§2"}

    with pytest.raises(NoteRefusedError, match=r"claim \^c2 does not read back") as refused:
        NoteWriter(_target(tmp_path)).write(_with_claims(_ONE_PARAGRAPH, two_paragraphs))

    assert "does not read back from the page at all" in str(refused.value)
    assert "one paragraph, with no blank line inside it" in str(refused.value)
    assert not (tmp_path / "Wiki" / "Talks" / "agents-that-plan.md").exists()


def test_a_field_value_with_a_line_break_is_refused_naming_the_claim_and_the_field(tmp_path: Path) -> None:
    broken = {**_ONE_PARAGRAPH, "quote": "agents keep\na task list"}

    with pytest.raises(NoteRefusedError, match=r"claim \^c1 does not read back as written: its quote 'agents keep\\na task list' reads back as") as refused:
        NoteWriter(_target(tmp_path)).write(_with_claims(broken))

    assert "each field value on one line" in str(refused.value)


def test_edge_whitespace_and_a_soft_break_in_the_text_are_not_refused(tmp_path: Path) -> None:
    padded = {**_ONE_PARAGRAPH, "text": " Planning agents keep a task list. ", "loc": "§1 ", "condition": "long sessions "}
    soft_break = {**_ONE_PARAGRAPH, "text": "The list survives\ncompaction.", "loc": "§2"}

    written = NoteWriter(_target(tmp_path)).write(_with_claims(padded, soft_break))

    assert written.id == "talk.agents-that-plan"


def test_check_names_the_claim_that_does_not_read_back_without_writing(tmp_path: Path) -> None:
    two_paragraphs = {**_ONE_PARAGRAPH, "text": "The list survives compaction.\n\nIt is re-read every turn."}

    with pytest.raises(NoteRefusedError, match=r"claim \^c1 does not read back"):
        NoteWriter(_target(tmp_path)).check(_with_claims(two_paragraphs))


def test_a_quote_wrapped_in_double_quotes_is_written_and_reads_back_as_the_bare_snippet(tmp_path: Path) -> None:
    wrapped = {**_ONE_PARAGRAPH, "quote": '"agents keep a task list"'}

    written = NoteWriter(_target(tmp_path)).write(_with_claims(wrapped))

    note = parse(written.path.read_text(encoding="utf-8"), source_path="note.md", spec=research_spec())
    assert [claim.quote for claim in note.claims] == ["agents keep a task list"]


def test_an_obsidian_comment_in_a_claim_is_not_refused(tmp_path: Path) -> None:
    commented = {**_ONE_PARAGRAPH, "text": "Planning agents keep a task list. %%check the date%%", "condition": "long sessions %%from the abstract%%"}

    written = NoteWriter(_target(tmp_path)).write(_with_claims(commented))

    note = parse(written.path.read_text(encoding="utf-8"), source_path="note.md", spec=research_spec())
    assert [claim.text for claim in note.claims] == ["Planning agents keep a task list."]
    assert [claim.fields["condition"] for claim in note.claims] == ["long sessions"]


def test_every_claim_that_does_not_read_back_is_named_at_once(tmp_path: Path) -> None:
    two_paragraphs = {**_ONE_PARAGRAPH, "text": "The list survives compaction.\n\nIt is re-read every turn.", "loc": "§2"}
    broken_quote = {**_ONE_PARAGRAPH, "quote": "agents keep\na task list", "loc": "§3"}

    with pytest.raises(NoteRefusedError) as refused:
        NoteWriter(_target(tmp_path)).write(_with_claims(_ONE_PARAGRAPH, two_paragraphs, broken_quote))

    message = str(refused.value)
    assert "claim ^c2 does not read back from the page at all" in message
    assert "claim ^c3 does not read back as written: its quote" in message
    assert "claim ^c1" not in message
    assert message.count("Keep each claim's text to one paragraph") == 1
