"""`NoteWriter.create`: publish a checked note at a name nothing holds, and never replace anything."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from engram.authoring.contracts import AuthoringKnowledgeBase, NoteDraft, NoteRefusedError, NoteWriter
from engram.testing.specs import minimal_spec

if TYPE_CHECKING:
    from pathlib import Path

DRAFT = NoteDraft.model_validate(
    {
        "page_type": "note",
        "title": "What the run concluded",
        "summary": "One reading of what the evidence supports.",
        "frontmatter": {"status": "synthesized", "created": "2026-09-13"},
        "sections": [{"heading": "TL;DR", "body": "It holds under the stated conditions."}],
    }
)

NOTE_ID = "note0123456789abcdef01234567"


def _target(tmp_path: Path) -> AuthoringKnowledgeBase:
    (tmp_path / "_meta" / "vocabulary").mkdir(parents=True)
    return AuthoringKnowledgeBase(name="minimal", collection="minimal", path=tmp_path, schema=minimal_spec())


def test_a_checked_note_lands_under_its_page_types_folder(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    created = writer.create(writer.check(DRAFT.payload(NOTE_ID)))
    assert created.id == NOTE_ID
    assert created.path == tmp_path / "Units" / "Notes" / "0123456789abcdef01234567.md"
    text = created.path.read_text(encoding="utf-8")
    assert "## TL;DR" in text
    assert "It holds under the stated conditions." in text


def test_a_second_create_of_the_same_note_refuses_and_leaves_the_file_alone(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    created = writer.create(writer.check(DRAFT.payload(NOTE_ID)))
    standing = created.path.read_bytes()
    with pytest.raises(FileExistsError):
        writer.create(writer.check(DRAFT.payload(NOTE_ID)))
    assert created.path.read_bytes() == standing


def test_an_edit_at_that_name_is_never_replaced_by_a_create(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    checked = writer.check(DRAFT.payload(NOTE_ID))
    checked.path.parent.mkdir(parents=True, exist_ok=True)
    checked.path.write_text("what somebody wrote here first", encoding="utf-8")
    with pytest.raises(FileExistsError):
        writer.create(checked)
    assert checked.path.read_text(encoding="utf-8") == "what somebody wrote here first"


def test_a_payload_that_breaks_the_grammar_never_reaches_a_create(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    wrong_prefix = DRAFT.payload("entrymine")
    with pytest.raises(NoteRefusedError):
        writer.check(wrong_prefix)
    assert not (tmp_path / "Units").exists()


def test_the_same_draft_renders_the_same_bytes_whatever_the_day(tmp_path: Path) -> None:
    """A draft carrying its own `created` is not stamped again, so a retry tomorrow publishes today's bytes."""
    writer = NoteWriter(_target(tmp_path))
    first = writer.check(DRAFT.payload(NOTE_ID))
    second = writer.check(DRAFT.payload(NOTE_ID))
    assert first.rendered == second.rendered
    assert "created: '2026-09-13'" in first.rendered
