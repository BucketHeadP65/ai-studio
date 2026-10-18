"""The project files plugin: containment, hash conflicts, sidecars, and `{project_id}` resolution."""

from __future__ import annotations

import hashlib
import shutil

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
import pytest

from engram.kb.contracts import NoteAnchor, NoteSheet, add_reply

from harness.features.projects.controller import ProjectsController
from harness.features.projects.domain import add_note, annotate_read
from harness.features.projects.infrastructure import project_files
from harness.features.projects.infrastructure.note_sidecars import load_sheet, save_sheet, sidecar_path
from harness.main import project_root_is_gone
from harness.session import MissingProjectRootError
from tests.support.controllers import mounted
from tests.support.fakes.projects import FakeProjectIndex

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

_UNKNOWN_PROJECT_ID = "no-such-project"


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


@asynccontextmanager
async def _project(root: Path) -> AsyncGenerator[tuple[httpx.AsyncClient, str]]:
    """`root` known to a controller of its own, under one project id."""
    project_id = "test-project"
    controller = ProjectsController(projects=FakeProjectIndex({project_id: root}))
    app = mounted(controller)
    app.add_exception_handler(MissingProjectRootError, project_root_is_gone)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        yield client, project_id


class TestFilesModule:
    """The pure, contained ops -- no HTTP, no registry."""

    def test_traversal_outside_the_root_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

        with pytest.raises(PermissionError):
            project_files.read(root, "../outside.txt")

    def test_a_symlink_escaping_the_root_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        (root / "escape").symlink_to(outside)

        with pytest.raises(PermissionError):
            project_files.read(root, "escape/secret.txt")

    def test_a_stale_hash_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("original", encoding="utf-8")

        with pytest.raises(ValueError, match="changed"):
            project_files.write(root, "note.md", "new content", "0" * 12)

    def test_a_missing_expected_hash_writes_unconditionally(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        target = root / "note.md"
        target.write_text("original", encoding="utf-8")

        new_hash = project_files.write(root, "note.md", "replaced", None)

        assert target.read_text(encoding="utf-8") == "replaced"
        assert new_hash == _hash("replaced")

    def test_write_creates_a_new_file_when_none_exists(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        new_hash = project_files.write(root, "new.md", "fresh content", None)

        assert (root / "new.md").read_text(encoding="utf-8") == "fresh content"
        assert new_hash == _hash("fresh content")

    def test_rename_carries_a_centralized_sidecar_and_the_notes_follow_the_new_path(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        target = root / "note.md"
        target.write_text("body", encoding="utf-8")
        sheet, _note = add_note(NoteSheet(), anchor=NoteAnchor(exact="body"), text="watch this")
        save_sheet(root, target, sheet)
        centralized = sidecar_path(root, target)
        assert centralized.is_file(), "the fixture must land at the centralized home"

        project_files.rename(root, "note.md", "renamed.md")

        assert not centralized.exists()
        renamed = root / "renamed.md"
        assert load_sheet(root, renamed) == sheet, "the notes must follow the file to its new path"
        assert sidecar_path(root, renamed).is_file()

    def test_rename_refuses_when_only_the_centralized_destination_sidecar_exists(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("source body", encoding="utf-8")
        destination = root / "renamed.md"
        destination.write_text("placeholder", encoding="utf-8")
        centralized_destination = sidecar_path(root, destination)
        save_sheet(root, destination, NoteSheet(notes=(add_note(NoteSheet(), anchor=NoteAnchor(exact="x"), text="y")[1],)))
        destination.unlink()
        assert centralized_destination.is_file(), "the fixture's destination sidecar must survive the file's own removal"

        with pytest.raises(FileExistsError):
            project_files.rename(root, "note.md", "renamed.md")

        assert (root / "note.md").exists(), "a refused rename must not partially apply"
        assert not (root / "renamed.md").exists()
        assert centralized_destination.is_file()

    def test_rename_and_delete_leave_a_file_beside_the_source_alone(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("body", encoding="utf-8")
        beside = root / "note.md.threads.json"
        beside.write_text('{"version": 1, "notes": []}', encoding="utf-8")

        project_files.rename(root, "note.md", "renamed.md")
        project_files.delete(root, "renamed.md")

        assert beside.is_file()
        assert beside.read_text(encoding="utf-8") == '{"version": 1, "notes": []}'

    def test_create_makes_an_empty_file_and_answers_its_hash(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        digest = project_files.create(root, "new.md")

        assert (root / "new.md").read_text(encoding="utf-8") == ""
        assert digest == _hash("")

    def test_delete_removes_a_centralized_sidecar(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        target = root / "note.md"
        target.write_text("body", encoding="utf-8")
        save_sheet(root, target, NoteSheet(notes=(add_note(NoteSheet(), anchor=NoteAnchor(exact="body"), text="watch this")[1],)))
        centralized = sidecar_path(root, target)
        assert centralized.is_file(), "the fixture must land at the centralized home"

        project_files.delete(root, "note.md")

        assert not target.exists()
        assert not centralized.exists()

    def test_tree_skips_aifactory_and_dotdirs(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "readme.md").write_text("hi", encoding="utf-8")
        (root / ".aifactory").mkdir()
        (root / ".aifactory" / "config.json").write_text("{}", encoding="utf-8")
        (root / ".git").mkdir()
        (root / ".git" / "HEAD").write_text("ref", encoding="utf-8")
        (root / "notes").mkdir()
        (root / "notes" / "a.md").write_text("a", encoding="utf-8")

        entries = project_files.tree(root)

        paths = {entry.path for entry in entries}
        assert paths == {"readme.md", "notes", "notes/a.md"}

    def test_tree_never_shows_the_agent_threads_home(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "readme.md").write_text("hi", encoding="utf-8")
        (root / ".agent-threads").mkdir(parents=True)
        (root / ".agent-threads" / "readme.md.threads.json").write_text("{}", encoding="utf-8")

        entries = project_files.tree(root)

        paths = {entry.path for entry in entries}
        assert paths == {"readme.md"}


class TestFilesRoutes:
    """`{project_id}` resolution, the obsidian flag, and the route-level status contract."""

    async def test_unknown_project_id_is_a_404(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root) as (client, _project_id):
            response = await client.get(f"/api/projects/{_UNKNOWN_PROJECT_ID}/files")

            assert response.status_code == 404
            assert response.json() == {"detail": "no project with that id here"}

    async def test_the_obsidian_flag_reflects_the_vault_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("body", encoding="utf-8")

        async with _project(root) as (client, project_id):
            plain = await client.get(f"/api/projects/{project_id}/file", params={"path": "note.md"})
            assert plain.status_code == 200
            assert plain.json()["obsidian"] is False

            (root / ".obsidian").mkdir()
            vaulted = await client.get(f"/api/projects/{project_id}/file", params={"path": "note.md"})
            assert vaulted.json()["obsidian"] is True

    async def test_a_stale_hash_is_refused_with_409(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("original", encoding="utf-8")

        async with _project(root) as (client, project_id):
            response = await client.put(
                f"/api/projects/{project_id}/file", json={"path": "note.md", "content": "new", "expected_hash": "0" * 12}
            )

            assert response.status_code == 409

    async def test_a_write_with_no_expected_hash_key_overwrites_unconditionally(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("original", encoding="utf-8")

        async with _project(root) as (client, project_id):
            response = await client.put(f"/api/projects/{project_id}/file", json={"path": "note.md", "content": "replaced"})

            assert response.status_code == 200
            assert (root / "note.md").read_text(encoding="utf-8") == "replaced"
            assert response.json()["hash"] == _hash("replaced")

    async def test_a_write_to_a_new_path_creates_the_file(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root) as (client, project_id):
            response = await client.put(f"/api/projects/{project_id}/file", json={"path": "new.md", "content": "fresh content"})

            assert response.status_code == 200
            assert response.json()["hash"] == _hash("fresh content")
            assert (root / "new.md").read_text(encoding="utf-8") == "fresh content"

    async def test_create_makes_an_empty_file(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root) as (client, project_id):
            response = await client.post(f"/api/projects/{project_id}/file/create", json={"path": "new.md"})

            assert response.status_code == 200
            assert response.json()["hash"] == _hash("")
            assert (root / "new.md").read_text(encoding="utf-8") == ""

    async def test_create_refuses_a_body_with_unknown_fields(self, tmp_path: Path) -> None:
        """A caller sending `content` to create expects it written; dropping it silently loses data."""
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root) as (client, project_id):
            response = await client.post(
                f"/api/projects/{project_id}/file/create", json={"path": "new.md", "content": "# lost"}
            )

            assert response.status_code == 422
            assert not (root / "new.md").exists(), "a refused create writes nothing"

    async def test_create_refuses_an_existing_path_with_409(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("already here", encoding="utf-8")

        async with _project(root) as (client, project_id):
            response = await client.post(f"/api/projects/{project_id}/file/create", json={"path": "note.md"})

            assert response.status_code == 409
            assert (root / "note.md").read_text(encoding="utf-8") == "already here", "a refused create must not truncate"

    async def test_create_refuses_a_traversal_with_400(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root) as (client, project_id):
            response = await client.post(f"/api/projects/{project_id}/file/create", json={"path": "../outside.txt"})

            assert response.status_code == 400
            assert not (tmp_path / "outside.txt").exists()

    async def test_create_dir_makes_an_empty_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root) as (client, project_id):
            response = await client.post(f"/api/projects/{project_id}/dir/create", json={"path": "new-folder"})

            assert response.status_code == 204
            assert (root / "new-folder").is_dir()

    async def test_create_dir_refuses_an_existing_path_with_409(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        (root / "existing").mkdir(parents=True)

        async with _project(root) as (client, project_id):
            response = await client.post(f"/api/projects/{project_id}/dir/create", json={"path": "existing"})

            assert response.status_code == 409

    async def test_create_dir_refuses_a_traversal_with_400(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root) as (client, project_id):
            response = await client.post(f"/api/projects/{project_id}/dir/create", json={"path": "../outside"})

            assert response.status_code == 400
            assert not (tmp_path / "outside").exists()

    async def test_rename_carries_the_sidecar_over_http(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        target = root / "note.md"
        target.write_text("body", encoding="utf-8")
        save_sheet(root, target, NoteSheet(notes=(add_note(NoteSheet(), anchor=NoteAnchor(exact="body"), text="watch this")[1],)))

        async with _project(root) as (client, project_id):
            response = await client.post(f"/api/projects/{project_id}/file/rename", json={"path": "note.md", "to": "renamed.md"})

            assert response.status_code == 204
            renamed = root / "renamed.md"
            assert renamed.exists()
            assert sidecar_path(root, renamed).is_file()
            assert not target.exists()
            assert not sidecar_path(root, target).exists()

    async def test_rename_over_http_refuses_an_existing_destination_sidecar(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("source body", encoding="utf-8")
        destination = root / "renamed.md"
        destination.write_text("placeholder", encoding="utf-8")
        save_sheet(root, destination, NoteSheet(notes=(add_note(NoteSheet(), anchor=NoteAnchor(exact="x"), text="y")[1],)))
        destination.unlink()

        async with _project(root) as (client, project_id):
            response = await client.post(f"/api/projects/{project_id}/file/rename", json={"path": "note.md", "to": "renamed.md"})

            assert response.status_code == 409
            assert (root / "note.md").exists()
            assert not destination.exists()
            assert sidecar_path(root, destination).is_file()

    async def test_delete_removes_the_sidecar_over_http(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        target = root / "note.md"
        target.write_text("body", encoding="utf-8")
        save_sheet(root, target, NoteSheet(notes=(add_note(NoteSheet(), anchor=NoteAnchor(exact="body"), text="watch this")[1],)))

        async with _project(root) as (client, project_id):
            response = await client.delete(f"/api/projects/{project_id}/file", params={"path": "note.md"})

            assert response.status_code == 204
            assert not target.exists()
            assert not sidecar_path(root, target).exists()

    async def test_traversal_is_refused_with_400_over_http(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root) as (client, project_id):
            response = await client.get(f"/api/projects/{project_id}/file", params={"path": "../outside.txt"})

            assert response.status_code == 400

    async def test_a_vanished_project_root_is_a_410(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root) as (client, project_id):
            shutil.rmtree(root)

            response = await client.get(f"/api/projects/{project_id}/files")

            assert response.status_code == 410


class TestNotesRoutes:
    """The notes plugin: server-minted ids and stamps, fresh anchor resolution, and the CRUD cycle."""

    async def test_post_computes_only_id_and_stamp(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("one\ntwo\nthree\n", encoding="utf-8")

        async with _project(root) as (client, project_id):
            response = await client.post(
                f"/api/projects/{project_id}/notes",
                json={"path": "note.md", "anchor": {"exact": "two"}, "text": "watch this"},
            )

            assert response.status_code == 200
            note = response.json()["note"]
            assert note["text"] == "watch this"
            assert note["archived"] is False
            assert note["orphaned"] is False
            assert note["line"] == 2
            assert note["id"]
            assert note["status"] == "open"
            assert note["replies"] == []
            assert "+" in note["created_at"] or note["created_at"].endswith("Z")

    async def test_list_resolves_line_and_orphan_fresh(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        target = root / "note.md"
        target.write_text("one\ntwo\nthree\n", encoding="utf-8")

        async with _project(root) as (client, project_id):
            created = await client.post(
                f"/api/projects/{project_id}/notes",
                json={"path": "note.md", "anchor": {"exact": "two"}, "text": "watch this"},
            )
            assert created.json()["note"]["orphaned"] is False

            target.write_text("one\nCHANGED\nthree\n", encoding="utf-8")
            listed = await client.get(f"/api/projects/{project_id}/notes", params={"path": "note.md"})

            assert listed.status_code == 200
            [note] = listed.json()["notes"]
            assert note["orphaned"] is True
            assert note["line"] is None

    async def test_list_with_no_path_covers_every_annotated_file(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "a.md").write_text("alpha", encoding="utf-8")
        (root / "b.md").write_text("beta", encoding="utf-8")

        async with _project(root) as (client, project_id):
            await client.post(f"/api/projects/{project_id}/notes", json={"path": "a.md", "anchor": {"exact": "alpha"}, "text": "a"})
            await client.post(f"/api/projects/{project_id}/notes", json={"path": "b.md", "anchor": {"exact": "beta"}, "text": "b"})

            listed = await client.get(f"/api/projects/{project_id}/notes")

            assert listed.status_code == 200
            paths = {note["path"] for note in listed.json()["notes"]}
            assert paths == {"a.md", "b.md"}

    async def test_patch_archiving_a_note_hides_it_from_the_agent_render(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        target = root / "note.md"
        target.write_text("one\ntwo\nthree\n", encoding="utf-8")

        async with _project(root) as (client, project_id):
            created = await client.post(
                f"/api/projects/{project_id}/notes",
                json={"path": "note.md", "anchor": {"exact": "two"}, "text": "watch this"},
            )
            note_id = created.json()["note"]["id"]

            patched = await client.patch(f"/api/projects/{project_id}/notes/{note_id}", json={"path": "note.md", "archived": True})

            assert patched.status_code == 200
            body = patched.json()["note"]
            assert body["archived"] is True
            assert body["text"] == "watch this", "archiving must not touch the text"

        sheet = load_sheet(root, target)
        rendered = annotate_read("RAW RESULT", target.read_text(encoding="utf-8"), sheet)
        assert rendered == "RAW RESULT", "an archived note must render nothing to the agent"

    async def test_patch_can_change_text_without_touching_archived(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("one\ntwo\nthree\n", encoding="utf-8")

        async with _project(root) as (client, project_id):
            created = await client.post(
                f"/api/projects/{project_id}/notes",
                json={"path": "note.md", "anchor": {"exact": "two"}, "text": "original"},
            )
            note_id = created.json()["note"]["id"]

            patched = await client.patch(f"/api/projects/{project_id}/notes/{note_id}", json={"path": "note.md", "text": "revised"})

            assert patched.json()["note"]["text"] == "revised"
            assert patched.json()["note"]["archived"] is False

    async def test_patch_an_unknown_note_id_is_a_404(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("body", encoding="utf-8")

        async with _project(root) as (client, project_id):
            response = await client.patch(
                f"/api/projects/{project_id}/notes/no-such-id", json={"path": "note.md", "text": "x"}
            )

            assert response.status_code == 404

    async def test_delete_removes_the_note(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("one\ntwo\nthree\n", encoding="utf-8")

        async with _project(root) as (client, project_id):
            created = await client.post(
                f"/api/projects/{project_id}/notes",
                json={"path": "note.md", "anchor": {"exact": "two"}, "text": "watch this"},
            )
            note_id = created.json()["note"]["id"]

            deleted = await client.delete(f"/api/projects/{project_id}/notes/{note_id}", params={"path": "note.md"})
            listed = await client.get(f"/api/projects/{project_id}/notes", params={"path": "note.md"})

            assert deleted.status_code == 204
            assert listed.json()["notes"] == []

    async def test_delete_an_unknown_note_id_is_a_404(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("body", encoding="utf-8")

        async with _project(root) as (client, project_id):
            response = await client.delete(f"/api/projects/{project_id}/notes/no-such-id", params={"path": "note.md"})

            assert response.status_code == 404

    async def test_reply_appends_an_owner_reply_to_the_thread(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("one\ntwo\nthree\n", encoding="utf-8")

        async with _project(root) as (client, project_id):
            created = await client.post(
                f"/api/projects/{project_id}/notes",
                json={"path": "note.md", "anchor": {"exact": "two"}, "text": "watch this"},
            )
            note_id = created.json()["note"]["id"]

            replied = await client.post(
                f"/api/projects/{project_id}/notes/{note_id}/reply", json={"path": "note.md", "text": "on it"}
            )

            assert replied.status_code == 200
            [reply] = replied.json()["note"]["replies"]
            assert reply["author"] == "owner"
            assert reply["text"] == "on it"

    async def test_reply_refuses_a_body_with_unknown_fields(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("body", encoding="utf-8")

        async with _project(root) as (client, project_id):
            response = await client.post(
                f"/api/projects/{project_id}/notes/some-id/reply",
                json={"path": "note.md", "text": "on it", "author": "agent"},
            )

            assert response.status_code == 422

    async def test_reply_to_an_unknown_note_id_is_a_404(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("body", encoding="utf-8")

        async with _project(root) as (client, project_id):
            response = await client.post(
                f"/api/projects/{project_id}/notes/no-such-id/reply", json={"path": "note.md", "text": "on it"}
            )

            assert response.status_code == 404

    async def test_resolve_then_reopen_a_thread(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("one\ntwo\nthree\n", encoding="utf-8")

        async with _project(root) as (client, project_id):
            created = await client.post(
                f"/api/projects/{project_id}/notes",
                json={"path": "note.md", "anchor": {"exact": "two"}, "text": "watch this"},
            )
            note_id = created.json()["note"]["id"]

            resolved = await client.post(
                f"/api/projects/{project_id}/notes/{note_id}/resolve", params={"path": "note.md"}
            )
            assert resolved.status_code == 200
            assert resolved.json()["note"]["status"] == "resolved"

            reopened = await client.post(
                f"/api/projects/{project_id}/notes/{note_id}/reopen", params={"path": "note.md"}
            )
            assert reopened.status_code == 200
            assert reopened.json()["note"]["status"] == "open"

    async def test_resolve_an_unknown_note_id_is_a_404(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("body", encoding="utf-8")

        async with _project(root) as (client, project_id):
            response = await client.post(
                f"/api/projects/{project_id}/notes/no-such-id/resolve", params={"path": "note.md"}
            )

            assert response.status_code == 404

    async def test_a_resolved_thread_is_excluded_from_the_agents_render(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        target = root / "note.md"
        target.write_text("one\ntwo\nthree\n", encoding="utf-8")

        async with _project(root) as (client, project_id):
            created = await client.post(
                f"/api/projects/{project_id}/notes",
                json={"path": "note.md", "anchor": {"exact": "two"}, "text": "watch this"},
            )
            note_id = created.json()["note"]["id"]
            await client.post(f"/api/projects/{project_id}/notes/{note_id}/resolve", params={"path": "note.md"})

        sheet = load_sheet(root, target)
        rendered = annotate_read("RAW RESULT", target.read_text(encoding="utf-8"), sheet)
        assert rendered == "RAW RESULT", "a resolved thread must render nothing to the agent"

    async def test_notes_on_a_missing_file_is_a_404(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root) as (client, project_id):
            response = await client.post(
                f"/api/projects/{project_id}/notes",
                json={"path": "ghost.md", "anchor": {"exact": "x"}, "text": "y"},
            )

            assert response.status_code == 404


class TestNotesSummaryRoute:
    """`GET .../notes/summary`: per-file aggregates over every annotated file."""

    async def test_a_project_with_no_annotated_files_answers_an_empty_list(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root) as (client, project_id):
            response = await client.get(f"/api/projects/{project_id}/notes/summary")

            assert response.status_code == 200
            assert response.json() == {"files": []}

    async def test_summarizes_open_resolved_and_the_latest_agent_reply(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("one\ntwo\n", encoding="utf-8")

        async with _project(root) as (client, project_id):
            created = await client.post(
                f"/api/projects/{project_id}/notes",
                json={"path": "note.md", "anchor": {"exact": "one"}, "text": "watch this"},
            )
            note_id = created.json()["note"]["id"]
            await client.post(f"/api/projects/{project_id}/notes/{note_id}/reply", json={"path": "note.md", "text": "on it"})

            response = await client.get(f"/api/projects/{project_id}/notes/summary")

            assert response.status_code == 200
            [entry] = response.json()["files"]
            assert entry["path"] == "note.md"
            assert entry["open_notes"] == 1
            assert entry["resolved_notes"] == 0
            assert entry["latest_agent_reply_at"] is None, "the reply endpoint posts an owner reply, not an agent one"

    async def test_unknown_project_id_is_a_404(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root) as (client, _project_id):
            response = await client.get(f"/api/projects/{_UNKNOWN_PROJECT_ID}/notes/summary")

            assert response.status_code == 404

    async def test_a_vanished_project_root_is_a_410(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root) as (client, project_id):
            shutil.rmtree(root)

            response = await client.get(f"/api/projects/{project_id}/notes/summary")

            assert response.status_code == 410


class TestNotesSidecarHome:
    """A note created through the plugin lands at the `.agent-threads/` home."""

    async def test_a_new_note_lands_at_the_centralized_sidecar_home(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "docs").mkdir()
        (root / "docs" / "x.md").write_text("one two", encoding="utf-8")

        async with _project(root) as (client, project_id):
            response = await client.post(
                f"/api/projects/{project_id}/notes",
                json={"path": "docs/x.md", "anchor": {"exact": "one"}, "text": "watch this"},
            )

            assert response.status_code == 200

        assert (root / ".agent-threads" / "docs" / "x.md.threads.json").is_file()
        assert not (root / "docs" / "x.md.threads.json").exists()

    async def test_a_sidecar_beside_the_file_is_not_read_through_the_door(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("one two", encoding="utf-8")
        beside_sheet = NoteSheet(notes=(add_note(NoteSheet(), anchor=NoteAnchor(exact="one"), text="beside")[1],))
        (root / "note.md.threads.json").write_text(beside_sheet.model_dump_json(), encoding="utf-8")

        async with _project(root) as (client, project_id):
            listed = await client.get(f"/api/projects/{project_id}/notes", params={"path": "note.md"})
            assert listed.status_code == 200
            assert listed.json()["notes"] == []

        assert (root / "note.md.threads.json").exists()
        assert not (root / ".agent-threads" / "note.md.threads.json").exists()


class TestNoteAnchorOffsets:
    """`start`/`end` character offsets alongside the listing's `line`."""

    async def test_a_plain_anchor_carries_its_character_offsets(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("one two three\n", encoding="utf-8")

        async with _project(root) as (client, project_id):
            created = await client.post(
                f"/api/projects/{project_id}/notes",
                json={"path": "note.md", "anchor": {"exact": "two"}, "text": "watch this"},
            )

            note = created.json()["note"]
            assert note["start"] == 4
            assert note["end"] == 7

    async def test_a_disambiguated_anchor_carries_the_right_offsets(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("alpha target beta\nomega target gamma\n", encoding="utf-8")

        async with _project(root) as (client, project_id):
            created = await client.post(
                f"/api/projects/{project_id}/notes",
                json={"path": "note.md", "anchor": {"exact": "target", "prefix": "omega ", "suffix": " gamma"}, "text": "x"},
            )

            note = created.json()["note"]
            second_line_start = len("alpha target beta\n")
            assert note["start"] == second_line_start + len("omega ")
            assert note["end"] == note["start"] + len("target")

    async def test_an_orphaned_anchor_carries_null_offsets(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        target = root / "note.md"
        target.write_text("one two three\n", encoding="utf-8")

        async with _project(root) as (client, project_id):
            created = await client.post(
                f"/api/projects/{project_id}/notes",
                json={"path": "note.md", "anchor": {"exact": "two"}, "text": "watch this"},
            )
            assert created.json()["note"]["start"] is not None

            target.write_text("completely different content\n", encoding="utf-8")
            listed = await client.get(f"/api/projects/{project_id}/notes", params={"path": "note.md"})

            [note] = listed.json()["notes"]
            assert note["orphaned"] is True
            assert note["start"] is None
            assert note["end"] is None


class TestNoteEditDoors:
    """B2: editing a note's text, and editing an existing reply's text (owner-authored only)."""

    async def test_note_text_can_be_edited_at_any_time(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("one two three\n", encoding="utf-8")

        async with _project(root) as (client, project_id):
            created = await client.post(
                f"/api/projects/{project_id}/notes",
                json={"path": "note.md", "anchor": {"exact": "two"}, "text": "original"},
            )
            note_id = created.json()["note"]["id"]
            await client.post(f"/api/projects/{project_id}/notes/{note_id}/resolve", params={"path": "note.md"})

            patched = await client.patch(
                f"/api/projects/{project_id}/notes/{note_id}", json={"path": "note.md", "text": "revised"}
            )

            assert patched.status_code == 200
            assert patched.json()["note"]["text"] == "revised"

    async def test_an_owner_replys_text_can_be_edited(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("one two three\n", encoding="utf-8")

        async with _project(root) as (client, project_id):
            created = await client.post(
                f"/api/projects/{project_id}/notes",
                json={"path": "note.md", "anchor": {"exact": "two"}, "text": "watch this"},
            )
            note_id = created.json()["note"]["id"]
            await client.post(
                f"/api/projects/{project_id}/notes/{note_id}/reply", json={"path": "note.md", "text": "on it"}
            )

            patched = await client.patch(
                f"/api/projects/{project_id}/notes/{note_id}/reply",
                json={"path": "note.md", "reply_index": 0, "text": "actually, done"},
            )

            assert patched.status_code == 200
            [reply] = patched.json()["note"]["replies"]
            assert reply["text"] == "actually, done"
            assert reply["author"] == "owner"

    async def test_editing_an_agent_reply_is_refused_in_a_plain_sentence(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        target = root / "note.md"
        target.write_text("one two three\n", encoding="utf-8")

        async with _project(root) as (client, project_id):
            created = await client.post(
                f"/api/projects/{project_id}/notes",
                json={"path": "note.md", "anchor": {"exact": "two"}, "text": "watch this"},
            )
            note_id = created.json()["note"]["id"]

        sheet = load_sheet(root, target)
        sheet, _updated = add_reply(sheet, note_id, author="agent", text="fixed it")
        save_sheet(root, target, sheet)

        async with _project(root) as (client, project_id):
            patched = await client.patch(
                f"/api/projects/{project_id}/notes/{note_id}/reply",
                json={"path": "note.md", "reply_index": 0, "text": "nope"},
            )

            assert patched.status_code == 400
            detail = patched.json()["detail"]
            assert all(char.isalnum() or char in " ,.?()" for char in detail)

    async def test_editing_an_unknown_notes_reply_is_a_404(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("body", encoding="utf-8")

        async with _project(root) as (client, project_id):
            response = await client.patch(
                f"/api/projects/{project_id}/notes/no-such-id/reply",
                json={"path": "note.md", "reply_index": 0, "text": "x"},
            )

            assert response.status_code == 404

    async def test_editing_an_out_of_range_reply_index_is_a_404(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        (root / "note.md").write_text("one two three\n", encoding="utf-8")

        async with _project(root) as (client, project_id):
            created = await client.post(
                f"/api/projects/{project_id}/notes",
                json={"path": "note.md", "anchor": {"exact": "two"}, "text": "watch this"},
            )
            note_id = created.json()["note"]["id"]

            response = await client.patch(
                f"/api/projects/{project_id}/notes/{note_id}/reply",
                json={"path": "note.md", "reply_index": 0, "text": "x"},
            )

            assert response.status_code == 404


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
