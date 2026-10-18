"""The project routes: one project's files, the owner notes on them and its skills, by project id.

`{project_id}` resolves to a root through the injected `ProjectIndex`. An id it does not know answers 404 in each
route's own body shape. A root that no longer exists on disk raises the shared `MissingProjectRootError`, which the
app-level handler this process installs answers with 410.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from engram.kb.contracts import Note
from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from wireup.integration.fastapi import WireupRoute

from harness import session as session_state
from harness.features.projects import service
from harness.features.projects.api_models import (
    DirCreateRequest,
    FileCreateRequest,
    FileReadResult,
    FileRenameRequest,
    FileTreeResult,
    FileWriteRequest,
    FileWriteResult,
    NoteCreateRequest,
    NotePatchRequest,
    NoteReplyPatchRequest,
    NoteReplyRequest,
    NoteResult,
    NotesListResult,
    NotesSummaryResult,
    NoteView,
    SkillBodyResult,
    SkillBodyWriteRequest,
    SkillScaffoldRequest,
    SkillScaffoldResult,
    SkillsListResult,
)
from harness.features.projects.domain import resolve_anchor
from harness.features.projects.exceptions import (
    ProjectError,
    ProjectRequestRefusedError,
    ProjectTargetConflictError,
    ProjectTargetMissingError,
)
from harness.features.projects.infrastructure.skill_files import BUILTIN_SKILL_DIRECTORY
from harness.features.sessions.contracts import project_config
from harness.session import ProjectIndex

_UNKNOWN_PROJECT = "no project with that id here"


def _unknown_project() -> JSONResponse:
    """The refusal every route in this controller gives for an id nothing knows."""
    return JSONResponse({"detail": _UNKNOWN_PROJECT}, status_code=404)


def _note_view(note: Note, path: str, text: str) -> NoteView:
    """`note` as the wire shape, its anchor resolved fresh against `text`."""
    hit = resolve_anchor(text, note.anchor)
    return NoteView(
        id=note.id,
        anchor=note.anchor,
        text=note.text,
        created_at=note.created_at,
        archived=note.archived,
        replies=list(note.replies),
        status=note.status,
        path=path,
        orphaned=hit is None,
        line=hit.line if hit is not None else None,
        start=hit.offset if hit is not None else None,
        end=hit.end if hit is not None else None,
    )


def _skill_write_target_refusal(root: Path, library: str) -> JSONResponse | None:
    """The refusal for writing to `library`, or `None` when it is one of the project's configured skill directories."""
    if library == BUILTIN_SKILL_DIRECTORY:
        return JSONResponse({"detail": "builtin skills are read-only"}, status_code=403)
    config = project_config(root)
    if library not in config.skills:
        return JSONResponse({"detail": f"{library!r} is not a configured skill library"}, status_code=400)
    return None


_REFUSAL_STATUS: dict[type[ProjectError], int] = {
    ProjectRequestRefusedError: 400,
    ProjectTargetMissingError: 404,
    ProjectTargetConflictError: 409,
}


def _refusal(error: ProjectError) -> JSONResponse:
    """The answer to a refused request: the status its error type maps to, and its message as `detail`."""
    return JSONResponse({"detail": str(error)}, status_code=_REFUSAL_STATUS[type(error)])


