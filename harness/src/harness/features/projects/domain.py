"""The owner notes on a project's markdown: the edits to a note sheet, the anchor ladder and the agent's render.

An anchor is a W3C-style text-quote selector on the source markdown: `exact`, with `prefix` and `suffix` to tell a
repeated `exact` apart. `resolve_anchor` finds it again in a file's current text on every call, so an edit that moves or
rewords the text around it never touches the sidecar. A note whose anchor no longer resolves stays in the sheet, marked
orphaned rather than dropped. Each note is a thread: the owner's comment, a status (`open` or `resolved`) and replies,
each with its own author (`owner` or `agent`).
"""

from __future__ import annotations

import hashlib
import re

from datetime import UTC, datetime
from uuid import uuid4

from engram.kb.contracts import Note, NoteAnchor, NoteSheet
from pydantic import BaseModel, ConfigDict, Field

_FENCE_HEADER = "--- owner notes (annotations, not part of the file) ---"


class AnchorHit(BaseModel):
    """A resolved anchor: where the span sits in the current text."""

    model_config = ConfigDict(frozen=True)

    offset: int = Field(description="Character offset of the span start.")
    end: int = Field(description="Character offset just past the span's end.")
    line: int = Field(description="1-based line of the span start.")


def short_content_hash(content: str) -> str:
    """The hash that detects a conflicting write: the sha256 hex digest of `content`, cut to its first 12 characters.

    Args:
        content: The text to hash.

    Returns:
        Twelve hex characters.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def add_note(sheet: NoteSheet, *, anchor: NoteAnchor, text: str) -> tuple[NoteSheet, Note]:
    """Append a new owner note to `sheet`, minting its id and its timestamp.

    Args:
        sheet: The file's note sheet.
        anchor: Where the note points.
        text: The note body.

    Returns:
        The sheet with the note appended, and the new note.
    """
    note = Note(id=str(uuid4()), anchor=anchor, text=text, created_at=datetime.now(UTC))
    return NoteSheet(version=sheet.version, notes=(*sheet.notes, note)), note


def replace_note(sheet: NoteSheet, note_id: str, *, text: str | None, archived: bool | None) -> tuple[NoteSheet, Note]:
    """Change the text, the archived state or both of one owner note in `sheet`.

    Args:
        sheet: The file's note sheet.
        note_id: The note to change.
        text: The new note body, or `None` to keep it.
        archived: The new archived state, or `None` to keep it.

    Returns:
        The sheet with the note changed, and the changed note.

    Raises:
        KeyError: No note in `sheet` has `note_id`.
    """
    updated: Note | None = None
    notes_out: list[Note] = []
    for note in sheet.notes:
        if note.id != note_id:
            notes_out.append(note)
            continue
        changes = {k: v for k, v in {"text": text, "archived": archived}.items() if v is not None}
        updated = note.model_copy(update=changes)
        notes_out.append(updated)
    if updated is None:
        msg = f"no note with id {note_id!r}"
        raise KeyError(msg)
    return NoteSheet(version=sheet.version, notes=tuple(notes_out)), updated


def remove_note(sheet: NoteSheet, note_id: str) -> NoteSheet:
    """Remove one owner note from `sheet`.

    Args:
        sheet: The file's note sheet.
        note_id: The note to remove.

    Returns:
        The sheet without the note.

    Raises:
        KeyError: No note in `sheet` has `note_id`.
    """
    remaining = tuple(note for note in sheet.notes if note.id != note_id)
    if len(remaining) == len(sheet.notes):
        msg = f"no note with id {note_id!r}"
        raise KeyError(msg)
    return NoteSheet(version=sheet.version, notes=remaining)


def replace_reply(sheet: NoteSheet, note_id: str, reply_index: int, *, text: str) -> tuple[NoteSheet, Note]:
    """Give one owner reply in a note's thread new text. An agent reply is never edited.

    Args:
        sheet: The file's note sheet.
        note_id: The note whose thread holds the reply.
        reply_index: The reply's 0-based position in the thread.
        text: The reply's new body.

    Returns:
        The sheet with the reply changed, and the changed note.

    Raises:
        KeyError: No note in `sheet` has `note_id`, or `reply_index` is out of range for its thread.
        PermissionError: The agent wrote the reply at `reply_index`.
    """
    updated: Note | None = None
    notes_out: list[Note] = []
    for note in sheet.notes:
        if note.id != note_id:
            notes_out.append(note)
            continue
        if reply_index < 0 or reply_index >= len(note.replies):
            msg = f"no reply at index {reply_index} on note {note_id!r}"
            raise KeyError(msg)
        target = note.replies[reply_index]
        if target.author != "owner":
            msg = "an agent reply cannot be edited"
            raise PermissionError(msg)
        replies = list(note.replies)
        replies[reply_index] = target.model_copy(update={"text": text})
        updated = note.model_copy(update={"replies": tuple(replies)})
        notes_out.append(updated)
    if updated is None:
        msg = f"no note with id {note_id!r}"
        raise KeyError(msg)
    return NoteSheet(version=sheet.version, notes=tuple(notes_out)), updated


def resolve_anchor(text: str, anchor: NoteAnchor) -> AnchorHit | None:
    """Find `anchor` in `text`, by exact match first and by a whitespace-normalized match second.

    A repeated `exact` narrows by prefix and suffix, then to the one heading occurrence when the anchor was taken on a
    heading, then by prefix alone or by suffix alone.

    Args:
        text: The annotated file's current text.
        anchor: Where the note points.

    Returns:
        Where the span sits, or `None` when the anchor is orphaned.
    """
    exact = anchor.exact
    if not exact:
        return None
    hit = _single_hit(text, list(re.finditer(re.escape(exact), text)), anchor)
    if hit is not None:
        return hit
    return _single_hit(text, list(_flexible_pattern(exact).finditer(text)), anchor)


def annotate_read(result: str, text: str, sheet: NoteSheet) -> str:
    """Append every open, unarchived thread of `sheet` to a `read_file` return, replies included.

    A resolved thread is a settled conversation and is left out, as an archived one is. `result` is untouched above
    the fence.

    Args:
        result: What `read_file` returned.
        text: The annotated file's current text.
        sheet: The file's note sheet.

    Returns:
        `result` with the fence and the threads below it, or `result` alone when no thread is shown.
    """
    shown = [note for note in sheet.notes if not note.archived and note.status == "open"]
    lines: list[str] = []
    for index, note in enumerate(shown, start=1):
        lines.append(f'[n{index}] {_where(text, note)}, status open, on "{note.anchor.exact}": {note.text}')
        lines.extend(f"    reply ({reply.author}): {reply.text}" for reply in note.replies)
    if not lines:
        return result
    return f"{result}\n\n{_FENCE_HEADER}\n" + "\n".join(lines) + "\n"


def _where(text: str, note: Note) -> str:
    """`"line <n>"` for a note whose anchor still resolves, `"orphaned"` otherwise."""
    hit = resolve_anchor(text, note.anchor)
    return "orphaned" if hit is None else f"line {hit.line}"


def _hit_at(text: str, start: int, end: int) -> AnchorHit:
    return AnchorHit(offset=start, end=end, line=text.count("\n", 0, start) + 1)


def _single_hit(text: str, matches: list[re.Match[str]], anchor: NoteAnchor) -> AnchorHit | None:
    """The one span `matches` narrows to by the anchor ladder, else `None`."""
    if len(matches) == 1:
        return _hit_at(text, matches[0].start(), matches[0].end())
    if len(matches) > 1:
        pools = [
            [m for m in matches if _prefix_matches(text, m.start(), anchor.prefix) and _suffix_matches(text, m.end(), anchor.suffix)],
            [m for m in matches if _starts_heading_line(text, m.start())] if _anchor_on_heading(anchor) else [],
            [m for m in matches if _prefix_matches(text, m.start(), anchor.prefix)] if anchor.prefix else [],
            [m for m in matches if _suffix_matches(text, m.end(), anchor.suffix)] if anchor.suffix else [],
        ]
        for pool in pools:
            if len(pool) == 1:
                return _hit_at(text, pool[0].start(), pool[0].end())
    return None


def _prefix_matches(text: str, start: int, prefix: str) -> bool:
    return not prefix or text[max(0, start - len(prefix)) : start].endswith(prefix)


def _suffix_matches(text: str, end: int, suffix: str) -> bool:
    return not suffix or text[end : end + len(suffix)].startswith(suffix)


_HEADING_TAIL = re.compile(r"(?:^|\n)#{1,6} \Z")
_HEADING_LINE = re.compile(r"#{1,6} ")


def _anchor_on_heading(anchor: NoteAnchor) -> bool:
    """True when the anchor's own prefix shows the note was taken on a heading's text."""
    return _HEADING_TAIL.search(anchor.prefix) is not None


def _starts_heading_line(text: str, start: int) -> bool:
    """True when the span at `start` sits right after a heading marker at the start of its line."""
    line_start = text.rfind("\n", 0, start) + 1
    return _HEADING_LINE.fullmatch(text, line_start, start) is not None


def _flexible_pattern(exact: str) -> re.Pattern[str]:
    """`exact` as a regex where any run of whitespace matches any run of whitespace in the target."""
    tokens = re.split(r"(\s+)", exact)
    escaped = (r"\s+" if token.isspace() else re.escape(token) for token in tokens)
    return re.compile("".join(escaped), re.DOTALL)
