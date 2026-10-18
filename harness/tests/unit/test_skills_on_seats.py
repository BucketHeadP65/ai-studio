"""A seat names its skills by name: a custom agent, a profile, and the chat agent's own default.

`Skills(SKILLS, include=names)` is the one narrowing this whole slice performs -- never a whole
directory mounted wide. `ORCHESTRATOR_SKILLS` is the chat agent's catalog when no profile names
its own list; `list_skills` is the read plugin an authoring agent calls before naming any of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

from core.ai.workflows.contracts import AgentStep, Shape
from engram.skills.contracts import SKILLS as ENGRAM_SKILLS
from pydantic_ai.capabilities import WrapperCapability
from pydantic_ai.models.test import TestModel
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness.skills import Skills

from harness import plugins
from harness.composition import build_worker_agent
from harness.domain.ports import ORCHESTRATOR_SKILLS
from harness.features.authoring.agent import _SkillsForSelection
from harness.features.selfauthoring.exceptions import AgentValidationError
from harness.features.selfauthoring.infrastructure.agents import FileCustomAgents, validate_agent_document
from harness.features.selfauthoring.infrastructure.profiles import FileCustomProfiles
from harness.features.selfauthoring.service import select_profile, selected_profile_skills
from harness.features.selfauthoring.skills_plugin import SkillsPluginToolset, list_skills_lines
from harness.features.sessions.project_config import ProjectConfig
from harness.settings import CHECKOUT, SKILLS, WebSearchSettings
from tests.support.environment import fake_engram
from tests.support.selfauthoring import FakeActivationLog
from tests.support.state import a_state

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from pydantic import JsonValue
    from pydantic_ai import Agent
    from pydantic_ai.tools import RunContext
    from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef

    from harness.session import Runtime

BRIEFING = "Read the ledger and say whether the quarter looks healthy."

_STEP_OUTPUT = Shape(json_schema={"type": "string"})


def _step(agent: Mapping[str, JsonValue]) -> AgentStep:
    return AgentStep(name="weigh", agent=agent, output=_STEP_OUTPUT)


def _worker_agent(agent_document: Mapping[str, JsonValue]) -> Agent[None, object]:
    return build_worker_agent(
        _step(agent_document),
        str,
        model=TestModel(),
        engram=fake_engram(),
        reads=(),
        budget=10,
        promoted=(),
        resolve_agent=lambda name: {},
        web_search=WebSearchSettings(),
    )


def _mounted_skill_ids(agent: Agent[None, object]) -> frozenset[str]:
    """The skill names every `Skills` mounted on `agent` narrows its catalog to, empty when none is mounted.

    `Skills` reads its libraries through the run's workspace at run start, so a built agent holds no per-skill
    capability to look at. Its own `include` is what it was narrowed to.
    """
    names: set[str] = set()

    def _collect(cap: object) -> None:
        if isinstance(cap, Skills) and cap.include is not None:
            names.update(cap.include)

    agent._root_capability.apply(_collect)
    return frozenset(names)


class TestBuildWorkerAgentSkills:
    def test_a_worker_naming_skills_holds_only_those(self) -> None:
        agent = _worker_agent({"instructions": BRIEFING, "skills": ["agent-ux"]})

        assert _mounted_skill_ids(agent) == frozenset({"agent-ux"})

    def test_a_worker_naming_no_skills_mounts_no_skills_capability(self) -> None:
        agent = _worker_agent({"instructions": BRIEFING})

        assert _mounted_skill_ids(agent) == frozenset()

    def test_a_spec_naming_the_upstream_skills_entry_directly_still_validates(self) -> None:
        spec_yaml = f"instructions: hi\ncapabilities:\n  - Skills:\n      directories: {SKILLS!s}\n      include: [agent-ux]\n"

        agent_spec = validate_agent_document(spec_yaml)

        assert [capability.name for capability in agent_spec.capabilities] == ["Skills"]


class TestAgentStoreSkillsValidation:
    def _store(self, tmp_path: Path) -> FileCustomAgents:
        return FileCustomAgents(tmp_path / "agents", activations=FakeActivationLog())

    def test_a_known_skill_validates(self, tmp_path: Path) -> None:
        record = self._store(tmp_path).write("weigher", f"instructions: {BRIEFING!r}\nskills: [agent-ux]\n")

        assert record.last_error is None

    def test_an_unknown_skill_is_refused_naming_the_registry(self, tmp_path: Path) -> None:
        record = self._store(tmp_path).write("weigher", f"instructions: {BRIEFING!r}\nskills: [nope]\n")

        assert record.last_error is not None
        assert "'nope'" in record.last_error
        assert "agent-ux" in record.last_error

    def test_validate_agent_document_raises_for_an_unknown_skill(self) -> None:
        with pytest.raises(AgentValidationError, match="nope"):
            validate_agent_document(f"instructions: {BRIEFING!r}\nskills: [nope]\n")


class TestAgentDocumentRefusesAWorkspace:
    """The harness decides an agent's workspace, so a document may not bring one."""

    def test_a_document_naming_local_workspace_is_refused(self) -> None:
        with pytest.raises(AgentValidationError, match="LocalWorkspace"):
            validate_agent_document(f"instructions: {BRIEFING!r}\ncapabilities:\n  - LocalWorkspace: /\n")

    def test_a_document_without_one_still_validates(self) -> None:
        validate_agent_document(f"instructions: {BRIEFING!r}\ncapabilities:\n  - WebSearch\n")

    def test_a_wrapper_around_a_capability_without_one_validates(self) -> None:
        validate_agent_document(
            f"instructions: {BRIEFING!r}\ncapabilities:\n  - PrefixTools:\n      prefix: web\n      capability: WebSearch\n"
        )

    def test_a_wrapper_around_local_workspace_is_refused_naming_local_workspace(self) -> None:
        with pytest.raises(AgentValidationError, match="'LocalWorkspace'"):
            validate_agent_document(
                f"instructions: {BRIEFING!r}\ncapabilities:\n"
                "  - PrefixTools:\n      prefix: ws\n      capability:\n        LocalWorkspace: /\n"
            )

    def test_a_custom_wrapper_that_supplies_its_own_workspace_is_refused(self) -> None:
        with pytest.raises(AgentValidationError, match="'OwnWorkspaceWrapper'"):
            validate_agent_document(
                f"instructions: {BRIEFING!r}\ncapabilities:\n  - OwnWorkspaceWrapper:\n      wrapped: WebSearch\n",
                custom_capability_types=[_OwnWorkspaceWrapper],
            )


