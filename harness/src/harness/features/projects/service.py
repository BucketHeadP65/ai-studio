"""The use cases over one project: its files, the owner notes on them and its skills.

Every function takes the project root, which belongs to the request. A builtin error a request can cause is raised as
a `ProjectError` subclass carrying the same message. Every other error passes through unchanged.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal

from engram.kb.contracts import add_reply, set_status

from harness.features.projects import domain
from harness.features.projects.exceptions import ProjectRequestRefusedError, ProjectTargetConflictError, ProjectTargetMissingError
from harness.features.projects.infrastructure import note_sidecars, project_files, skill_files
from harness.features.projects.read_models import FileNoteSummary

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from engram.kb.contracts import Note, NoteAnchor, NoteSheet

    from harness.features.projects.internal_contracts import SkillDirectories
    from harness.features.projects.read_models import FileEntry, SkillCard


@contextmanager
def _refused_on(
    *, refused: tuple[type[Exception], ...] = (), missing: tuple[type[Exception], ...] = (), conflict: tuple[type[Exception], ...] = ()
) -> Generator[None]:
    """Raise each listed builtin error as the slice error of its group, with the same message."""
    try:
        yield
    except refused as error:
        raise ProjectRequestRefusedError(str(error)) from error
    except missing as error:
        raise ProjectTargetMissingError(str(error)) from error
    except conflict as error:
        raise ProjectTargetConflictError(str(error)) from error


def _read_annotated_file(root: Path, path: str) -> tuple[str, Path]:
    """`path`'s current text and its resolved location under `root`, the first step of every owner note use case."""
    with _refused_on(refused=(PermissionError, IsADirectoryError), missing=(FileNotFoundError,)):
        content, _digest = project_files.read(root, path)
    return content, project_files.resolve_in_project(root, path)


def file_tree(root: Path) -> list[FileEntry]:
    """Every directory and file of the project, as its file tree shows them.

    Args:
        root: The project root.

    Returns:
        The entries, sorted by path.
    """
    return project_files.tree(root)


def file_content(root: Path, path: str) -> tuple[str, str]:
    """One file's current text and its content hash.

    Args:
        root: The project root.
        path: The file's path relative to `root`.

    Returns:
        The text and its hash.

    Raises:
        ProjectRequestRefusedError: `path` resolves outside `root` or names a directory.
        ProjectTargetMissingError: Nothing is at `path`.
    """
    with _refused_on(refused=(PermissionError, IsADirectoryError), missing=(FileNotFoundError,)):
        return project_files.read(root, path)


def is_obsidian_folder(root: Path) -> bool:
    """Whether the project folder holds `.obsidian/`.

    Args:
        root: The project root.

    Returns:
        True when `.obsidian/` is a directory under `root`.
    """
    return (root / ".obsidian").is_dir()


def write_file(root: Path, path: str, content: str, expected_hash: str | None) -> str:
    """Write one file's content, refusing a stale `expected_hash`.

    Args:
        root: The project root.
        path: The file's path relative to `root`.
        content: The file's new full content.
        expected_hash: The hash the file must have now, or `None` to write unconditionally.

    Returns:
        The new content hash.

    Raises:
        ProjectRequestRefusedError: `path` resolves outside `root` or names a directory.
        ProjectTargetMissingError: `path`'s parent directory does not exist.
        ProjectTargetConflictError: The file changed since the caller read it.
    """
    with _refused_on(refused=(PermissionError, IsADirectoryError), missing=(FileNotFoundError,), conflict=(ValueError,)):
        return project_files.write(root, path, content, expected_hash)


def create_file(root: Path, path: str) -> str:
    """Create an empty file, refusing a path that already exists rather than truncating it.

    Args:
        root: The project root.
        path: The new file's path relative to `root`.

    Returns:
        The empty file's content hash.

    Raises:
        ProjectRequestRefusedError: `path` resolves outside `root`.
        ProjectTargetMissingError: `path`'s parent directory does not exist.
        ProjectTargetConflictError: Something is already at `path`.
    """
    with _refused_on(refused=(PermissionError,), missing=(FileNotFoundError,), conflict=(FileExistsError,)):
        return project_files.create(root, path)


