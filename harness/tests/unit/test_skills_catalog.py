"""The project skills plugin: catalog listing, scaffold, and `{project_id}` resolution over HTTP."""

from __future__ import annotations

import shutil

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
import pytest

from harness.features.projects.controller import ProjectsController
from harness.features.projects.infrastructure import skill_files
from harness.features.sessions.project_config import ProjectConfig
from harness.main import project_root_is_gone
from harness.session import MissingProjectRootError
from harness.settings import SKILLS
from tests.support.controllers import mounted
from tests.support.fakes.projects import FakeProjectIndex

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

_UNKNOWN_PROJECT_ID = "no-such-project"


def _write_skill(directory: Path, name: str, *, description: str = "What this skill is for.") -> None:
    skill_dir = directory / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\ndescription: {description}\n---\n\nBody.\n", encoding="utf-8")


def _write_broken_skill(directory: Path, name: str) -> None:
    skill_dir = directory / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\ndescription: never closed\n", encoding="utf-8")


@asynccontextmanager
async def _project(root: Path, *, config: ProjectConfig | None = None) -> AsyncGenerator[tuple[httpx.AsyncClient, str]]:
    """`root` known to a controller of its own, under one project id."""
    if config is not None:
        aifactory = root / ".aifactory"
        aifactory.mkdir(parents=True, exist_ok=True)
        (aifactory / "config.json").write_text(config.model_dump_json(), encoding="utf-8")
    project_id = "test-project"
    controller = ProjectsController(projects=FakeProjectIndex({project_id: root}))
    app = mounted(controller)
    app.add_exception_handler(MissingProjectRootError, project_root_is_gone)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        yield client, project_id