class ProjectsController:
    """One family, every route under one project's own id."""

    router = APIRouter(prefix="/api/projects", tags=["projects"], route_class=WireupRoute)

    def __init__(self, projects: ProjectIndex) -> None:
        """Hold what `{project_id}` resolves through.

        Args:
            projects: The index that answers a project id's root.
        """
        self._projects = projects

    async def _project_root(self, project_id: str) -> Path | None:
        """`project_id`'s root, or `None` for an unknown id, raising `MissingProjectRootError` when its folder is gone."""
        root = await self._projects.root_of(project_id)
        if root is None:
            return None
        if not root.is_dir():
            raise session_state.MissingProjectRootError(root)
        return root

    @router.get("/{project_id}/files")
    async def get_files_tree(self, project_id: str) -> JSONResponse:
        """The project's file tree."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        result = FileTreeResult(entries=service.file_tree(root))
        return JSONResponse(result.model_dump(mode="json"))

    @router.get("/{project_id}/file")
    async def get_file(self, project_id: str, path: str) -> JSONResponse:
        """One file's content, its hash, and whether the project folder holds `.obsidian/`."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        try:
            content, digest = service.file_content(root, path)
        except ProjectError as error:
            return _refusal(error)
        obsidian = service.is_obsidian_folder(root)
        result = FileReadResult(content=content, hash=digest, obsidian=obsidian)
        return JSONResponse(result.model_dump(mode="json"))

    @router.put("/{project_id}/file")
    async def put_file(self, project_id: str, body: FileWriteRequest) -> JSONResponse:
        """Write one file's content, refusing a stale `expected_hash`."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        try:
            digest = service.write_file(root, body.path, body.content, body.expected_hash)
        except ProjectError as error:
            return _refusal(error)
        return JSONResponse(FileWriteResult(hash=digest).model_dump(mode="json"))

    @router.post("/{project_id}/file/create")
    async def create_file(self, project_id: str, body: FileCreateRequest) -> JSONResponse:
        """Create an empty file, refusing a path that already exists rather than truncating it."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        try:
            digest = service.create_file(root, body.path)
        except ProjectError as error:
            return _refusal(error)
        return JSONResponse(FileWriteResult(hash=digest).model_dump(mode="json"))

    @router.post("/{project_id}/dir/create", status_code=204)
    async def create_directory(self, project_id: str, body: DirCreateRequest) -> Response:
        """Create an empty directory, refusing a path that already exists."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        try:
            service.create_directory(root, body.path)
        except ProjectError as error:
            return _refusal(error)
        return Response(status_code=204)

    @router.post("/{project_id}/file/rename", status_code=204)
    async def rename_file(self, project_id: str, body: FileRenameRequest) -> Response:
        """Rename one file, carrying its note sidecar when it has one."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        try:
            service.rename_file(root, body.path, body.to)
        except ProjectError as error:
            return _refusal(error)
        return Response(status_code=204)

    @router.delete("/{project_id}/file", status_code=204)
    async def delete_file(self, project_id: str, path: str) -> Response:
        """Delete one file and its note sidecar, when it has one."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        try:
            service.delete_file(root, path)
        except ProjectError as error:
            return _refusal(error)
        return Response(status_code=204)

    @router.get("/{project_id}/notes")
    async def get_notes(self, project_id: str, path: str | None = None) -> JSONResponse:
        """The owner notes on `path` when given, or on every annotated file otherwise, each anchor resolved fresh."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        if path is not None:
            try:
                content, sheet = service.notes_on(root, path)
            except ProjectError as error:
                return _refusal(error)
            views = [_note_view(note, path, content) for note in sheet.notes]
        else:
            views = [_note_view(note, rel, text) for note, rel, text in service.every_note(root)]
        return JSONResponse(NotesListResult(notes=views).model_dump(mode="json"))

    @router.get("/{project_id}/notes/summary")
    async def get_notes_summary(self, project_id: str) -> JSONResponse:
        """Per-file note aggregates over every annotated file: open and resolved counts and the latest agent reply."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        result = NotesSummaryResult(files=service.summarize(root))
        return JSONResponse(result.model_dump(mode="json"))

    @router.post("/{project_id}/notes")
    async def post_note(self, project_id: str, body: NoteCreateRequest) -> JSONResponse:
        """Add an owner note. The server mints only its id and its timestamp."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        try:
            note, content = service.add_note(root, body.path, anchor=body.anchor, text=body.text)
        except ProjectError as error:
            return _refusal(error)
        return JSONResponse(NoteResult(note=_note_view(note, body.path, content)).model_dump(mode="json"))

    @router.patch("/{project_id}/notes/{note_id}")
    async def patch_note(self, project_id: str, note_id: str, body: NotePatchRequest) -> JSONResponse:
        """Change an owner note's text, its archived state or both."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        try:
            note, content = service.change_note(root, body.path, note_id, text=body.text, archived=body.archived)
        except ProjectError as error:
            return _refusal(error)
        return JSONResponse(NoteResult(note=_note_view(note, body.path, content)).model_dump(mode="json"))

    @router.delete("/{project_id}/notes/{note_id}", status_code=204)
    async def delete_note(self, project_id: str, note_id: str, path: str) -> Response:
        """Delete one owner note."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        try:
            service.delete_note(root, path, note_id)
        except ProjectError as error:
            return _refusal(error)
        return Response(status_code=204)

    @router.post("/{project_id}/notes/{note_id}/reply")
    async def reply_to_note(self, project_id: str, note_id: str, body: NoteReplyRequest) -> JSONResponse:
        """Add an owner reply to a note's thread."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        try:
            note, content = service.reply_as_owner(root, body.path, note_id, body.text)
        except ProjectError as error:
            return _refusal(error)
        return JSONResponse(NoteResult(note=_note_view(note, body.path, content)).model_dump(mode="json"))

    @router.patch("/{project_id}/notes/{note_id}/reply")
    async def patch_reply(self, project_id: str, note_id: str, body: NoteReplyPatchRequest) -> JSONResponse:
        """Change an existing reply's text. An agent reply is refused."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        try:
            note, content = service.change_reply(root, body.path, note_id, body.reply_index, text=body.text)
        except ProjectError as error:
            return _refusal(error)
        return JSONResponse(NoteResult(note=_note_view(note, body.path, content)).model_dump(mode="json"))

    @router.post("/{project_id}/notes/{note_id}/resolve")
    async def resolve_note(self, project_id: str, note_id: str, path: str) -> JSONResponse:
        """Mark a note's thread resolved."""
        return await self._set_note_status(project_id, note_id, path, "resolved")

    @router.post("/{project_id}/notes/{note_id}/reopen")
    async def reopen_note(self, project_id: str, note_id: str, path: str) -> JSONResponse:
        """Reopen a note's thread."""
        return await self._set_note_status(project_id, note_id, path, "open")

    async def _set_note_status(self, project_id: str, note_id: str, path: str, status: Literal["open", "resolved"]) -> JSONResponse:
        """The body `resolve_note` and `reopen_note` share, setting the thread's status to `status`."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        try:
            note, content = service.set_note_status(root, path, note_id, status)
        except ProjectError as error:
            return _refusal(error)
        return JSONResponse(NoteResult(note=_note_view(note, path, content)).model_dump(mode="json"))

    @router.get("/{project_id}/skills")
    async def get_skills(self, project_id: str) -> JSONResponse:
        """The project's skill catalog: every configured skill directory plus the harness's builtin skills."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        config = project_config(root)
        result = SkillsListResult(skills=service.skills(root, config), libraries=[*config.skills, "builtin"])
        return JSONResponse(result.model_dump(mode="json"))

    @router.post("/{project_id}/skills")
    async def post_skill(self, project_id: str, body: SkillScaffoldRequest) -> JSONResponse:
        """Scaffold a new skill in one of the project's configured skill directories. The builtin skills are read-only."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        config = project_config(root)
        if body.library not in config.skills:
            return JSONResponse({"detail": f"{body.library!r} is not a configured skill library"}, status_code=400)
        try:
            path = service.scaffold_skill(root, body.library, body.name)
        except ProjectError as error:
            return _refusal(error)
        return JSONResponse(SkillScaffoldResult(path=path).model_dump(mode="json"))

    @router.get("/{project_id}/skills/body")
    async def get_skill_body(self, project_id: str, library: str, name: str) -> JSONResponse:
        """One skill's full `SKILL.md` body, from any configured skill directory or the builtin skills, which are read-only."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        try:
            content, digest, editable = service.skill_body(root, library, name)
        except ProjectError as error:
            return _refusal(error)
        return JSONResponse(SkillBodyResult(content=content, hash=digest, editable=editable).model_dump(mode="json"))

    @router.put("/{project_id}/skills/body")
    async def put_skill_body(self, project_id: str, body: SkillBodyWriteRequest) -> JSONResponse:
        """Write one skill's `SKILL.md` body, refusing a stale `expected_hash`. The builtin skills never accept a write."""
        root = await self._project_root(project_id)
        if root is None:
            return _unknown_project()
        refusal = _skill_write_target_refusal(root, body.library)
        if refusal is not None:
            return refusal
        try:
            digest = service.write_skill_body(root, body.library, body.name, body.content, body.expected_hash)
        except ProjectError as error:
            return _refusal(error)
        return JSONResponse(FileWriteResult(hash=digest).model_dump(mode="json"))