def create_directory(root: Path, path: str) -> None:
    """Create an empty directory, refusing a path that already exists.

    Args:
        root: The project root.
        path: The new directory's path relative to `root`.

    Raises:
        ProjectRequestRefusedError: `path` resolves outside `root`.
        ProjectTargetMissingError: `path`'s parent directory does not exist.
        ProjectTargetConflictError: Something is already at `path`.
    """
    with _refused_on(refused=(PermissionError,), missing=(FileNotFoundError,), conflict=(FileExistsError,)):
        project_files.create_dir(root, path)


def rename_file(root: Path, path: str, to: str) -> None:
    """Rename one file, carrying its note sidecar when it has one.

    Args:
        root: The project root.
        path: The file's path relative to `root`.
        to: Its new path relative to `root`.

    Raises:
        ProjectRequestRefusedError: `path` or `to` resolves outside `root`, or `path` names a directory.
        ProjectTargetMissingError: Nothing is at `path`, or `to`'s parent directory does not exist.
        ProjectTargetConflictError: Something is already at `to`, or `to` already has a note sidecar.
    """
    with _refused_on(refused=(PermissionError, IsADirectoryError), missing=(FileNotFoundError,), conflict=(FileExistsError,)):
        project_files.rename(root, path, to)


def delete_file(root: Path, path: str) -> None:
    """Delete one file and its note sidecar, when it has one.

    Args:
        root: The project root.
        path: The file's path relative to `root`.

    Raises:
        ProjectRequestRefusedError: `path` resolves outside `root` or names a directory.
        ProjectTargetMissingError: Nothing is at `path`.
    """
    with _refused_on(refused=(PermissionError, IsADirectoryError), missing=(FileNotFoundError,)):
        project_files.delete(root, path)


def notes_on(root: Path, path: str) -> tuple[str, NoteSheet]:
    """One file's current text and its owner notes.

    Args:
        root: The project root.
        path: The annotated file's path relative to `root`.

    Returns:
        The file's text and its note sheet.

    Raises:
        ProjectRequestRefusedError: `path` resolves outside `root` or names a directory.
        ProjectTargetMissingError: Nothing is at `path`.
    """
    content, resolved = _read_annotated_file(root, path)
    return content, note_sidecars.load_sheet(root, resolved)


def every_note(root: Path) -> list[tuple[Note, str, str]]:
    """Every owner note on every annotated file of the project, in path order.

    Args:
        root: The project root.

    Returns:
        One triple per note: the note, its file's path relative to `root`, and that file's current text.
    """
    return [
        (note, rel, project_files.file_text(file))
        for file, rel in note_sidecars.annotated_files(root)
        for note in note_sidecars.load_sheet(root, file).notes
    ]


def summarize(root: Path) -> list[FileNoteSummary]:
    """Every annotated file's note aggregate under `root`, sorted by path.

    The counts cover every note whatever its `archived` flag. `latest_agent_reply_at` is the most recent `Reply.at`
    across the file's notes among the replies whose `author` is `"agent"`, or `None` when the file has none.

    Args:
        root: The project root.

    Returns:
        One summary per annotated file.
    """
    summaries: list[FileNoteSummary] = []
    for file, rel in note_sidecars.annotated_files(root):
        sheet = note_sidecars.load_sheet(root, file)
        open_notes = sum(1 for note in sheet.notes if note.status == "open")
        resolved_notes = sum(1 for note in sheet.notes if note.status == "resolved")
        agent_replies = [reply.at for note in sheet.notes for reply in note.replies if reply.author == "agent"]
        summaries.append(
            FileNoteSummary(
                path=rel,
                open_notes=open_notes,
                resolved_notes=resolved_notes,
                latest_agent_reply_at=max(agent_replies) if agent_replies else None,
            )
        )
    return summaries


def add_note(root: Path, path: str, *, anchor: NoteAnchor, text: str) -> tuple[Note, str]:
    """Add an owner note to one file, minting only its id and its timestamp.

    Args:
        root: The project root.
        path: The annotated file's path relative to `root`.
        anchor: Where the note points.
        text: The note body.

    Returns:
        The new note, and the file's current text.

    Raises:
        ProjectRequestRefusedError: `path` resolves outside `root` or names a directory.
        ProjectTargetMissingError: Nothing is at `path`.
    """
    content, resolved = _read_annotated_file(root, path)
    sheet = note_sidecars.load_sheet(root, resolved)
    updated_sheet, note = domain.add_note(sheet, anchor=anchor, text=text)
    note_sidecars.save_sheet(root, resolved, updated_sheet)
    return note, content


