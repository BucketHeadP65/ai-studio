"""The note sidecars: each annotated file's owner notes, kept under the project root.

A file's notes live at `.agent-threads/<relative-file-path>.threads.json`, version 1, in a tree that mirrors the
project's own.
"""

from __future__ import annotations

import os

from pathlib import Path

from core.files import atomic_write
from core.observability import get_logger
from engram.kb.contracts import NOTES_DIR, NOTES_SUFFIX, NoteSheet

_logger = get_logger(__name__)


def sidecar_path(root: Path, file: Path) -> Path:
    """Where `file`'s sidecar lives under `root`: `.agent-threads/<relative-path>.threads.json`.

    `file` must be `root` itself or a path under it. `root` is resolved before the comparison, so a project root
    reached through a symlink still holds a `file` that arrives resolved.

    Args:
        root: The project root, as the caller holds it.
        file: The annotated file, resolved.

    Returns:
        The sidecar's path, whether or not it exists.
    """
    real_root = Path(os.path.realpath(root))
    rel = file.relative_to(real_root)
    return real_root / NOTES_DIR / rel.with_name(rel.name + NOTES_SUFFIX)


def has_sidecar(root: Path, file: Path) -> bool:
    """Whether `file` carries a note sidecar.

    Args:
        root: The project root.
        file: The file, resolved.

    Returns:
        True when the sidecar exists as a file.
    """
    return sidecar_path(root, file).is_file()


def load_sheet(root: Path, file: Path) -> NoteSheet:
    """`file`'s note sheet. No sidecar answers an empty sheet, and an unreadable one logs a warning and answers one too.

    Args:
        root: The project root.
        file: The annotated file, resolved.

    Returns:
        The note sheet.
    """
    sidecar = sidecar_path(root, file)
    if not sidecar.is_file():
        return NoteSheet()
    try:
        raw = sidecar.read_text(encoding="utf-8")
        return NoteSheet.model_validate_json(raw)
    except (OSError, ValueError) as exc:
        _logger.warning("note sidecar unreadable, treating {file} as unannotated: {reason}", file=str(file), reason=str(exc))
        return NoteSheet()


def save_sheet(root: Path, file: Path, sheet: NoteSheet) -> None:
    """Write `sheet` to `file`'s sidecar under `root`, creating its parent directories.

    Args:
        root: The project root.
        file: The annotated file, resolved.
        sheet: The note sheet to write.
    """
    sidecar = sidecar_path(root, file)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(sidecar, sheet.model_dump_json())


def annotated_files(root: Path) -> list[tuple[Path, str]]:
    """Every file under `root` that carries a note sidecar, sorted by its root-relative path.

    Args:
        root: The project root.

    Returns:
        One pair per file: its absolute path, and the same path relative to `root`, forward-slash separated.
    """
    real_root = Path(os.path.realpath(root))
    found: dict[str, Path] = {}
    notes_home = real_root / NOTES_DIR
    if notes_home.is_dir():
        for sidecar in notes_home.rglob(f"*{NOTES_SUFFIX}"):
            rel_in_home = sidecar.relative_to(notes_home)
            annotated_rel = rel_in_home.with_name(rel_in_home.name.removesuffix(NOTES_SUFFIX))
            annotated = real_root / annotated_rel
            if annotated.is_file():
                found[annotated_rel.as_posix()] = annotated
    pairs = sorted(found.items(), key=lambda pair: pair[0])
    return [(path, rel) for rel, path in pairs]
