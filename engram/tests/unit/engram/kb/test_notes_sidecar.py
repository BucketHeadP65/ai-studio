"""`engram.kb.domain.note_threads` and `engram.kb.infrastructure.note_sidecars`: the sidecar sheet, its home, and the thread-editing helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from engram.kb.contracts import Note, NoteAnchor, NoteSheet, add_reply, load_sheet, notes_home, save_sheet, set_status, sidecar_for

if TYPE_CHECKING:
    from pathlib import Path


def _note(note_id: str = "n1") -> Note:
    return Note(id=note_id, anchor=NoteAnchor(exact="a span"), text="a note", created_at=datetime(2026, 1, 1, tzinfo=UTC))


def test_a_saved_sheet_reloads_equal(tmp_path: Path) -> None:
    sidecar = tmp_path / "docs" / "a.md.threads.json"
    sheet = NoteSheet(notes=(_note(),))

    save_sheet(sidecar, sheet)

    assert load_sheet(sidecar) == sheet


def test_load_sheet_on_a_missing_path_answers_an_empty_sheet(tmp_path: Path) -> None:
    assert load_sheet(tmp_path / "nope.threads.json") == NoteSheet()


def test_load_sheet_on_unparseable_content_answers_an_empty_sheet(tmp_path: Path) -> None:
    sidecar = tmp_path / "bad.threads.json"
    sidecar.write_text("not json", encoding="utf-8")

    assert load_sheet(sidecar) == NoteSheet()


def test_add_reply_returns_the_updated_note_and_appends_to_the_sheet() -> None:
    sheet = NoteSheet(notes=(_note(),))

    updated_sheet, updated_note = add_reply(sheet, "n1", author="owner", text="a reply")

    assert updated_note.replies[-1].text == "a reply"
    assert updated_note.replies[-1].author == "owner"
    assert updated_sheet.notes[0] == updated_note


def test_add_reply_on_an_unknown_id_raises_key_error() -> None:
    with pytest.raises(KeyError):
        add_reply(NoteSheet(notes=(_note(),)), "nope", author="owner", text="x")


def test_set_status_returns_the_updated_note_and_the_sheet() -> None:
    sheet = NoteSheet(notes=(_note(),))

    updated_sheet, updated_note = set_status(sheet, "n1", "resolved")

    assert updated_note.status == "resolved"
    assert updated_sheet.notes[0].status == "resolved"


def test_set_status_on_an_unknown_id_raises_key_error() -> None:
    with pytest.raises(KeyError):
        set_status(NoteSheet(notes=(_note(),)), "nope", "resolved")


def test_notes_home_walks_up_to_the_directory_owning_agent_threads(tmp_path: Path) -> None:
    (tmp_path / ".agent-threads").mkdir()
    nested = tmp_path / "docs" / "adr"
    nested.mkdir(parents=True)

    assert notes_home(nested) == tmp_path


def test_notes_home_answers_start_when_no_ancestor_owns_agent_threads(tmp_path: Path) -> None:
    nested = tmp_path / "docs"
    nested.mkdir()

    assert notes_home(nested) == nested


def test_sidecar_for_mirrors_the_annotated_path_under_dot_agent_threads(tmp_path: Path) -> None:
    home = tmp_path
    assert sidecar_for(home, home / "docs" / "a.md") == home / ".agent-threads" / "docs" / "a.md.threads.json"


def test_the_saved_bytes_equal_the_sheets_own_json(tmp_path: Path) -> None:
    sidecar = tmp_path / "a.md.threads.json"
    sheet = NoteSheet(notes=(_note(),))

    save_sheet(sidecar, sheet)

    assert sidecar.read_text(encoding="utf-8") == sheet.model_dump_json()
