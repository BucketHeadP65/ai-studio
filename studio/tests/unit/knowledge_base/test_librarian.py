"""Unit tests for `src/studio/knowledge_base/refresh/infrastructure/librarian.py` — the optional post-sync placement pass.

`LibrarianPlacer.place()` never raises: an agent's answer decides a destination (existing folder,
newly named folder, or empty to leave the mirror alone), and every failure mode — the agent
raising, a destination that would escape `Sources/`, a name already occupied there — leaves the
file exactly where it landed and is recorded in the report's `left` list, never aborting the pass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai import Agent, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from studio.knowledge_base.refresh.infrastructure.librarian import (
    LibrarianAgent,
    LibrarianPlacement,
    LibrarianPlacer,
    NewMirror,
    new_mirrors_at,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo


def _write_mirror(path: Path, *, title: str, item_type: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: {title}\nitem_type: {item_type}\n---\nBody text.\n", encoding="utf-8")


def _mirror_at(path: Path, *, title: str, kind: str) -> NewMirror:
    _write_mirror(path, title=title, item_type=kind)
    return NewMirror(path=path, title=title, item_type=kind)


def _scripted(folder: str) -> Callable[[], LibrarianAgent]:
    """An `agent_factory` whose agent always answers the given `folder`."""

    def factory() -> LibrarianAgent:
        return Agent(TestModel(custom_output_args={"folder": folder}), output_type=LibrarianPlacement)

    return factory


def _by_title(mapping: dict[str, str]) -> Callable[[], LibrarianAgent]:
    """An `agent_factory` whose agent answers a different folder per mirror, keyed by the mirror's title.

    Unlike `_scripted` (one fixed answer for the whole pass), this reads the mirror's title out of
    the prompt `_decide` builds, so a batch can exercise "this one succeeds, that one doesn't" in a
    single `place()` call — the shape `place()` reuses one agent instance across every mirror.
    """

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        user_text = "\n".join(
            part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        )
        title = next(candidate for candidate in mapping if f"New mirror title: {candidate}" in user_text)
        return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args={"folder": mapping[title]})])

    def factory() -> LibrarianAgent:
        return Agent(FunctionModel(script), output_type=LibrarianPlacement)

    return factory


def _raising() -> Callable[[], LibrarianAgent]:
    """An `agent_factory` whose agent always raises, standing in for an unreachable model."""

    def _raise(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        msg = "agent unavailable"
        raise RuntimeError(msg)

    def factory() -> LibrarianAgent:
        return Agent(FunctionModel(_raise), output_type=LibrarianPlacement)

    return factory


class TestNewMirrorsAt:
    def test_reads_title_and_item_type_from_frontmatter(self, tmp_path: Path):
        _write_mirror(tmp_path / "Some Paper.md", title="Some Paper", item_type="paper")

        mirrors = new_mirrors_at(tmp_path, ["Some Paper.md"])

        assert mirrors == (NewMirror(path=tmp_path / "Some Paper.md", title="Some Paper", item_type="paper"),)

    def test_drops_a_missing_file_without_raising(self, tmp_path: Path):
        assert new_mirrors_at(tmp_path, ["gone.md"]) == ()

    def test_drops_a_file_with_no_frontmatter(self, tmp_path: Path):
        (tmp_path / "plain.md").write_text("just text\n", encoding="utf-8")

        assert new_mirrors_at(tmp_path, ["plain.md"]) == ()

    def test_drops_a_file_missing_title_or_item_type(self, tmp_path: Path):
        (tmp_path / "half.md").write_text("---\ntitle: Half\n---\nbody\n", encoding="utf-8")

        assert new_mirrors_at(tmp_path, ["half.md"]) == ()

    def test_one_bad_row_does_not_block_the_rest(self, tmp_path: Path):
        _write_mirror(tmp_path / "Good.md", title="Good", item_type="article")

        mirrors = new_mirrors_at(tmp_path, ["gone.md", "Good.md"])

        assert [m.title for m in mirrors] == ["Good"]


class TestLibrarianPlacerMovesIntoAnExistingFolder:
    async def test_moves_the_mirror_into_the_folder_the_agent_names(self, tmp_path: Path):
        (tmp_path / "Papers").mkdir()
        mirror = _mirror_at(tmp_path / "New Paper.md", title="New Paper", kind="paper")
        placer = LibrarianPlacer(_scripted("Papers"))

        report = await placer.place(tmp_path, [mirror])

        destination = tmp_path / "Papers" / "New Paper.md"
        assert report.moved == (destination,)
        assert report.left == ()
        assert not mirror.path.exists()
        assert destination.is_file()

    async def test_a_batch_reuses_a_folder_created_earlier_in_the_same_pass(self, tmp_path: Path):
        first = _mirror_at(tmp_path / "First.md", title="First", kind="paper")
        second = _mirror_at(tmp_path / "Second.md", title="Second", kind="paper")
        placer = LibrarianPlacer(_scripted("Papers/ML"))

        report = await placer.place(tmp_path, [first, second])

        assert set(report.moved) == {tmp_path / "Papers" / "ML" / "First.md", tmp_path / "Papers" / "ML" / "Second.md"}
        assert report.left == ()


class TestLibrarianPlacerMovesIntoANewFolder:
    async def test_creates_the_named_nested_folder_and_moves_the_mirror_there(self, tmp_path: Path):
        mirror = _mirror_at(tmp_path / "A New Video.md", title="A New Video", kind="video")
        placer = LibrarianPlacer(_scripted("Videos/Talks"))

        report = await placer.place(tmp_path, [mirror])

        destination = tmp_path / "Videos" / "Talks" / "A New Video.md"
        assert report.moved == (destination,)
        assert destination.is_file()
        assert not mirror.path.exists()


class TestLibrarianPlacerLeavesTheMirrorInPlace:
    async def test_an_empty_folder_answer_leaves_it_at_the_top_level(self, tmp_path: Path):
        mirror = _mirror_at(tmp_path / "Stays.md", title="Stays", kind="article")
        placer = LibrarianPlacer(_scripted(""))

        report = await placer.place(tmp_path, [mirror])

        assert report.moved == ()
        assert report.left == ()  # an explicit "leave it" is not a failure
        assert mirror.path.is_file()

    async def test_a_destination_escaping_sources_is_refused(self, tmp_path: Path):
        mirror = _mirror_at(tmp_path / "Escape.md", title="Escape", kind="article")
        placer = LibrarianPlacer(_scripted("../outside"))

        report = await placer.place(tmp_path, [mirror])

        assert report.moved == ()
        assert report.left == ("Escape.md",)
        assert mirror.path.is_file()

    async def test_a_destination_naming_the_archive_folder_is_refused(self, tmp_path: Path):
        mirror = _mirror_at(tmp_path / "Bad.md", title="Bad", kind="article")
        placer = LibrarianPlacer(_scripted("_archive"))

        report = await placer.place(tmp_path, [mirror])

        assert report.left == ("Bad.md",)
        assert mirror.path.is_file()

    async def test_an_occupied_destination_is_refused_and_nothing_is_overwritten(self, tmp_path: Path):
        (tmp_path / "Papers").mkdir()
        (tmp_path / "Papers" / "Taken.md").write_text("already here\n", encoding="utf-8")
        mirror = _mirror_at(tmp_path / "Taken.md", title="Taken", kind="paper")
        placer = LibrarianPlacer(_scripted("Papers"))

        report = await placer.place(tmp_path, [mirror])

        assert report.left == ("Taken.md",)
        assert mirror.path.is_file()
        assert (tmp_path / "Papers" / "Taken.md").read_text(encoding="utf-8") == "already here\n"


class TestLibrarianPlacerAgentFailure:
    async def test_a_raising_agent_leaves_every_mirror_in_place_and_the_pass_still_completes(self, tmp_path: Path):
        first = _mirror_at(tmp_path / "First.md", title="First", kind="paper")
        second = _mirror_at(tmp_path / "Second.md", title="Second", kind="video")
        placer = LibrarianPlacer(_raising())

        report = await placer.place(tmp_path, [first, second])

        assert report.moved == ()
        assert set(report.left) == {"First.md", "Second.md"}
        assert first.path.is_file()
        assert second.path.is_file()


class TestLibrarianPlacerContinuesPastAMoveFailure:
    async def test_a_destination_blocked_by_a_file_does_not_stop_the_rest_of_the_batch(self, tmp_path: Path):
        # "Papers" exists as a plain file: the blocked mirror's `mkdir(parents=True)` raises
        # `FileExistsError` (an `OSError`), which must not escape `place()`'s loop.
        (tmp_path / "Papers").write_text("i am a file, not a folder", encoding="utf-8")
        blocked = _mirror_at(tmp_path / "Blocked.md", title="Blocked", kind="paper")
        fine = _mirror_at(tmp_path / "Fine.md", title="Fine", kind="video")
        placer = LibrarianPlacer(_by_title({"Blocked": "Papers", "Fine": "Videos"}))

        report = await placer.place(tmp_path, [blocked, fine])

        assert report.left == ("Blocked.md",)
        assert blocked.path.is_file()
        destination = tmp_path / "Videos" / "Fine.md"
        assert report.moved == (destination,)
        assert destination.is_file()
        assert not fine.path.exists()


class TestLibrarianPlacerRefusesASymlinkEscape:
    async def test_a_destination_through_a_symlinked_folder_leaving_sources_is_refused(self, tmp_path: Path):
        sources_dir = tmp_path / "knowledge_base" / "Sources"
        sources_dir.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (sources_dir / "Escape").symlink_to(outside, target_is_directory=True)
        mirror = _mirror_at(sources_dir / "Sneaky.md", title="Sneaky", kind="article")
        placer = LibrarianPlacer(_scripted("Escape"))

        report = await placer.place(sources_dir, [mirror])

        assert report.moved == ()
        assert report.left == ("Sneaky.md",)
        assert mirror.path.is_file()
        assert list(outside.iterdir()) == []


class TestLibrarianPlacerEmptyBatch:
    async def test_no_new_mirrors_never_builds_an_agent(self, tmp_path: Path):
        built: list[object] = []

        def factory() -> LibrarianAgent:
            built.append(object())
            return Agent(TestModel(custom_output_args={"folder": ""}), output_type=LibrarianPlacement)

        report = await LibrarianPlacer(factory).place(tmp_path, [])

        assert report == report.__class__()
        assert built == []
