"""`AuthoringTools.read_notes` and `.resolve_note`: reading and resolving owner note threads on a knowledge base file."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from pydantic_ai import ModelRetry

from engram.kb.contracts import Note, NoteAnchor, NoteSheet, Reply, load_sheet, sidecar_for
from tests.unit.engram.retrieval.test_authoring_tools import _doors

if TYPE_CHECKING:
    from pathlib import Path


def _write_sidecar(knowledge_base_root: Path, rel: str, sheet: NoteSheet) -> None:
    """Write `sheet` at `rel`'s sidecar under a knowledge base whose notes home is `knowledge_base_root` itself."""
    (knowledge_base_root / ".agent-threads").mkdir(exist_ok=True)
    sidecar = sidecar_for(knowledge_base_root, knowledge_base_root / rel)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(sheet.model_dump_json(), encoding="utf-8")


def _one_note(note_id: str = "n1") -> Note:
    return Note(id=note_id, anchor=NoteAnchor(exact="text"), text="a comment", created_at=datetime(2026, 1, 1, tzinfo=UTC))


async def test_read_notes_answers_the_text_and_only_the_open_unarchived_threads_in_order(tmp_path: Path) -> None:
    (tmp_path / ".agent-threads").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("the file text\n", encoding="utf-8")
    first = Note(
        id="n1",
        anchor=NoteAnchor(exact="the file"),
        text="first comment",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        replies=(Reply(author="owner", text="a reply", at=datetime(2026, 1, 2, tzinfo=UTC)),),
    )
    archived = first.model_copy(update={"id": "n2", "text": "archived comment", "archived": True})
    resolved = first.model_copy(update={"id": "n3", "text": "resolved comment", "status": "resolved"})
    second = first.model_copy(update={"id": "n4", "text": "second comment", "replies": ()})
    _write_sidecar(tmp_path, "docs/a.md", NoteSheet(notes=(first, archived, resolved, second)))
    doors, _, _ = _doors(tmp_path)

    result = await doors.read_notes("research", "docs/a.md")

    assert result["path"] == "docs/a.md"
    assert result["exists"] is True
    assert result["text"] == "the file text\n"
    threads = result["threads"]
    assert isinstance(threads, list)
    assert [thread["note_id"] for thread in threads] == ["n1", "n4"]
    assert threads[0] == {
        "note_id": "n1",
        "status": "open",
        "anchor": "the file",
        "text": "first comment",
        "created_at": "2026-01-01T00:00:00+00:00",
        "replies": [{"author": "owner", "text": "a reply", "at": "2026-01-02T00:00:00+00:00"}],
    }
    assert threads[1]["replies"] == []


async def test_read_notes_on_a_missing_file_answers_exists_false(tmp_path: Path) -> None:
    (tmp_path / ".agent-threads").mkdir()
    doors, _, _ = _doors(tmp_path)

    result = await doors.read_notes("research", "docs/nope.md")

    assert result == {"path": "docs/nope.md", "exists": False, "text": "", "threads": []}


async def test_read_notes_refuses_a_path_that_escapes_the_knowledge_base_root(tmp_path: Path) -> None:
    (tmp_path / ".agent-threads").mkdir()
    doors, _, _ = _doors(tmp_path)

    with pytest.raises(ModelRetry):
        await doors.read_notes("research", "../x.md")


async def test_read_notes_refuses_an_absolute_path(tmp_path: Path) -> None:
    (tmp_path / ".agent-threads").mkdir()
    doors, _, _ = _doors(tmp_path)

    with pytest.raises(ModelRetry):
        await doors.read_notes("research", "/abs.md")


async def test_resolve_note_with_a_reply_appends_it_and_resolves_the_thread(tmp_path: Path) -> None:
    (tmp_path / ".agent-threads").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("text\n", encoding="utf-8")
    _write_sidecar(tmp_path, "docs/a.md", NoteSheet(notes=(_one_note(),)))
    doors, _, _ = _doors(tmp_path)

    said = await doors.resolve_note("research", "docs/a.md", "n1", reply="looks good")

    assert said == "replied and resolved n1 on docs/a.md"
    reloaded = load_sheet(sidecar_for(tmp_path, tmp_path / "docs" / "a.md"))
    assert reloaded.notes[0].status == "resolved"
    assert len(reloaded.notes[0].replies) == 1
    assert reloaded.notes[0].replies[0].author == "agent"
    assert reloaded.notes[0].replies[0].text == "looks good"


async def test_resolve_note_without_a_reply_just_resolves(tmp_path: Path) -> None:
    (tmp_path / ".agent-threads").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("text\n", encoding="utf-8")
    _write_sidecar(tmp_path, "docs/a.md", NoteSheet(notes=(_one_note(),)))
    doors, _, _ = _doors(tmp_path)

    said = await doors.resolve_note("research", "docs/a.md", "n1")

    assert said == "resolved n1 on docs/a.md"
    reloaded = load_sheet(sidecar_for(tmp_path, tmp_path / "docs" / "a.md"))
    assert reloaded.notes[0].status == "resolved"
    assert reloaded.notes[0].replies == ()


async def test_resolve_note_on_an_unknown_id_names_the_files_thread_ids(tmp_path: Path) -> None:
    (tmp_path / ".agent-threads").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("text\n", encoding="utf-8")
    _write_sidecar(tmp_path, "docs/a.md", NoteSheet(notes=(_one_note(),)))
    doors, _, _ = _doors(tmp_path)

    with pytest.raises(ModelRetry, match="n1"):
        await doors.resolve_note("research", "docs/a.md", "nope")


def test_tools_names_read_notes_and_resolve_note(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path)

    names = [name for _fn, name, _description in doors.tools()]

    assert "read_notes" in names
    assert "resolve_note" in names
