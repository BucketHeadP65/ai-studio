"""Reading and writing note sidecars under a knowledge base's notes home."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.files import atomic_write

from engram.kb.domain.note_threads import NOTES_DIR, NoteSheet

if TYPE_CHECKING:
    from pathlib import Path


def notes_home(start: Path) -> Path:
    """Return the nearest ancestor of `start` (itself first, then upward) that owns a `.agent-threads` directory, else `start`.

    Args:
        start: The folder to search upward from.

    Returns:
        The notes home.
    """
    for candidate in (start, *start.parents):
        if (candidate / NOTES_DIR).is_dir():
            return candidate
    return start


def load_sheet(sidecar: Path) -> NoteSheet:
    """Read `sidecar`'s note sheet. A missing, unreadable, or malformed sidecar answers an empty sheet.

    Args:
        sidecar: The sidecar file.

    Returns:
        The note sheet it holds.
    """
    if not sidecar.is_file():
        return NoteSheet()
    try:
        return NoteSheet.model_validate_json(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return NoteSheet()


def save_sheet(sidecar: Path, sheet: NoteSheet) -> None:
    """Write `sheet` to `sidecar`, creating its parent directories.

    Args:
        sidecar: The sidecar file to write.
        sheet: The note sheet to store.
    """
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(sidecar, sheet.model_dump_json())