class TestCatalogModule:
    """`catalog()` over configured library dirs plus the builtin set -- no HTTP."""

    def test_a_valid_skill_is_listed_with_its_frontmatter(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        library = root / "skills"
        _write_skill(library, "my-skill", description="Use this when testing.")

        cards = skill_files.catalog(root, ProjectConfig(skills=("skills",)))

        [card] = [card for card in cards if card.library == "skills"]
        assert card.name == "my-skill"
        assert card.description == "Use this when testing."
        assert card.path == "skills/my-skill/SKILL.md"
        assert card.valid is True
        assert card.error is None

    def test_a_broken_skill_is_listed_with_its_error_not_vanished(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        library = root / "skills"
        _write_broken_skill(library, "broken-skill")

        cards = skill_files.catalog(root, ProjectConfig(skills=("skills",)))

        [card] = [card for card in cards if card.library == "skills"]
        assert card.name == "broken-skill"
        assert card.valid is False
        assert card.error is not None
        assert "unclosed" in card.error

    def test_a_missing_library_directory_is_skipped_not_raised(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        cards = skill_files.catalog(root, ProjectConfig(skills=("nonexistent",)))

        assert not any(card.library == "nonexistent" for card in cards)

    def test_builtin_skills_carry_the_builtin_library_and_an_absolute_path(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        cards = skill_files.catalog(root, ProjectConfig())

        builtin = [card for card in cards if card.library == "builtin"]
        assert builtin, "the harness's own skill set must always be present"
        assert all(card.path.startswith("/") for card in builtin)

    def test_two_configured_libraries_both_contribute_cards(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        _write_skill(root / "a", "from-a")
        _write_skill(root / "b", "from-b")

        cards = skill_files.catalog(root, ProjectConfig(skills=("a", "b")))

        names_by_library = {card.library: card.name for card in cards if card.library in {"a", "b"}}
        assert names_by_library == {"a": "from-a", "b": "from-b"}

    def test_engrams_skills_join_the_catalog_as_their_own_library(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        cards = skill_files.catalog(root, ProjectConfig())

        engram = [card for card in cards if card.library == "engram"]
        assert {card.name for card in engram} == {
            "documenting",
            "agent-threads",
            "searching-the-knowledge-base",
            "engram-init",
            "designing-note-writing",
        }
        assert all(card.valid for card in engram), [card.error for card in engram if not card.valid]


class TestReadBodyModule:
    """`read_body()`: builtin vs. configured-library addressing, and containment."""

    def test_reads_a_configured_project_skill_as_editable(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        _write_skill(root / "skills", "my-skill")

        content, digest, editable = skill_files.read_body(root, "skills", "my-skill")

        assert "Body." in content
        assert digest
        assert editable is True

    def test_reads_a_builtin_skill_as_read_only(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        builtins = [card for card in skill_files.catalog(root, ProjectConfig()) if card.library == "builtin"]
        name = builtins[0].name

        content, _digest, editable = skill_files.read_body(root, "builtin", name)

        assert content
        assert editable is False

    def test_raises_file_not_found_for_a_nonexistent_skill(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        with pytest.raises(FileNotFoundError):
            skill_files.read_body(root, "skills", "no-such-skill")

    def test_raises_permission_error_for_a_name_that_escapes_the_root(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        with pytest.raises(PermissionError):
            skill_files.read_body(root, "skills", "../../escaped")


class TestWriteBodyModule:
    """`write_body()`: overwrite, the hash conflict, and containment."""

    def test_writes_new_content_and_returns_its_hash(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        _write_skill(root / "skills", "my-skill")
        skill_file = root / "skills" / "my-skill" / "SKILL.md"

        digest = skill_files.write_body(root, "skills", "my-skill", "---\ndescription: new\n---\n\nNew body.\n", None)

        assert skill_file.read_text(encoding="utf-8") == "---\ndescription: new\n---\n\nNew body.\n"
        assert digest

    def test_a_stale_hash_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        _write_skill(root / "skills", "my-skill")

        with pytest.raises(ValueError, match="changed"):
            skill_files.write_body(root, "skills", "my-skill", "new content", "0" * 12)

    def test_raises_file_not_found_when_the_skill_directory_does_not_exist(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        with pytest.raises(FileNotFoundError):
            skill_files.write_body(root, "skills", "no-such-skill", "content", None)

    def test_raises_permission_error_for_a_name_that_escapes_the_root(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        with pytest.raises(PermissionError):
            skill_files.write_body(root, "skills", "../../escaped", "content", None)


class TestScaffoldModule:
    """`scaffold()`: creation, the 409-shaped conflict, and containment."""

    def test_scaffold_creates_a_loadable_skill_and_returns_the_relative_path(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        rel = skill_files.scaffold(root, "skills", "my-new-skill")

        assert rel == "skills/my-new-skill/SKILL.md"
        assert (root / rel).is_file()
        cards = skill_files.catalog(root, ProjectConfig(skills=("skills",)))
        [card] = [card for card in cards if card.library == "skills"]
        assert card.valid is True
        assert card.name == "my-new-skill"

    def test_scaffold_refuses_when_the_skill_already_exists(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        _write_skill(root / "skills", "dup")

        with pytest.raises(FileExistsError):
            skill_files.scaffold(root, "skills", "dup")

    def test_scaffold_refuses_a_name_that_escapes_the_root(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        with pytest.raises(PermissionError):
            skill_files.scaffold(root, "skills", "../../escaped")


class TestSkillsRoutes:
    """`{project_id}` resolution and the route-level status contract."""

    async def test_unknown_project_id_is_a_404(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root) as (client, _project_id):
            response = await client.get(f"/api/projects/{_UNKNOWN_PROJECT_ID}/skills")

            assert response.status_code == 404
            assert response.json() == {"detail": "no project with that id here"}

    async def test_a_vanished_project_root_is_a_410(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root) as (client, project_id):
            shutil.rmtree(root)

            response = await client.get(f"/api/projects/{project_id}/skills")

            assert response.status_code == 410

    async def test_libraries_field_lists_configured_dirs_plus_builtin_even_when_empty(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root, config=ProjectConfig(skills=("skills",))) as (client, project_id):
            response = await client.get(f"/api/projects/{project_id}/skills")

            assert response.status_code == 200
            body = response.json()
            assert body["libraries"] == ["skills", "builtin"]
            assert not any(card["library"] == "skills" for card in body["skills"]), (
                "an unpopulated configured library carries no cards yet"
            )

    async def test_get_skills_lists_a_configured_project_skill(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        _write_skill(root / "skills", "my-skill")

        async with _project(root, config=ProjectConfig(skills=("skills",))) as (client, project_id):
            response = await client.get(f"/api/projects/{project_id}/skills")

            assert response.status_code == 200
            body = response.json()
            [card] = [card for card in body["skills"] if card["library"] == "skills"]
            assert card["name"] == "my-skill"
            assert card["valid"] is True

    async def test_post_skill_scaffolds_and_returns_the_path(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root, config=ProjectConfig(skills=("skills",))) as (client, project_id):
            response = await client.post(f"/api/projects/{project_id}/skills", json={"library": "skills", "name": "new-skill"})

            assert response.status_code == 200
            assert response.json() == {"path": "skills/new-skill/SKILL.md"}
            assert (root / "skills" / "new-skill" / "SKILL.md").is_file()

    async def test_post_skill_refuses_an_existing_skill_with_409(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        _write_skill(root / "skills", "dup")

        async with _project(root, config=ProjectConfig(skills=("skills",))) as (client, project_id):
            response = await client.post(f"/api/projects/{project_id}/skills", json={"library": "skills", "name": "dup"})

            assert response.status_code == 409

    async def test_post_skill_refuses_an_unconfigured_library(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root, config=ProjectConfig(skills=("skills",))) as (client, project_id):
            response = await client.post(
                f"/api/projects/{project_id}/skills", json={"library": "not-configured", "name": "new-skill"}
            )

            assert response.status_code == 400
            assert not (root / "not-configured").exists()

    async def test_post_skill_refuses_builtin_as_a_target_library(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root) as (client, project_id):
            response = await client.post(f"/api/projects/{project_id}/skills", json={"library": "builtin", "name": "new-skill"})

            assert response.status_code == 400


class TestSkillBodyRoutes:
    """`GET`/`PUT .../skills/body`: full-body read for any library, write refused for builtin."""

    async def test_get_reads_a_configured_project_skill_as_editable(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        _write_skill(root / "skills", "my-skill")

        async with _project(root, config=ProjectConfig(skills=("skills",))) as (client, project_id):
            response = await client.get(
                f"/api/projects/{project_id}/skills/body", params={"library": "skills", "name": "my-skill"}
            )

            assert response.status_code == 200
            body = response.json()
            assert "Body." in body["content"]
            assert body["editable"] is True

    async def test_get_reads_a_builtin_skill_as_read_only(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root) as (client, project_id):
            listed = await client.get(f"/api/projects/{project_id}/skills")
            builtins = [card for card in listed.json()["skills"] if card["library"] == "builtin"]

            response = await client.get(
                f"/api/projects/{project_id}/skills/body", params={"library": "builtin", "name": builtins[0]["name"]}
            )

            assert response.status_code == 200
            assert response.json()["editable"] is False

    async def test_get_a_nonexistent_skill_is_a_404(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root, config=ProjectConfig(skills=("skills",))) as (client, project_id):
            response = await client.get(
                f"/api/projects/{project_id}/skills/body", params={"library": "skills", "name": "no-such-skill"}
            )

            assert response.status_code == 404

    async def test_put_writes_a_configured_project_skill(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        _write_skill(root / "skills", "my-skill")

        async with _project(root, config=ProjectConfig(skills=("skills",))) as (client, project_id):
            response = await client.put(
                f"/api/projects/{project_id}/skills/body",
                json={"library": "skills", "name": "my-skill", "content": "---\ndescription: edited\n---\n\nEdited.\n"},
            )

            assert response.status_code == 200
            assert response.json()["hash"]
            assert (root / "skills" / "my-skill" / "SKILL.md").read_text(encoding="utf-8") == (
                "---\ndescription: edited\n---\n\nEdited.\n"
            )

    async def test_put_refuses_builtin_as_the_target_library_with_403(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root) as (client, project_id):
            response = await client.put(
                f"/api/projects/{project_id}/skills/body",
                json={"library": "builtin", "name": "whatever", "content": "x"},
            )

            assert response.status_code == 403

    async def test_put_refuses_an_unconfigured_library_with_400(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()

        async with _project(root, config=ProjectConfig(skills=("skills",))) as (client, project_id):
            response = await client.put(
                f"/api/projects/{project_id}/skills/body",
                json={"library": "not-configured", "name": "whatever", "content": "x"},
            )

            assert response.status_code == 400

    async def test_put_refuses_a_stale_hash_with_409(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        _write_skill(root / "skills", "my-skill")

        async with _project(root, config=ProjectConfig(skills=("skills",))) as (client, project_id):
            response = await client.put(
                f"/api/projects/{project_id}/skills/body",
                json={"library": "skills", "name": "my-skill", "content": "x", "expected_hash": "0" * 12},
            )

            assert response.status_code == 409


def test_the_saving_skill_teaches_the_shelf_first() -> None:
    text = (SKILLS / "saving-a-workflow" / "SKILL.md").read_text(encoding="utf-8")
    assert "search_steps" in text
    heading = "Reuse a step from the library before defining one"
    assert heading in text
    assert text.index(heading) < text.index("Defining the steps")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
