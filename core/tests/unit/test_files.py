"""Filesystem helpers: atomic write, slug sanitisation, path-traversal guard."""

from __future__ import annotations

import errno
import os
import re
import stat
import threading
import zipfile

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from core.files import atomic_create, atomic_write, atomic_write_bytes, extract_zip, guarded_child, safe_slug

if TYPE_CHECKING:
    from collections.abc import Callable


def test_atomic_write_writes_text_and_leaves_no_temp_file(tmp_path: Path):
    target = tmp_path / "note.md"
    atomic_write(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_overwrites_existing_content(tmp_path: Path):
    target = tmp_path / "note.md"
    atomic_write(target, "first")
    atomic_write(target, "second")
    assert target.read_text(encoding="utf-8") == "second"


def test_concurrent_atomic_writes_of_one_path_all_land_and_leave_no_temp_files(tmp_path: Path):
    """Two writers racing on the same file each swap their own temp in; no write fails and nothing is left behind."""
    import threading

    target = tmp_path / "shared.md"
    errors: list[BaseException] = []

    def writer(text: str) -> None:
        try:
            for _ in range(200):
                atomic_write(target, text)
        except BaseException as error:  # noqa: BLE001 -- the test records any failure to assert on it
            errors.append(error)

    threads = [threading.Thread(target=writer, args=(f"writer {n}\n",)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert target.read_text(encoding="utf-8") in {f"writer {n}\n" for n in range(4)}
    assert sorted(child.name for child in tmp_path.iterdir()) == ["shared.md"]


def test_atomic_write_bytes_round_trips_binary(tmp_path: Path):
    target = tmp_path / "figure.png"
    payload = b"\x89PNG\r\n\x1a\n"
    atomic_write_bytes(target, payload)
    assert target.read_bytes() == payload


def _write_text(path: Path) -> None:
    atomic_write(path, "durable")


def _write_bytes(path: Path) -> None:
    atomic_write_bytes(path, b"durable")


@pytest.mark.parametrize("write", [_write_text, _write_bytes])
def test_atomic_writes_flush_the_file_before_the_swap_and_the_directory_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write: Callable[[Path], None]
):
    """A crash at any point leaves either the old file or the new one, whole."""
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def fsync(descriptor: int) -> None:
        events.append("fsync directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "fsync file")
        real_fsync(descriptor)

    def replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    write(tmp_path / "target.json")
    assert events == ["fsync file", "replace", "fsync directory"]
    assert sorted(child.name for child in tmp_path.iterdir()) == ["target.json"]


@pytest.mark.parametrize("write", [_write_text, _write_bytes])
def test_atomic_writes_create_a_fresh_hidden_temp_file_exclusively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write: Callable[[Path], None]
):
    """The temp file is created, never opened over something already at its name."""
    opened: list[tuple[str, int]] = []
    real_open = os.open

    def recording_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        opened.append((os.fspath(path), flags))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", recording_open)
    write(tmp_path / "target.json")
    write(tmp_path / "target.json")
    temps = [(path, flags) for path, flags in opened if path.endswith(".tmp")]
    assert len(temps) == 2
    for path, flags in temps:
        assert re.fullmatch(r"\.target\.json\.[0-9a-f]{32}\.tmp", Path(path).name)
        assert flags & os.O_CREAT
        assert flags & os.O_EXCL
    assert temps[0][0] != temps[1][0], "each write draws its own temp name"


def test_an_atomic_write_that_fails_to_swap_leaves_the_original_and_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "target.json"
    target.write_text("original", encoding="utf-8")

    def refuse(*_args: object, **_kwargs: object) -> None:
        msg = "the swap failed"
        raise OSError(errno.EIO, msg)

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="swap failed"):
        atomic_write(target, "never lands")
    assert target.read_text(encoding="utf-8") == "original"
    assert sorted(child.name for child in tmp_path.iterdir()) == ["target.json"]


def test_safe_slug_replaces_colon_with_dash_separator():
    assert safe_slug("Title: Subtitle") == "Title - Subtitle"


def test_safe_slug_replaces_illegal_path_and_obsidian_chars_with_dashes():
    # Each reserved char (path separators + Obsidian link chars) maps to a dash; runs are
    # NOT collapsed.
    slug = safe_slug('a/b*c"<>|#^[]d')
    assert slug.startswith("a-b-c")
    assert slug.endswith("d")
    assert not set(slug) & set('/*"<>|#^[]')  # no reserved char survives


def test_safe_slug_caps_length_and_falls_back_to_untitled():
    assert len(safe_slug("x" * 200, max_len=80)) == 80
    assert safe_slug("   ") == "untitled"
    assert safe_slug("///") == "untitled"


def test_guarded_child_resolves_a_normal_relative_path(tmp_path: Path):
    assert guarded_child(tmp_path, "sub/figure.png") == tmp_path / "sub/figure.png"


@pytest.mark.parametrize("escape", ["../secret", "sub/../../secret", "/etc/passwd"])
def test_guarded_child_rejects_traversal_and_absolute_paths(tmp_path: Path, escape: str):
    with pytest.raises(ValueError, match="escapes"):
        guarded_child(tmp_path, escape)


def test_atomic_create_publishes_complete_bytes_into_an_absent_target(tmp_path: Path):
    target = tmp_path / "note.md"
    atomic_create(target, b"every byte\n")
    assert target.read_bytes() == b"every byte\n"
    assert sorted(child.name for child in tmp_path.iterdir()) == ["note.md"]


def test_atomic_create_preserves_an_existing_target_and_refuses(tmp_path: Path):
    target = tmp_path / "note.md"
    target.write_bytes(b"what was already here")
    with pytest.raises(FileExistsError):
        atomic_create(target, b"what retention would have written")
    assert target.read_bytes() == b"what was already here"
    assert sorted(child.name for child in tmp_path.iterdir()) == ["note.md"]


def test_atomic_create_leaves_no_target_and_no_staging_when_the_link_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A link that cannot be made leaves the name free for the next attempt, not a short file at it."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        msg = "this filesystem supports no hard links"
        raise OSError(errno.EPERM, msg)

    monkeypatch.setattr(os, "link", refuse)
    target = tmp_path / "note.md"
    with pytest.raises(OSError, match="hard link"):
        atomic_create(target, b"never published")
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_atomic_create_never_undoes_an_ordinary_replacement_that_landed_first(tmp_path: Path):
    """A retry meets the bytes somebody else swapped in, and refuses rather than restoring its own."""
    target = tmp_path / "note.md"
    atomic_create(target, b"the retained note")
    atomic_write(target, "the owner's edit")
    with pytest.raises(FileExistsError):
        atomic_create(target, b"the retained note")
    assert target.read_text(encoding="utf-8") == "the owner's edit"
    assert sorted(child.name for child in tmp_path.iterdir()) == ["note.md"]


def test_atomic_create_never_writes_through_a_file_already_at_a_temp_name(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    planted = tmp_path / f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    planted.write_bytes(b"not mine")

    atomic_create(target, b"mine")

    assert target.read_bytes() == b"mine"
    assert planted.read_bytes() == b"not mine"


def _zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as bundle:
        for name, data in members.items():
            bundle.writestr(name, data)
    return path


def test_extract_zip_writes_every_member_under_the_destination(tmp_path: Path):
    archive = _zip(tmp_path / "a.zip", {"set/corpus.jsonl": b"{}\n", "set/qrels/test.tsv": b"q\td\t1\n"})

    written = extract_zip(archive, tmp_path / "out", max_bytes=1_000)

    assert sorted(path.relative_to(tmp_path / "out").as_posix() for path in written) == ["set/corpus.jsonl", "set/qrels/test.tsv"]
    assert (tmp_path / "out" / "set" / "qrels" / "test.tsv").read_bytes() == b"q\td\t1\n"


def test_extract_zip_refuses_a_member_that_leaves_the_destination_and_writes_nothing(tmp_path: Path):
    archive = _zip(tmp_path / "a.zip", {"ok.txt": b"x", "../escape.txt": b"x"})

    with pytest.raises(ValueError, match=r"escape\.txt"):
        extract_zip(archive, tmp_path / "out", max_bytes=1_000)

    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "out" / "ok.txt").exists()


def test_extract_zip_stops_past_the_size_cap(tmp_path: Path):
    archive = _zip(tmp_path / "a.zip", {"big.txt": b"x" * 5_000})

    with pytest.raises(ValueError, match="1000 bytes"):
        extract_zip(archive, tmp_path / "out", max_bytes=1_000)