def change_note(root: Path, path: str, note_id: str, *, text: str | None, archived: bool | None) -> tuple[Note, str]:
    """Change an owner note's text, its archived state or both.

    Args:
        root: The project root.
        path: The annotated file's path relative to `root`.
        note_id: The note to change.
        text: The new note body, or `None` to keep it.
        archived: The new archived state, or `None` to keep it.

    Returns:
        The changed note, and the file's current text.

    Raises:
        ProjectRequestRefusedError: `path` resolves outside `root` or names a directory.
        ProjectTargetMissingError: Nothing is at `path`, or no note has `note_id`.
    """
    content, resolved = _read_annotated_file(root, path)
    sheet = note_sidecars.load_sheet(root, resolved)
    with _refused_on(missing=(KeyError,)):
        updated_sheet, note = domain.replace_note(sheet, note_id, text=text, archived=archived)
    note_sidecars.save_sheet(root, resolved, updated_sheet)
    return note, content


def delete_note(root: Path, path: str, note_id: str) -> None:
    """Delete one owner note.

    Args:
        root: The project root.
        path: The annotated file's path relative to `root`.
        note_id: The note to delete.

    Raises:
        ProjectRequestRefusedError: `path` resolves outside `root` or names a directory.
        ProjectTargetMissingError: Nothing is at `path`, or no note has `note_id`.
    """
    _content, resolved = _read_annotated_file(root, path)
    sheet = note_sidecars.load_sheet(root, resolved)
    with _refused_on(missing=(KeyError,)):
        updated_sheet = domain.remove_note(sheet, note_id)
    note_sidecars.save_sheet(root, resolved, updated_sheet)


def reply_as_owner(root: Path, path: str, note_id: str, text: str) -> tuple[Note, str]:
    """Add an owner reply to a note's thread.

    Args:
        root: The project root.
        path: The annotated file's path relative to `root`.
        note_id: The note whose thread the reply joins.
        text: The reply body.

    Returns:
        The note with its new reply, and the file's current text.

    Raises:
        ProjectRequestRefusedError: `path` resolves outside `root` or names a directory.
        ProjectTargetMissingError: Nothing is at `path`, or no note has `note_id`.
    """
    content, resolved = _read_annotated_file(root, path)
    sheet = note_sidecars.load_sheet(root, resolved)
    with _refused_on(missing=(KeyError,)):
        updated_sheet, note = add_reply(sheet, note_id, author="owner", text=text)
    note_sidecars.save_sheet(root, resolved, updated_sheet)
    return note, content


def change_reply(root: Path, path: str, note_id: str, reply_index: int, *, text: str) -> tuple[Note, str]:
    """Change an existing owner reply's text. An agent reply is refused.

    Args:
        root: The project root.
        path: The annotated file's path relative to `root`.
        note_id: The note whose thread holds the reply.
        reply_index: The reply's 0-based position in the thread.
        text: The reply's new body.

    Returns:
        The note with the changed reply, and the file's current text.

    Raises:
        ProjectRequestRefusedError: `path` resolves outside `root` or names a directory, or the agent wrote the reply.
        ProjectTargetMissingError: Nothing is at `path`, no note has `note_id`, or no reply is at `reply_index`.
    """
    content, resolved = _read_annotated_file(root, path)
    sheet = note_sidecars.load_sheet(root, resolved)
    with _refused_on(refused=(PermissionError,), missing=(KeyError,)):
        updated_sheet, note = domain.replace_reply(sheet, note_id, reply_index, text=text)
    note_sidecars.save_sheet(root, resolved, updated_sheet)
    return note, content


def set_note_status(root: Path, path: str, note_id: str, status: Literal["open", "resolved"]) -> tuple[Note, str]:
    """Mark a note's thread resolved, or open it again.

    Args:
        root: The project root.
        path: The annotated file's path relative to `root`.
        note_id: The note whose thread changes.
        status: The thread's new status.

    Returns:
        The note with its new status, and the file's current text.

    Raises:
        ProjectRequestRefusedError: `path` resolves outside `root` or names a directory.
        ProjectTargetMissingError: Nothing is at `path`, or no note has `note_id`.
    """
    content, resolved = _read_annotated_file(root, path)
    sheet = note_sidecars.load_sheet(root, resolved)
    with _refused_on(missing=(KeyError,)):
        updated_sheet, note = set_status(sheet, note_id, status)
    note_sidecars.save_sheet(root, resolved, updated_sheet)
    return note, content


