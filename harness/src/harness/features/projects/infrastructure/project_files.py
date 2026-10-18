"""A project's own files: contained reads and writes under its root.

Every relative path is resolved against the root with `os.path.realpath` before any I/O. A `../` segment and a symlink
that leads outside the root are refused the same way, whether the target exists yet or not. Every content hash is
`short_content_hash`. Rename carries a file's note sidecar, and delete removes it with the file.
"""

from __future__ import annotations

import os

from pathlib import Path

from core.files import atomic_write

from harness.features.projects.domain import short_content_hash
from harness.features.projects.infrastructure.note_sidecars import sidecar_path
from harness.features.projects.read_models import FileEntry


def resolve_in_project(root: Path, rel: str) -> Path:
    """`rel` resolved against `root`, refused when it resolves outside it.

    Resolution comes first, so a symlink is judged by where it leads, not by its own name.

    Args:
        root: The project root.
        rel: A path relative to `root`.

    Returns:
        The real path `rel` names, whether or not anything is there yet.

    Raises:
        PermissionError: `rel` names a location outside `root`, by a `../` segment or by a symlink.
    """
    real_root = Path(os.path.realpath(root))
    candidate = (root / rel).resolve()
    real = Path(os.path.realpath(candidate))
    if not real.is_relative_to(real_root):
        msg = f"path {rel!r} resolves outside the project root"
        raise PermissionError(msg)
    return real


def file_text(file: Path) -> str:
    """`file`'s text, decoded strictly as UTF-8, with its line endings translated.

    Args:
        file: The file to read.

    Returns:
        The text.
    """
    return file.read_text(encoding="utf-8")


def file_text_replacing_undecodable(file: Path) -> str:
    """`file`'s bytes decoded as UTF-8, each undecodable byte replaced and every line ending kept.

    Args:
        file: The file to read.

    Returns:
        The text.
    """
    return file.read_bytes().decode("utf-8", errors="replace")


def tree(root: Path) -> list[FileEntry]:
    """Every directory and file under `root`, skipping every name that starts with a dot.

    A symlink that resolves outside `root` is left out of the listing.

    Args:
        root: The project root.

    Returns:
        The entries, sorted by path.
    """
    real_root = Path(os.path.realpath(root))
    entries: list[FileEntry] = []
    pending = [real_root]
    while pending:
        current = pending.pop()
        for child in current.iterdir():
            if child.name.startswith("."):
                continue
            real_child = Path(os.path.realpath(child))
            if not real_child.is_relative_to(real_root):
                continue
            rel = real_child.relative_to(real_root).as_posix()
            if real_child.is_dir():
                entries.append(FileEntry(path=rel, kind="dir", size=0))
                pending.append(real_child)
            elif real_child.is_file():
                try:
                    size = real_child.stat().st_size
                except OSError:
                    continue
                entries.append(FileEntry(path=rel, kind="file", size=size))
    entries.sort(key=lambda entry: entry.path)
    return entries


def read(root: Path, rel: str) -> tuple[str, str]:
    """`rel`'s current text and its content hash.

    Args:
        root: The project root.
        rel: The file's path relative to `root`.

    Returns:
        The text and its hash.

    Raises:
        PermissionError: `rel` resolves outside `root`.
        IsADirectoryError: `rel` names a directory.
        FileNotFoundError: Nothing is at `rel`.
    """
    resolved = resolve_in_project(root, rel)
    if resolved.is_dir():
        msg = f"{rel!r} is a directory, not a file"
        raise IsADirectoryError(msg)
    if not resolved.is_file():
        msg = f"no file at {rel!r}"
        raise FileNotFoundError(msg)
    content = resolved.read_text(encoding="utf-8")
    return content, short_content_hash(content)


def write(root: Path, rel: str, content: str, expected_hash: str | None) -> str:
    """Write `content` to `rel`, refusing a stale `expected_hash`.

    An `expected_hash` of `None` writes unconditionally. When it is given and the file exists, it is compared with the
    file's current hash first.

    Args:
        root: The project root.
        rel: The file's path relative to `root`.
        content: The file's new full content.
        expected_hash: The hash the file must have now, or `None`.

    Returns:
        The new content hash.

    Raises:
        PermissionError: `rel` resolves outside `root`.
        IsADirectoryError: `rel` names a directory.
        ValueError: `expected_hash` is given and does not match the file's current hash.
        FileNotFoundError: `rel`'s parent directory does not exist.
    """
    resolved = resolve_in_project(root, rel)
    if resolved.is_dir():
        msg = f"{rel!r} is a directory, not a file"
        raise IsADirectoryError(msg)
    if expected_hash is not None and resolved.is_file():
        current = resolved.read_text(encoding="utf-8")
        if short_content_hash(current) != expected_hash:
            msg = f"file {rel!r} has changed since it was read"
            raise ValueError(msg)
    if not resolved.parent.is_dir():
        msg = f"parent directory for {rel!r} does not exist"
        raise FileNotFoundError(msg)
    atomic_write(resolved, content)
    return short_content_hash(content)


