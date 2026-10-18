"""The request and response bodies of the project routes: files, owner notes and skills."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from engram.kb.contracts import NoteAnchor, Reply
from pydantic import BaseModel, ConfigDict, Field

from harness.features.projects.read_models import FileEntry, FileNoteSummary, SkillCard


class FileTreeResult(BaseModel):
    """A project's file tree."""

    entries: list[FileEntry] = Field(description="Every dir and file under the project root, filtered per the tree rules.")


class FileReadResult(BaseModel):
    """A file's current content, its hash, and whether the project folder holds `.obsidian/`."""

    content: str = Field(description="The file's full text.")
    hash: str = Field(description="Its current content hash.")
    obsidian: bool = Field(description="Whether the project root has a `.obsidian/` directory.")


class FileWriteRequest(BaseModel):
    """A PUT to one file: its new content, and the hash it must currently have."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Root-relative file path.")
    content: str = Field(description="The file's new full content.")
    expected_hash: str | None = Field(
        default=None, description="Current hash to check before writing. Absent or null writes unconditionally."
    )


class FileCreateRequest(BaseModel):
    """A create: where the new, empty file goes."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Root-relative path for the new file. Nothing may be there yet.")


class DirCreateRequest(BaseModel):
    """A create: where the new, empty directory goes."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Root-relative path for the new directory. Nothing may be there yet.")


class FileWriteResult(BaseModel):
    """A write's or a create's outcome: the file's content hash."""

    hash: str = Field(description="The new content hash.")


class FileRenameRequest(BaseModel):
    """A rename: the file's current path, and where it goes."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Root-relative path of the file being renamed.")
    to: str = Field(description="Its new root-relative path.")


class NoteView(BaseModel):
    """One owner note, its anchor resolved fresh against the annotated file's current text."""

    id: str = Field(description="Note id.")
    anchor: NoteAnchor = Field(description="Where the note points.")
    text: str = Field(description="The note body.")
    created_at: datetime = Field(description="When it was placed.")
    archived: bool = Field(description="Whether it is hidden from the agent's render.")
    replies: list[Reply] = Field(description="The thread's replies, in the order they were posted.")
    status: Literal["open", "resolved"] = Field(description="Whether the thread is still open.")
    path: str = Field(description="Root-relative path of the annotated file.")
    orphaned: bool = Field(description="Whether the anchor no longer resolves in the current text.")
    line: int | None = Field(description="1-based line of the resolved anchor. `None` when orphaned.")
    start: int | None = Field(description="Character offset the anchor's span starts at. `None` when orphaned.")
    end: int | None = Field(description="Character offset just past the anchor's span. `None` when orphaned.")


class NotesListResult(BaseModel):
    """Every owner note a listing covers, each anchor resolved fresh."""

    notes: list[NoteView] = Field(description="One file's notes, or every annotated file's when none was named.")


class NotesSummaryResult(BaseModel):
    """Every annotated file's note aggregate."""

    files: list[FileNoteSummary] = Field(description="One entry per annotated file, sorted by path.")


class NoteResult(BaseModel):
    """One owner note, as it now stands."""

    note: NoteView = Field(description="The note.")


class NoteCreateRequest(BaseModel):
    """A new owner note: which file, where on it, and its text."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Root-relative path of the annotated file.")
    anchor: NoteAnchor = Field(description="Where the note points, computed by the client.")
    text: str = Field(description="The note body.")


class NotePatchRequest(BaseModel):
    """An edit to one owner note: its file, and the fields changing."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Root-relative path of the annotated file.")
    text: str | None = Field(default=None, description="New note body, when changing it.")
    archived: bool | None = Field(default=None, description="New archived state, when changing it.")


class NoteReplyRequest(BaseModel):
    """A reply into a note's thread: its file, and the reply body."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Root-relative path of the annotated file.")
    text: str = Field(description="The reply body.")


class NoteReplyPatchRequest(BaseModel):
    """An edit to one existing reply: its file, its position in the thread, and its new text."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Root-relative path of the annotated file.")
    reply_index: int = Field(description="0-based position of the reply within the note's thread.")
    text: str = Field(description="The reply's new body.")


class SkillsListResult(BaseModel):
    """The project's skill catalog and the skill directories it was built from."""

    skills: list[SkillCard] = Field(description="Every discovered skill, valid or not.")
    libraries: list[str] = Field(description="The project's configured library dirs plus 'builtin'.")


class SkillScaffoldRequest(BaseModel):
    """A new skill: which configured skill directory, and its name."""

    model_config = ConfigDict(extra="forbid")

    library: str = Field(description="One of the project's configured skill library dirs.")
    name: str = Field(description="The new skill's directory name.")


class SkillScaffoldResult(BaseModel):
    """A scaffolded skill's location."""

    path: str = Field(description="The new SKILL.md, relative to the project root.")


class SkillBodyResult(BaseModel):
    """A skill's full `SKILL.md` body."""

    content: str = Field(description="The skill's full markdown, frontmatter included.")
    hash: str = Field(description="Its current content hash.")
    editable: bool = Field(description="Whether this skill's file can be saved back through `put_skill_body`.")


class SkillBodyWriteRequest(BaseModel):
    """A write to one skill's body: which skill directory and skill, its new content, and the hash it must currently have."""

    model_config = ConfigDict(extra="forbid")

    library: str = Field(description="One of the project's configured skill library dirs. Never 'builtin'.")
    name: str = Field(description="The skill's directory name.")
    content: str = Field(description="The skill's new full content.")
    expected_hash: str | None = Field(
        default=None, description="Current hash to check before writing. Absent or null writes unconditionally."
    )