@dataclass
class _OwnWorkspaceWrapper(WrapperCapability[object]):
    @classmethod
    def get_serialization_name(cls) -> str:
        return "OwnWorkspaceWrapper"

    def get_workspace(self, ctx: RunContext[object], *, ref: WorkspaceRef | None) -> WorkspaceBackend | None:
        return LocalWorkspaceBackend("/")


KNOWLEDGE_BASE_WITH_SKILLS = """\
description: Retrieval work only.
plugins: [knowledge-base]
skills: [agent-ux]
"""

KNOWLEDGE_BASE_WITH_UNKNOWN_SKILL = """\
description: Retrieval work only.
plugins: [knowledge-base]
skills: [nope]
"""


class TestProfileSkillsSelection:
    def _store(self, tmp_path: Path) -> FileCustomProfiles:
        return FileCustomProfiles(tmp_path / "custom" / "profiles", activations=FakeActivationLog())

    def test_a_known_skill_validates(self, tmp_path: Path) -> None:
        record = self._store(tmp_path).write("researcher", KNOWLEDGE_BASE_WITH_SKILLS)

        assert record.last_error is None

    def test_an_unknown_skill_is_refused_naming_the_registry(self, tmp_path: Path) -> None:
        record = self._store(tmp_path).write("researcher", KNOWLEDGE_BASE_WITH_UNKNOWN_SKILL)

        assert record.last_error is not None
        assert "'nope'" in record.last_error
        assert "agent-ux" in record.last_error

    def test_selecting_a_profile_applies_its_skills(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.write("researcher", KNOWLEDGE_BASE_WITH_SKILLS)
        store.activate("researcher")

        select_profile("researcher", root=tmp_path / "custom", state=a_state(tmp_path))

        assert selected_profile_skills(a_state(tmp_path)) == ("agent-ux",)

    def test_no_selection_means_no_skills_override(self, tmp_path: Path) -> None:
        assert selected_profile_skills(a_state(tmp_path)) is None


class TestChatAgentSkillsCatalog:
    """`_SkillsForSelection`: the chat agent's dynamic include over the profile selection."""

    def _resolve(self, tmp_path: Path) -> Skills[Runtime]:
        capability = _SkillsForSelection([SKILLS, ENGRAM_SKILLS], a_state(tmp_path))
        resolved = capability._resolve(cast("RunContext[Runtime]", None))
        assert isinstance(resolved, Skills)
        return resolved

    def test_no_profile_selected_gives_the_orchestrator_default(self, tmp_path: Path) -> None:
        skills = self._resolve(tmp_path)

        assert skills.include == frozenset(ORCHESTRATOR_SKILLS)

    def test_a_selected_profiles_skills_become_the_catalog(self, tmp_path: Path) -> None:
        plugins.apply_selection(a_state(tmp_path), plugins.Selection(name="researcher", plugins=("knowledge-base",), skills=("agent-ux",)))

        skills = self._resolve(tmp_path)

        assert skills.include == frozenset({"agent-ux"})

    def test_an_active_profile_naming_no_skills_holds_none(self, tmp_path: Path) -> None:
        plugins.apply_selection(a_state(tmp_path), plugins.Selection(name="researcher", plugins=("knowledge-base",), skills=()))

        skills = self._resolve(tmp_path)

        assert skills.include == frozenset()


class TestListSkills:
    def test_list_skills_answers_every_registry_line(self) -> None:
        lines = list_skills_lines(CHECKOUT, ProjectConfig()).splitlines()

        assert len(lines) == 14
        assert all(": " in line for line in lines)
        assert any(line.startswith("agent-ux: ") for line in lines)
        assert any(line.startswith("documenting: ") for line in lines)

    async def test_the_toolset_answers_the_same_lines(self) -> None:
        toolset = SkillsPluginToolset[object](CHECKOUT, ProjectConfig())

        reply = await toolset.list_skills()

        assert reply == list_skills_lines(CHECKOUT, ProjectConfig())