def create(root: Path, rel: str) -> str:
    """Create an empty file at `rel`.

    Args:
        root: The project root.
        rel: The new file's path relative to `root`.

    Returns:
        The empty file's content hash.

    Raises:
        PermissionError: `rel` resolves outside `root`.
        FileExistsError: Something is already there.
        FileNotFoundError: `rel`'s parent directory does not exist.
    """
    resolved = resolve_in_project(root, rel)
    if resolved.exists():
        msg = f"{rel!r} already exists"
        raise FileExistsError(msg)
    if not resolved.parent.is_dir():
        msg = f"parent directory for {rel!r} does not exist"
        raise FileNotFoundError(msg)
    atomic_write(resolved, "")
    return short_content_hash("")


def create_dir(root: Path, rel: str) -> None:
    """Create an empty directory at `rel`.

    Args:
        root: The project root.
        rel: The new directory's path relative to `root`.

    Raises:
        PermissionError: `rel` resolves outside `root`.
        FileExistsError: Something is already there.
        FileNotFoundError: `rel`'s parent directory does not exist.
    """
    resolved = resolve_in_project(root, rel)
    if resolved.exists():
        msg = f"{rel!r} already exists"
        raise FileExistsError(msg)
    if not resolved.parent.is_dir():
        msg = f"parent directory for {rel!r} does not exist"
        raise FileNotFoundError(msg)
    resolved.mkdir()


def rename(root: Path, rel: str, to: str) -> None:
    """Rename the file at `rel` to `to`, carrying its note sidecar when it has one.

    Only a single file moves. A directory is refused.

    Args:
        root: The project root.
        rel: The file's path relative to `root`.
        to: Its new path relative to `root`.

    Raises:
        PermissionError: `rel` or `to` resolves outside `root`.
        IsADirectoryError: `rel` names a directory.
        FileNotFoundError: Nothing is at `rel`, or `to`'s parent directory does not exist.
        FileExistsError: Something is already at `to`, or `to` already has a note sidecar.
    """
    source = resolve_in_project(root, rel)
    if source.is_dir():
        msg = f"{rel!r} is a directory. Directory rename is not supported here"
        raise IsADirectoryError(msg)
    if not source.is_file():
        msg = f"no file at {rel!r}"
        raise FileNotFoundError(msg)
    destination = resolve_in_project(root, to)
    if destination.exists():
        msg = f"{to!r} already exists"
        raise FileExistsError(msg)
    if not destination.parent.is_dir():
        msg = f"parent directory for {to!r} does not exist"
        raise FileNotFoundError(msg)
    source_centralized = sidecar_path(root, source)
    destination_centralized = sidecar_path(root, destination)
    if destination_centralized.exists():
        # A sidecar already at the destination is ordinary in a project edited outside this app, and either outcome
        # is wrong. With a source sidecar the move drops the notes already there, and without one the renamed file
        # inherits them as its own. So a destination sidecar alone refuses the rename, before anything moves.
        msg = f"{to!r} already has a note sidecar"
        raise FileExistsError(msg)
    source.rename(destination)
    if source_centralized.is_file():
        destination_centralized.parent.mkdir(parents=True, exist_ok=True)
        source_centralized.rename(destination_centralized)


def delete(root: Path, rel: str) -> None:
    """Delete the file at `rel` and its note sidecar, when it has one.

    Only a single file is removed. A directory is refused.

    Args:
        root: The project root.
        rel: The file's path relative to `root`.

    Raises:
        PermissionError: `rel` resolves outside `root`.
        IsADirectoryError: `rel` names a directory.
        FileNotFoundError: Nothing is at `rel`.
    """
    resolved = resolve_in_project(root, rel)
    if resolved.is_dir():
        msg = f"{rel!r} is a directory. Directory delete is not supported here"
        raise IsADirectoryError(msg)
    if not resolved.is_file():
        msg = f"no file at {rel!r}"
        raise FileNotFoundError(msg)
    resolved.unlink()
    centralized = sidecar_path(root, resolved)
    if centralized.is_file():
        centralized.unlink()
