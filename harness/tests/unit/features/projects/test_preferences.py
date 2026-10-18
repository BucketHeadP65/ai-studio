"""Preferences: the owner's standing voice, read fresh from disk on every request."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from core.ai.workflows.contracts import StepRegistry
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest
from pydantic_ai.models.test import TestModel

from harness.features.projects.preferences import Preferences
from harness.session import NoSessionLog, Runtime

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

from tests.support.state import a_throwaway_state


def _runtime() -> Runtime:
    """A `Runtime` with nothing Preferences touches -- it never reads `ctx.deps`."""
    return Runtime(registry=StepRegistry(), library=cast("Any", None), agent=cast("Any", None), session_id=None, session_log=NoSessionLog(), state=a_throwaway_state())


def _instructions(result: object) -> str:
    """Every instructions string across a run's model requests, joined."""
    messages = result.all_messages()  # type: ignore[attr-defined]
    return "\n".join(message.instructions for message in messages if isinstance(message, ModelRequest) and message.instructions)


def _agent(*, global_path: Path, project_path: Path) -> Agent[Runtime, str]:
    return Agent(
        TestModel(call_tools=[]),
        deps_type=Runtime,
        output_type=str,
        capabilities=[Preferences[Runtime](global_path=global_path, project_path=project_path)],
    )


class TestPreferences:
    async def test_both_files_present_reach_the_instructions_in_their_own_sections(self, tmp_path: Path) -> None:
        global_path = tmp_path / "global-preferences.md"
        project_path = tmp_path / "project-preferences.md"
        global_path.write_text("Always answer in plain language.", encoding="utf-8")
        project_path.write_text("This repo uses tabs, not spaces.", encoding="utf-8")

        result = await _agent(global_path=global_path, project_path=project_path).run("hi", deps=_runtime())
        text = _instructions(result)

        assert "## Owner preferences" in text
        assert "Always answer in plain language." in text
        assert "### This project" in text
        assert "This repo uses tabs, not spaces." in text
        # The project text must land under its own heading, after the global text.
        assert text.index("Always answer in plain language.") < text.index("### This project")
        assert text.index("### This project") < text.index("This repo uses tabs, not spaces.")

    async def test_neither_file_present_adds_no_block_at_all(self, tmp_path: Path) -> None:
        global_path = tmp_path / "no-such-global.md"
        project_path = tmp_path / "no-such-project.md"

        result = await _agent(global_path=global_path, project_path=project_path).run("hi", deps=_runtime())
        text = _instructions(result)

        assert "Owner preferences" not in text

    async def test_only_the_global_file_present_omits_the_project_section(self, tmp_path: Path) -> None:
        global_path = tmp_path / "global-preferences.md"
        project_path = tmp_path / "no-such-project.md"
        global_path.write_text("Always answer in plain language.", encoding="utf-8")

        result = await _agent(global_path=global_path, project_path=project_path).run("hi", deps=_runtime())
        text = _instructions(result)

        assert "## Owner preferences" in text
        assert "Always answer in plain language." in text
        assert "### This project" not in text

    async def test_only_the_project_file_present_omits_nothing_of_its_own(self, tmp_path: Path) -> None:
        global_path = tmp_path / "no-such-global.md"
        project_path = tmp_path / "project-preferences.md"
        project_path.write_text("This repo uses tabs, not spaces.", encoding="utf-8")

        result = await _agent(global_path=global_path, project_path=project_path).run("hi", deps=_runtime())
        text = _instructions(result)

        assert "## Owner preferences" in text
        assert "### This project" in text
        assert "This repo uses tabs, not spaces." in text

    async def test_an_empty_file_contributes_nothing_the_same_as_a_missing_one(self, tmp_path: Path) -> None:
        global_path = tmp_path / "global-preferences.md"
        project_path = tmp_path / "no-such-project.md"
        global_path.write_text("   \n", encoding="utf-8")

        result = await _agent(global_path=global_path, project_path=project_path).run("hi", deps=_runtime())
        text = _instructions(result)

        assert "Owner preferences" not in text

    async def test_an_edit_between_two_runs_on_the_same_agent_reaches_the_second_run(self, tmp_path: Path) -> None:
        global_path = tmp_path / "global-preferences.md"
        project_path = tmp_path / "no-such-project.md"
        global_path.write_text("First version of the preference.", encoding="utf-8")
        agent = _agent(global_path=global_path, project_path=project_path)

        first = await agent.run("hi", deps=_runtime())
        global_path.write_text("Second version of the preference.", encoding="utf-8")
        second = await agent.run("hi", deps=_runtime())

        assert "First version of the preference." in _instructions(first)
        assert "Second version of the preference." not in _instructions(first)
        assert "Second version of the preference." in _instructions(second)
        assert "First version of the preference." not in _instructions(second)
