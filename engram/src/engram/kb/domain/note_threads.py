"""Owner note threads on a knowledge base's markdown: the sidecar sheet and its home under the knowledge base.

Each annotated file's notes live at `<home>/.agent-threads/<file relative to home>.threads.json`,
`home` being the nearest directory owning a `.agent-threads` folder (`notes_home`). A note is a
discussion thread: the original comment, a status (`open` or `resolved`), and a sequence of
replies each carrying its own author (`owner` or `agent`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NOTES_DIR = Path(".agent-threads")
"""Where every sidecar lives, relative to a knowledge base's notes home. Its presence also marks the home."""

NOTES_SUFFIX = ".threads.json"
"""The sidecar file suffix appended to an annotated file's own path."""


class NoteAnchor(BaseModel):
    """A text-quote selector on the source markdown."""

    model_config = ConfigDict(frozen=True)

    exact: str = Field(description="The anchored span, verbatim from the source markdown.")
    prefix: str = Field(default="", description="Up to 32 chars before the span, disambiguating repeats.")
    suffix: str = Field(default="", description="Up to 32 chars after the span.")


class Reply(BaseModel):
    """One reply in a note's thread."""

    author: Literal["owner", "agent"] = Field(description="Who wrote the reply.")
    text: str = Field(description="The reply body.")
    at: datetime = Field(description="When it was posted.")


class Note(BaseModel):
    """One owner annotation: the original comment, its thread of replies, and its status.

    A note written without `replies` or `status` parses as a note with no replies and an open thread.
    """

    id: str = Field(description="Note id, a uuid4 string.")
    anchor: NoteAnchor = Field(description="Where the note points.")
    text: str = Field(description="The note body.")
    created_at: datetime = Field(description="When it was placed.")
    archived: bool = Field(default=False, description="Hidden from reads and the default listing.")
    replies: tuple[Reply, ...] = Field(default=(), description="The thread's replies, in the order they were posted.")
    status: Literal["open", "resolved"] = Field(default="open", description="Whether the thread is still open.")


class NoteSheet(BaseModel):
    """The sidecar's whole content, with its format version."""

    version: Literal[1] = Field(default=1, description="Sidecar format version.")
    notes: tuple[Note, ...] = Field(default=(), description="Every note on the file, archived included.")


def sidecar_for(home: Path, annotated: Path) -> Path:
    """The sidecar path of `annotated` under `home`.

    Args:
        home: The notes home, the directory that owns the `.agent-threads` folder.
        annotated: The annotated file, which must lie under `home`.

    Returns:
        `home/.agent-threads/<annotated relative to home>.threads.json`.
    """
    rel = annotated.relative_to(home)
    return home / NOTES_DIR / rel.with_name(rel.name + NOTES_SUFFIX)


def open_threads(sheet: NoteSheet) -> tuple[Note, ...]:
    """Every note in `sheet` whose thread is open and not archived, in sheet order.

    Args:
        sheet: The sidecar content to read.

    Returns:
        The open, unarchived notes.
    """
    return tuple(note for note in sheet.notes if note.status == "open" and not note.archived)


def add_reply(sheet: NoteSheet, note_id: str, *, author: Literal["owner", "agent"], text: str) -> tuple[NoteSheet, Note]:
    """Return `sheet` with a reply appended to `note_id`'s thread, and the updated note.

    The reply is stamped with the current UTC time.

    Args:
        sheet: The sidecar content, left unchanged.
        note_id: The id of the note to reply to.
        author: Who writes the reply.
        text: The reply body.

    Returns:
        The new sheet and the updated note.

    Raises:
        KeyError: If no note in `sheet` has `note_id`.
    """
    reply = Reply(author=author, text=text, at=datetime.now(UTC))
    updated: Note | None = None
    notes_out: list[Note] = []
    for note in sheet.notes:
        if note.id != note_id:
            notes_out.append(note)
            continue
        updated = note.model_copy(update={"replies": (*note.replies, reply)})
        notes_out.append(updated)
    if updated is None:
        msg = f"no note with id {note_id!r}"
        raise KeyError(msg)
    return NoteSheet(version=sheet.version, notes=tuple(notes_out)), updated


def set_status(sheet: NoteSheet, note_id: str, status: Literal["open", "resolved"]) -> tuple[NoteSheet, Note]:
    """Return `sheet` with `note_id`'s thread status changed, and the updated note.

    Args:
        sheet: The sidecar content, left unchanged.
        note_id: The id of the note to change.
        status: The new thread status.

    Returns:
        The new sheet and the updated note.

    Raises:
        KeyError: If no note in `sheet` has `note_id`.
    """
    updated: Note | None = None
    notes_out: list[Note] = []
    for note in sheet.notes:
        if note.id != note_id:
            notes_out.append(note)
            continue
        updated = note.model_copy(update={"status": status})
        notes_out.append(updated)
    if updated is None:
        msg = f"no note with id {note_id!r}"
        raise KeyError(msg)
    return NoteSheet(version=sheet.version, notes=tuple(notes_out)), updated
