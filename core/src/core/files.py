"""Filesystem helpers: atomic writes, an atomic create, a cooperative lock, filename slugs, and path guards.

An atomic write stages the bytes in a temp file in the destination's own directory and swaps it into place,
so a reader never sees a half-written file and a crash leaves the original intact. `atomic_create` writes a
name only when it is free and refuses one that is taken.
"""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import zipfile

from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

# Characters illegal in a filename: OS path separators and the characters Obsidian reserves for link
# syntax. A colon is handled separately and becomes a spaced hyphen, so 'Title: Subtitle' reads naturally.
_ILLEGAL = re.compile(r'[\\/*?"<>|#^\[\]]')
_WHITESPACE = re.compile(r"\s+")


def _fresh_temp(path: Path) -> tuple[Path, int]:
    """A hidden temp file beside `path` under a random name, created now and never over anything already there."""
    temp = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
    return temp, os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)


def atomic_write(path: Path, text: str) -> None:
    """Put exactly `text` at `path`, whole, leaving whatever was there until every byte is on disk.

    The temp file is a hidden sibling of `path`, created fresh and never opened over anything
    already at its name. It is flushed to disk before the swap, and the directory entry after it.
    The file takes the default mode the umask allows. Two writers of the same `path` each swap
    their own temp file in, and the last swap wins whole.

    Args:
        path: The file to replace or create.
        text: The text to write, encoded as UTF-8.
    """
    _replace_durably(path, text.encode("utf-8"))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Put exactly `data` at `path`, whole, with the same guarantees as `atomic_write`.

    Args:
        path: The file to replace or create.
        data: The bytes to write.
    """
    _replace_durably(path, data)


def _replace_durably(path: Path, data: bytes) -> None:
    """Stage `data` in a freshly created hidden temp file beside `path`, flush it, swap it in and flush the directory entry."""
    temp, descriptor = _fresh_temp(path)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
        fsync_directory(path.parent)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def fsync_directory(directory: Path) -> None:
    """Flush `directory`'s own entry, so a name created or replaced inside it survives a crash.

    Args:
        directory: The directory whose entry is flushed.
    """
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_create(path: Path, data: bytes) -> None:
    """Put exactly `data` at `path`, and refuse outright if anything already holds that name.

    Every byte is written and flushed to a fresh temp file in `path`'s own directory before the name is
    claimed by a hard link, so `path` is never a short file and a writer racing this one loses the
    link rather than the bytes. The directory entry is flushed once the link is made. The temp file
    is removed whether the link was made or not.

    Args:
        path: The file to create.
        data: The bytes to write.

    Raises:
        FileExistsError: `path` already exists. Whatever is there is left exactly as it was, and
            nothing is written. A caller that can accept an identical file compares the bytes
            itself, since the two cases are one refusal here.
        OSError: The bytes could not be staged, or the filesystem supports no hard link. Neither
            leaves a target behind.
    """
    temp, descriptor = _fresh_temp(path)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        path.hardlink_to(temp)
        fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def exclusive_lock(path: Path) -> Generator[None]:
    """Hold `path` as a cooperative exclusive lock for the block, creating it when it is absent.

    The lock binds processes on one POSIX device only. It is not a network filesystem guarantee.

    Args:
        path: The lock file.

    Yields:
        Nothing. The lock is held until the block exits.
    """
    with path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def safe_slug(title: str, max_len: int = 80) -> str:
    """A filesystem-safe note stem for `title`, with illegal characters replaced, trimmed and capped.

    A colon becomes a spaced hyphen and any other illegal character becomes a hyphen. The result is
    never empty. A blank or all-illegal title gives `"untitled"`.

    Args:
        title: The note title.
        max_len: The most characters the stem may hold.

    Returns:
        The stem.
    """
    cleaned = _ILLEGAL.sub("-", title.replace(":", " - "))
    cleaned = _WHITESPACE.sub(" ", cleaned).strip().strip(" .-").strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .-")
    return cleaned or "untitled"


def guarded_child(root: Path, relative: str) -> Path:
    """Resolve `relative` under `root`, rejecting anything that would escape `root`.

    Args:
        root: The directory the result must stay under.
        relative: The POSIX-style relative path.

    Returns:
        `root` joined with `relative`.

    Raises:
        ValueError: If `relative` is absolute or contains a `..` component
            that would let the result escape `root` (path-traversal guard).
    """
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        msg = f"path escapes {root}: {relative!r}"
        raise ValueError(msg)
    return root / relative


def extract_zip(archive: Path, dest: Path, *, max_bytes: int) -> list[Path]:
    """Extract every file in `archive` under `dest`, refusing a member that would land outside it.

    Every member's path is checked before anything is written, so a refused archive writes nothing.
    Extraction stops once the files written pass `max_bytes`, counted as they are written, not as the
    archive claims.

    Args:
        archive: The zip file to read.
        dest: The directory the files land under, created when missing.
        max_bytes: The most bytes the extracted files may hold together.

    Returns:
        The path of every file written, in archive order.

    Raises:
        ValueError: A member is absolute or climbs out of `dest`, or the extracted files pass `max_bytes`.
    """
    root = dest.resolve()
    written: list[Path] = []
    total = 0
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        for member in members:
            target = (root / member.filename).resolve()
            if target != root and root not in target.parents:
                msg = f"{archive.name} holds {member.filename!r}, which would land outside {dest}"
                raise ValueError(msg)
        for member in members:
            target = (root / member.filename).resolve()
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, target.open("wb") as sink:
                while chunk := source.read(1 << 20):
                    total += len(chunk)
                    if total > max_bytes:
                        msg = f"{archive.name} expands past {max_bytes} bytes"
                        raise ValueError(msg)
                    sink.write(chunk)
            written.append(target)
    return written


def has_hidden_component(path: Path) -> bool:
    """Whether any segment of the resolved `path` is a dot-prefixed name, such as `.ssh` or `.config`.

    The path is resolved first, with symlinks followed and `..` and `.` collapsed, so a relative or
    symlinked route to the same hidden directory answers the same.

    Args:
        path: The path to check.

    Returns:
        True when a segment of the resolved path starts with a dot.
    """
    return any(part.startswith(".") for part in path.resolve().parts)