def reply_as_agent(root: Path, path: str, note_id: str, text: str) -> None:
    """Add the agent's reply to a note's thread on an annotated file.

    Args:
        root: The root the agent's file tools read from.
        path: The annotated file's path relative to `root`.
        note_id: The note whose thread the reply joins.
        text: The reply body.

    Raises:
        ProjectRequestRefusedError: `path` resolves outside `root`.
        ProjectTargetMissingError: No annotated file is at `path`, or no note has `note_id`.
    """
    with _refused_on(refused=(PermissionError,)):
        resolved = project_files.resolve_in_project(root, path)
    if not resolved.is_file() or not note_sidecars.has_sidecar(root, resolved):
        msg = f"no annotated file at {path!r}"
        raise ProjectTargetMissingError(msg)
    sheet = note_sidecars.load_sheet(root, resolved)
    with _refused_on(missing=(KeyError,)):
        updated_sheet, _note = add_reply(sheet, note_id, author="agent", text=text)
    note_sidecars.save_sheet(root, resolved, updated_sheet)


def annotated_text(root: Path, path: str) -> tuple[str, NoteSheet] | None:
    """An annotated file's current text and its owner notes, or `None` when `path` is not an annotated file.

    Undecodable bytes are replaced rather than raised, as the file tools read the file, so an annotated file the
    agent could read stays readable. A path outside `root` answers `None`. It never raises a refusal.

    Args:
        root: The root the agent's file tools read from.
        path: The file's path relative to `root`.

    Returns:
        The text and the note sheet, or `None`.
    """
    try:
        resolved = project_files.resolve_in_project(root, path)
    except PermissionError:
        return None
    if not resolved.is_file() or not note_sidecars.has_sidecar(root, resolved):
        return None
    sheet = note_sidecars.load_sheet(root, resolved)
    return project_files.file_text_replacing_undecodable(resolved), sheet


def skills(root: Path, config: SkillDirectories) -> list[SkillCard]:
    """The project's skill catalog: every configured skill directory, the harness's own and engram's own.

    Args:
        root: The project root.
        config: The project's configuration.

    Returns:
        Every skill's card, valid or not.
    """
    return skill_files.catalog(root, config)


def skill_body(root: Path, library: str, name: str) -> tuple[str, str, bool]:
    """One skill's full `SKILL.md`, its hash, and whether it can be saved back.

    Args:
        root: The project root.
        library: The skill directory, project-relative or `"builtin"`.
        name: The skill's directory name.

    Returns:
        The content, its hash, and whether it can be saved back.

    Raises:
        ProjectRequestRefusedError: The skill's location escapes `root`.
        ProjectTargetMissingError: No `SKILL.md` is there.
    """
    with _refused_on(refused=(PermissionError,), missing=(FileNotFoundError,)):
        return skill_files.read_body(root, library, name)


def scaffold_skill(root: Path, library: str, name: str) -> str:
    """Create a new skill's `SKILL.md` stub in a configured skill directory.

    Args:
        root: The project root.
        library: The configured skill directory, project-relative.
        name: The new skill's directory name.

    Returns:
        The new `SKILL.md`'s path relative to the project root.

    Raises:
        ProjectRequestRefusedError: `library` or `name` resolves outside `root`.
        ProjectTargetConflictError: A skill already exists there.
    """
    with _refused_on(refused=(PermissionError,), conflict=(FileExistsError,)):
        return skill_files.scaffold(root, library, name)


def write_skill_body(root: Path, library: str, name: str, content: str, expected_hash: str | None) -> str:
    """Write one skill's `SKILL.md`, refusing a stale `expected_hash`.

    The caller refuses the harness's own skill directory and an unconfigured one before calling.

    Args:
        root: The project root.
        library: The configured skill directory, project-relative.
        name: The skill's directory name.
        content: The skill's new full content.
        expected_hash: The hash the file must have now, or `None` to write unconditionally.

    Returns:
        The new content hash.

    Raises:
        ProjectRequestRefusedError: The skill's location escapes `root`.
        ProjectTargetMissingError: The skill's directory does not exist.
        ProjectTargetConflictError: The file changed since the caller read it.
    """
    with _refused_on(refused=(PermissionError,), missing=(FileNotFoundError,), conflict=(ValueError,)):
        return skill_files.write_body(root, library, name, content, expected_hash)
