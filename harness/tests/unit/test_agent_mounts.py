"""The project's context policy, mounted: which compactor `strategy` selects, and recall's cap.

The mount is proved through `build_agent`, introspecting the built agent's own capability list the
way `test_flags.py` proves the friction mount -- a future rewrite of the capabilities list is
caught the moment it drops a plugin or stops honouring the config.
"""

from __future__ import annotations

import re

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

from core.ai.empty_answer import EmptyAnswerRetry
from core.ai.shell import PathGuardedShell
from core.ai.voice import RuntimeVoiced
from core.ai.workflows.contracts import StepRegistry, WorkflowContract
from core.ai.workflows.infrastructure.folder import FOLDER, folder_toolset
from pydantic_ai.capabilities import AbstractCapability, CombinedCapability, Toolset
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ModelResponse, TextContent, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.anthropic import AnthropicCompaction
from pydantic_ai.models.test import TestModel
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness import DynamicWorkflow, SubAgents
from pydantic_ai_harness.compaction import ReportContextUsage, TieredCompaction
from pydantic_ai_harness.conversation_search import ConversationSearch
from pydantic_ai_harness.filesystem import FileSystem, FileSystemToolset
from pydantic_ai_harness.memory import InMemoryStore
from pydantic_ai_harness.spend import SpendLimits
from pydantic_ai_harness.system_reminders import SystemReminders

from harness import plugins
from harness.adapters import models
from harness.adapters.model_list import ModelList
from harness.adapters.model_tiers import ModelTiers
from harness.domain.contracts import SANDBOX
from harness.features.authoring.agent import (
    FILES_DENIED_PATTERNS,
    SELF_AUTHORING_CAPABILITY_ID,
    _always,
    _in_code_mode,
    _SkillsForSelection,
    build_agent,
)
from harness.features.authoring.process import WorkflowProcess
from harness.features.authoring.workers import DELEGATE_TOOL, EXTERNAL_AGENT_TOOL, SCRIPT_TOOL
from harness.features.projects.memory import memory_mounts
from harness.features.running.spend_report import SpendReporter
from harness.features.scheduling.contracts import Scheduler
from harness.features.sessions.project_config import CompactionConfig, ProjectConfig
from harness.session import NoSessionLog, Runtime, SessionPool
from harness.settings import CHECKOUT, WORKSPACE, RunSettings, WebSearchSettings
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.request_parameters import FakeRequestParametersSink
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.support.fakes.telemetry import FakeTelemetry
from tests.support.runtime import a_runtime
from tests.support.state import a_state, a_throwaway_state, a_transcript, records_under
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from core.ai.workflows.contracts import WorkflowLibrary
    from core.thinking import Thinking
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models import Model
    from pydantic_ai.models.function import AgentInfo, FunctionModel
    from pydantic_ai.tools import DeferredToolRequests


@dataclass
class _NoResearch(AbstractCapability[Runtime]):
    """Fills `build_agent`'s required research slot; the mount under test is the context policy."""


def _fake_build(model_id: str, *, thinking: Thinking) -> Model:
    """A `build` seam with no gateway: every tier answers a fresh `TestModel`."""
    del model_id, thinking
    return TestModel()


def _build(
    project: ProjectConfig, tmp_path: Path, *, model: Model | None = None,
    delegation: bool = False,
    save_capability_id: str | None = None,
    skill_hint: AbstractCapability[Runtime] | None = None,
) -> Agent[Runtime, str | DeferredToolRequests]:
    port = WorkflowContract[Runtime](
        source=folder_toolset(tmp_path / "workflows"),
        vocabulary=FOLDER,
        expose=(),
        registry=StepRegistry(),
        saved_by="test",
        save_capability_id=save_capability_id,
    )
    return build_agent(
        model if model is not None else TestModel(),
        WorkflowProcess(port=port, records=records_under(tmp_path), reporter=SpendReporter(FakeTelemetry()), scheduler=Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()), sessions=APool(SessionPool(a_runtime())), topics=FakeBroadcaster(), run=RunSettings()),
        _NoResearch(),
        state=a_state(tmp_path),
        custom=tmp_path / "custom",
        web_search=WebSearchSettings(),
        store=a_transcript(tmp_path),
        parameters=FakeRequestParametersSink(),
        external_agents=dict,
        project=project,
        tiers=ModelTiers(state=a_state(tmp_path), models=ModelList(listing=None), build=_fake_build) if delegation else None,
        skill_hint=skill_hint,
    )


class TestCompactionMount:
    def test_the_tiered_strategy_mounts_tiered_compaction_at_its_configured_target(self, tmp_path: Path) -> None:
        project = ProjectConfig(compaction=CompactionConfig(strategy="tiered", target_tokens=42_000))
        agent = _build(project, tmp_path)

        tiered: list[TieredCompaction[Runtime]] = []
        agent._root_capability.apply(lambda cap: tiered.append(cap) if isinstance(cap, TieredCompaction) else None)

        assert len(tiered) == 1
        assert tiered[0].target_tokens == 42_000

    def test_the_tiered_strategy_mounts_no_provider_native_compaction(self, tmp_path: Path) -> None:
        project = ProjectConfig(compaction=CompactionConfig(strategy="tiered"))
        agent = _build(project, tmp_path)

        provider_native: list[AnthropicCompaction[Runtime]] = []
        agent._root_capability.apply(lambda cap: provider_native.append(cap) if isinstance(cap, AnthropicCompaction) else None)

        assert provider_native == []

    def test_the_provider_native_strategy_mounts_anthropic_compaction_at_its_configured_threshold(self, tmp_path: Path) -> None:
        project = ProjectConfig(compaction=CompactionConfig(strategy="provider-native", target_tokens=99_000))
        agent = _build(project, tmp_path)

        provider_native: list[AnthropicCompaction[Runtime]] = []
        agent._root_capability.apply(lambda cap: provider_native.append(cap) if isinstance(cap, AnthropicCompaction) else None)

        assert len(provider_native) == 1
        assert provider_native[0].token_threshold == 99_000

    def test_the_provider_native_strategy_mounts_no_tiered_compaction(self, tmp_path: Path) -> None:
        project = ProjectConfig(compaction=CompactionConfig(strategy="provider-native"))
        agent = _build(project, tmp_path)

        tiered: list[TieredCompaction[Runtime]] = []
        agent._root_capability.apply(lambda cap: tiered.append(cap) if isinstance(cap, TieredCompaction) else None)

        assert tiered == []

    def test_report_context_usage_is_always_mounted_once(self, tmp_path: Path) -> None:
        agent = _build(ProjectConfig(), tmp_path)

        mounted: list[ReportContextUsage[Runtime]] = []
        agent._root_capability.apply(lambda cap: mounted.append(cap) if isinstance(cap, ReportContextUsage) else None)

        assert len(mounted) == 1

    def test_retry_on_empty_answer_is_always_mounted_once(self, tmp_path: Path) -> None:
        agent = _build(ProjectConfig(), tmp_path)

        mounted: list[EmptyAnswerRetry[Runtime]] = []
        agent._root_capability.apply(lambda cap: mounted.append(cap) if isinstance(cap, EmptyAnswerRetry) else None)

        assert len(mounted) == 1


class TestRecallMount:
    def test_recall_carries_max_matches_from_config(self, tmp_path: Path) -> None:
        project = ProjectConfig(compaction=CompactionConfig(recall_max_matches=9))
        agent = _build(project, tmp_path)

        mounted: list[plugins.Switched[Runtime]] = []

        def _collect(cap: AbstractCapability[Runtime]) -> None:
            if isinstance(cap, plugins.Switched) and cap.plugin == "recall":
                mounted.append(cap)

        agent._root_capability.apply(_collect)

        assert len(mounted) == 1
        wrapped = mounted[0].wrapped
        assert isinstance(wrapped, RuntimeVoiced)
        assert isinstance(wrapped.wrapped, ConversationSearch)
        assert wrapped.wrapped.max_matches == 9


class TestSkillsMount:
    def test_a_ghost_skills_directory_is_dropped_and_the_real_one_still_mounts(self, tmp_path: Path) -> None:
        project = ProjectConfig(skills=("definitely-not-a-real-skills-directory", "tests"))

        agent = _build(project, tmp_path)

        mounted: list[plugins.Switched[Runtime]] = []

        def _collect(cap: AbstractCapability[Runtime]) -> None:
            if isinstance(cap, plugins.Switched) and cap.plugin == "skills":
                mounted.append(cap)

        agent._root_capability.apply(_collect)

        assert len(mounted) == 1
        skills = mounted[0].wrapped
        assert isinstance(skills, _SkillsForSelection)
        assert CHECKOUT / "tests" in skills.directories
        assert CHECKOUT / "definitely-not-a-real-skills-directory" not in skills.directories


class TestFilesDoorDeniesTheNoteSidecar:
    """The files plugin's `denied_patterns` keeps a project's `*.threads.json` out of the agent's reach."""

    def test_the_mount_carries_the_sidecar_deny_pattern(self, tmp_path: Path) -> None:
        agent = _build(ProjectConfig(), tmp_path)

        mounted: list[plugins.Switched[Runtime]] = []

        def _collect(cap: AbstractCapability[Runtime]) -> None:
            if isinstance(cap, plugins.Switched) and cap.plugin == "files":
                mounted.append(cap)

        agent._root_capability.apply(_collect)

        assert len(mounted) == 1
        wrapped = mounted[0].wrapped
        assert isinstance(wrapped, CombinedCapability)
        files_plugin = next(cap for cap in wrapped.capabilities if isinstance(cap, FileSystem))
        assert "**/*.threads.json" in files_plugin.denied_patterns

    async def test_reading_a_sidecar_is_refused_through_the_built_agent(self, tmp_path: Path) -> None:
        def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
            del info
            for message in messages:
                for part in message.parts:
                    if isinstance(part, ToolReturnPart):
                        return ModelResponse(parts=[TextPart(str(part.content))])
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "probe.md.threads.json"})])

        agent = _build(ProjectConfig(), tmp_path, model=streaming_function_model(function))
        runtime = Runtime(
            registry=StepRegistry(), library=cast("WorkflowLibrary", None), agent=cast("Agent[Runtime, str | DeferredToolRequests]", object()), session_log=NoSessionLog(), state=a_state(tmp_path)
        )
        result = await agent.run("read the note sidecar for probe.md", deps=runtime)

        output = result.output
        assert isinstance(output, str)
        assert "denied" in output.lower()


class TestFilesDoorDeniesCredentials:
    """The files plugin's `denied_patterns` keeps the shared settings file, `.env` files and the external agents registry out of the agent's reach."""

    async def test_reading_the_settings_file_at_the_root_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "application_settings.json").write_text("{}", encoding="utf-8")
        toolset = FileSystem[Runtime](root_dir=tmp_path, denied_patterns=list(FILES_DENIED_PATTERNS)).get_toolset()
        assert isinstance(toolset, FileSystemToolset)

        with pytest.raises(ModelRetry):
            await toolset.read_file("application_settings.json", workspace=LocalWorkspaceBackend(tmp_path))

    async def test_reading_the_settings_file_under_a_subdirectory_is_refused(self, tmp_path: Path) -> None:
        nested = tmp_path / "config"
        nested.mkdir()
        (nested / "application_settings.json").write_text("{}", encoding="utf-8")
        toolset = FileSystem[Runtime](root_dir=tmp_path, denied_patterns=list(FILES_DENIED_PATTERNS)).get_toolset()
        assert isinstance(toolset, FileSystemToolset)

        with pytest.raises(ModelRetry):
            await toolset.read_file("config/application_settings.json", workspace=LocalWorkspaceBackend(tmp_path))

    async def test_reading_an_env_file_is_refused_at_the_root_and_under_a_subdirectory(self, tmp_path: Path) -> None:
        nested = tmp_path / "service"
        nested.mkdir()
        for path in (tmp_path / ".env", tmp_path / ".env.local", nested / ".env"):
            path.write_text("KEY=value", encoding="utf-8")
        toolset = FileSystem[Runtime](root_dir=tmp_path, denied_patterns=list(FILES_DENIED_PATTERNS)).get_toolset()
        assert isinstance(toolset, FileSystemToolset)

        for name in (".env", ".env.local", "service/.env"):
            with pytest.raises(ModelRetry):
                await toolset.read_file(name, workspace=LocalWorkspaceBackend(tmp_path))

    async def test_reading_the_external_agents_registry_under_a_state_home_is_refused(self, tmp_path: Path) -> None:
        registry = a_state(tmp_path / "harness" / ".state").external_agents
        registry.parent.mkdir(parents=True)
        registry.write_text('{"coder": {"command": "coder", "env": {"PROVIDER_API_KEY": "secret"}}}', encoding="utf-8")
        toolset = FileSystem[Runtime](root_dir=tmp_path, denied_patterns=list(FILES_DENIED_PATTERNS)).get_toolset()
        assert isinstance(toolset, FileSystemToolset)

        with pytest.raises(ModelRetry):
            await toolset.read_file(str(registry.relative_to(tmp_path)), workspace=LocalWorkspaceBackend(tmp_path))


class TestShellDoorDeniesTheFilesDoorsPaths:
    """The shell plugin refuses a command naming a path the files plugin denies, before the command runs."""

    def test_the_mount_carries_the_files_plugins_deny_list(self, tmp_path: Path) -> None:
        wrapped = _switched(_build(ProjectConfig(), tmp_path))["shell"].wrapped

        assert isinstance(wrapped, CombinedCapability)
        shell = next(cap for cap in wrapped.capabilities if isinstance(cap, PathGuardedShell))
        assert tuple(shell.denied_path_patterns) == FILES_DENIED_PATTERNS

    async def test_a_command_naming_the_settings_file_is_refused_through_the_built_agent(self, tmp_path: Path) -> None:
        def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
            del info
            for message in messages:
                for part in message.parts:
                    if isinstance(part, ToolReturnPart):
                        return ModelResponse(parts=[TextPart(str(part.content))])
            return ModelResponse(parts=[ToolCallPart("run_command", {"command": "cat no-such-probe/application_settings.json"})])

        agent = _build(ProjectConfig(), tmp_path, model=streaming_function_model(function))
        runtime = Runtime(
            registry=StepRegistry(), library=cast("WorkflowLibrary", None), agent=cast("Agent[Runtime, str | DeferredToolRequests]", object()), session_log=NoSessionLog(), state=a_state(tmp_path)
        )
        result = await agent.run("read the credentials", deps=runtime)

        assert result.output == "Path 'no-such-probe/application_settings.json' is denied by pattern '**/application_settings.json'."

    async def test_a_command_naming_the_external_agents_registry_is_refused_through_the_built_agent(self, tmp_path: Path) -> None:
        registry = f"no-such-probe/.state/{a_state(tmp_path).external_agents.name}"

        def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
            del info
            for message in messages:
                for part in message.parts:
                    if isinstance(part, ToolReturnPart):
                        return ModelResponse(parts=[TextPart(str(part.content))])
            return ModelResponse(parts=[ToolCallPart("run_command", {"command": f"cat {registry}"})])

        agent = _build(ProjectConfig(), tmp_path, model=streaming_function_model(function))
        runtime = Runtime(
            registry=StepRegistry(), library=cast("WorkflowLibrary", None), agent=cast("Agent[Runtime, str | DeferredToolRequests]", object()), session_log=NoSessionLog(), state=a_state(tmp_path)
        )
        result = await agent.run("read the external agents' keys", deps=runtime)

        assert result.output == f"Path '{registry}' is denied by pattern '**/external-agents.json'."


class TestToolRetries:
    """A model gets three tries at a tool call before the third failure ends the turn; output retries stay two.

    Read from `_max_tool_retries`/`_max_output_retries`: pydantic-ai resolves `Agent(retries=...)` onto these
    dataclass fields and exposes no public accessor for the resolved values.
    """

    def test_the_prompted_output_construction_resolves_three_tool_retries_and_two_output_retries(self, tmp_path: Path) -> None:
        port = WorkflowContract[Runtime](
            source=folder_toolset(tmp_path / "workflows"), vocabulary=FOLDER, expose=(), registry=StepRegistry(), saved_by="test"
        )
        agent = build_agent(
            TestModel(),
            WorkflowProcess(port=port, records=records_under(tmp_path), reporter=SpendReporter(FakeTelemetry()), scheduler=Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()), sessions=APool(SessionPool(a_runtime())), topics=FakeBroadcaster(), run=RunSettings()),
            _NoResearch(),
            state=a_state(tmp_path),
            custom=tmp_path / "custom",
            web_search=WebSearchSettings(),
            store=a_transcript(tmp_path),
            parameters=FakeRequestParametersSink(),
            external_agents=dict,
            project=ProjectConfig(),
        )

        assert agent._max_tool_retries == 3
        assert agent._max_output_retries == 2

    def test_the_str_output_construction_resolves_three_tool_retries_and_two_output_retries(self, tmp_path: Path) -> None:
        agent = _build(ProjectConfig(), tmp_path)

        assert agent._max_tool_retries == 3
        assert agent._max_output_retries == 2


def _switched[OutputT](agent: Agent[Runtime, OutputT]) -> dict[str, plugins.Switched[Runtime]]:
    mounted: dict[str, plugins.Switched[Runtime]] = {}

    def _collect(cap: AbstractCapability[Runtime]) -> None:
        if isinstance(cap, plugins.Switched):
            mounted[cap.plugin] = cap

    agent._root_capability.apply(_collect)
    return mounted


class TestDelegationMount:
    def test_both_doors_mount_when_a_build_seam_is_given(self, tmp_path: Path) -> None:
        agent = _build(ProjectConfig(), tmp_path, delegation=True)

        mounted = _switched(agent)

        assert isinstance(mounted["delegate"].wrapped, SubAgents)
        assert isinstance(mounted["agent-scripts"].wrapped, DynamicWorkflow)
        assert isinstance(mounted["external-agent"].wrapped, Toolset)

    def test_no_build_seam_mounts_neither_door(self, tmp_path: Path) -> None:
        agent = _build(ProjectConfig(), tmp_path)

        mounted = _switched(agent)

        assert "delegate" not in mounted
        assert "agent-scripts" not in mounted

    def test_both_doors_are_in_the_toggleable_catalog(self, tmp_path: Path) -> None:
        toggleable = {plugin.name for plugin in plugins.TOGGLEABLE}

        assert {"delegate", "agent-scripts", "external-agent"} <= toggleable
        assert plugins.plugin_enabled(a_state(tmp_path), "delegate")
        assert plugins.plugin_enabled(a_state(tmp_path), "agent-scripts")
        assert plugins.plugin_enabled(a_state(tmp_path), "external-agent")

    def test_the_delegation_tools_are_acts_not_code_mode(self) -> None:
        assert not _in_code_mode("delegate_task", _always)
        assert not _in_code_mode("run_agent_script", _always)
        assert not _in_code_mode(EXTERNAL_AGENT_TOOL, _always)

    def test_the_writes_that_carry_a_document_are_direct_calls(self) -> None:
        """Edit ops, a spec, Python source: as tool-call JSON, never as a literal inside a Monty program."""
        for name in ("adapt_workflow", "save_workflow", "write_custom_capability", "write_custom_step", "write_custom_agent", "write_custom_profile"):
            assert not _in_code_mode(name, _always), name

    def test_the_workers_share_the_agents_own_spend_brake(self, tmp_path: Path) -> None:
        agent = _build(ProjectConfig(), tmp_path, delegation=True)
        agent_brakes: list[SpendLimits[Runtime]] = []
        agent._root_capability.apply(lambda cap: agent_brakes.append(cap) if isinstance(cap, SpendLimits) else None)
        delegate = _switched(agent)["delegate"].wrapped
        assert isinstance(delegate, SubAgents)
        (worker,) = delegate.agents

        worker_brakes: list[SpendLimits[Runtime]] = []
        worker_agent = cast("Agent[Runtime, str]", worker.agent)
        worker_agent._root_capability.apply(lambda cap: worker_brakes.append(cap) if isinstance(cap, SpendLimits) else None)

        assert len(agent_brakes) == 1
        assert agent_brakes[0].price is models.price_response
        assert worker_brakes == agent_brakes


def _offered_tool_names(info: AgentInfo) -> set[str]:
    """The tools a request offers the model, the ones `run_code` holds in its catalog included."""
    names = {tool.name for tool in info.function_tools}
    run_code = next((tool for tool in info.function_tools if tool.name == SANDBOX), None)
    catalog = run_code.description or "" if run_code is not None else ""
    return names | set(re.findall(r"async def (\w+)\(", catalog))


def _captured_params(function: Callable[[Sequence[ModelMessage], AgentInfo], ModelResponse]) -> FunctionModel:
    return streaming_function_model(function)


async def _run[OutputT](agent: Agent[Runtime, OutputT]) -> None:
    """Run `agent` over a session of its own -- `SessionStepPersistence` writes into that session's transcript."""
    runtime = Runtime(
        registry=StepRegistry(), library=cast("WorkflowLibrary", None), agent=cast("Agent[Runtime, str | DeferredToolRequests]", object()), session_log=NoSessionLog(), state=a_throwaway_state()
    )
    await agent.run("hi", deps=runtime)


_IDENTITY_CONTEXT = '<context id="capability:managed-prompt-studio_author">'
"""How the identity block opens: pydantic-ai frames a framework capability's dynamic block as a `<context>` element."""


def _context_body(content: str) -> str:
    """An instruction part's text without the `<context id="...">` element pydantic-ai frames an append-mode block in."""
    if not content.startswith("<context id="):
        return content
    return content.split(">\n", 1)[1].split("\n</context>", 1)[0]


class TestInstructionOrder:
    """The studio identity (`ManagedPrompt`) sorts first among capability-contributed instructions.

    pydantic-ai's own `InstructionPart.sorted` always sorts literal/static instruction text ahead
    of function-shaped/dynamic text (a cache-placement rule, not something this agent's
    composition controls), and `ManagedPrompt`'s resolved value is necessarily function-shaped --
    it depends on the active run's Logfire resolution, unknown until `wrap_run` fires. So the
    identity can be first AMONG the dynamic contributions, never ahead of a literal one still
    mounted. NoteOverlay and the memory guidance contribute dynamically for exactly that reason;
    `delegate_task`'s menu (vendor-owned, static by design) stays ahead of it whenever `delegate`
    has already been loaded this run.
    """

    async def test_studio_identity_is_the_first_capability_instruction(self, tmp_path: Path) -> None:
        captured: dict[str, AgentInfo] = {}

        def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
            captured["info"] = info
            return ModelResponse(parts=[TextPart("done")])

        agent = _build(ProjectConfig(), tmp_path, model=_captured_params(function))
        await _run(agent)

        parts = captured["info"].model_request_parameters.instruction_parts
        assert parts, "the built agent contributed no instructions at all"
        assert parts[0].content.startswith(f"{_IDENTITY_CONTEXT}\nYou are a coding agent working against a real machine.")

    async def test_the_shell_and_files_situating_sentences_render_beside_the_identity(self, tmp_path: Path) -> None:
        """The two plugins' situating sentences carry the resolved `WORKSPACE`, after the identity opens the prompt."""
        captured: dict[str, AgentInfo] = {}

        def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
            captured["info"] = info
            return ModelResponse(parts=[TextPart("done")])

        agent = _build(ProjectConfig(), tmp_path, model=_captured_params(function))
        await _run(agent)

        parts = captured["info"].model_request_parameters.instruction_parts
        assert parts, "the built agent contributed no instructions at all"
        contents = [_context_body(part.content) for part in parts]
        assert contents[0].startswith("You are a coding agent working against a real machine.")
        shell = next(text for text in contents if text.startswith("Commands run in "))
        files = next(text for text in contents if text.startswith("File paths are relative to "))
        assert shell.startswith(f"Commands run in {WORKSPACE}.")
        assert files.startswith(f"File paths are relative to {WORKSPACE}, nothing outside it is reachable.")
        assert "use the files plugin" in shell
        assert "use the shell" in files


class TestSelfAuthoringDeferral:
    """`save_workflow`, `run_agent_script`, and the four `author_*` tools defer. `delegate_task` never does."""

    async def test_a_fresh_agents_function_tools_exclude_the_six(self, tmp_path: Path) -> None:
        captured: dict[str, AgentInfo] = {}

        def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
            captured["info"] = info
            return ModelResponse(parts=[TextPart("done")])

        agent = _build(
            ProjectConfig(), tmp_path, model=_captured_params(function), delegation=True, save_capability_id="saving-a-workflow"
        )
        await _run(agent)

        tools = {td.name: td for td in captured["info"].model_request_parameters.function_tools}
        deferred = {
            "save_workflow": "saving-a-workflow",
            SCRIPT_TOOL: "agent-scripts",
            "write_custom_capability": SELF_AUTHORING_CAPABILITY_ID,
            "write_custom_step": SELF_AUTHORING_CAPABILITY_ID,
            "write_custom_agent": SELF_AUTHORING_CAPABILITY_ID,
            "write_custom_profile": SELF_AUTHORING_CAPABILITY_ID,
        }
        for name, capability_id in deferred.items():
            assert tools[name].defer_loading is True, f"{name} is not deferred"
            assert tools[name].capability_id == capability_id, f"{name} carries the wrong capability id"
        # The delegation plugin is on the belt from the first step: a plugin the orchestrator must load
        # before it can see it is a plugin it goes looking for elsewhere.
        assert tools[DELEGATE_TOOL].defer_loading is False, "delegate_task must never be deferred"

    async def test_the_run_verb_stays_always_on(self, tmp_path: Path) -> None:
        """The deferral is per-tool, not per-capability: `run_workflow` (in `ACTS`) must stay reachable.

        `search_workflows`/`fetch_workflow`/`adapt_workflow` are not asserted here: unlike
        `run_workflow` and `save_workflow` (both in `agent.ACTS`), CodeMode folds them into
        `run_code`'s own catalog by default, so they never appear as their own top-level
        `function_tools` entry to begin with. `WorkflowContract`'s own unit tests
        (`core/tests/unit/workflows/test_contract_port.py`) cover their `defer_loading` directly.
        """
        captured: dict[str, AgentInfo] = {}

        def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
            captured["info"] = info
            return ModelResponse(parts=[TextPart("done")])

        agent = _build(
            ProjectConfig(), tmp_path, model=_captured_params(function), delegation=True, save_capability_id="saving-a-workflow"
        )
        await _run(agent)

        tools = {td.name: td for td in captured["info"].model_request_parameters.function_tools}
        assert tools["run_workflow"].defer_loading is False

    @pytest.mark.parametrize(
        ("capability_id", "tool_name"),
        [("saving-a-workflow", "save_workflow"), ("agent-scripts", SCRIPT_TOOL), (SELF_AUTHORING_CAPABILITY_ID, "write_custom_capability")],
    )
    async def test_loading_the_capability_reveals_its_tool(self, tmp_path: Path, capability_id: str, tool_name: str) -> None:
        """`load_capability` is the real mechanism: after loading, the tool is revealed on the wire.

        `FunctionModel` declares no `tool_deferral_mode` (that is an Anthropic-style schema
        withholding signal), so the reveal reaches it the channel-less way: a plain system message
        naming the newly-available tool(s), read here from the next request's own history rather
        than from `revealed_tool_names` (which only a schema-withholding model populates).
        """
        seen: list[Sequence[ModelMessage]] = []
        offered: list[set[str]] = []

        def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
            seen.append(messages)
            offered.append(_offered_tool_names(info))
            if len(seen) == 1:
                return ModelResponse(parts=[ToolCallPart("load_capability", {"id": capability_id})])
            return ModelResponse(parts=[TextPart("done")])

        agent = _build(
            ProjectConfig(), tmp_path, model=_captured_params(function), delegation=True, save_capability_id="saving-a-workflow"
        )
        await _run(agent)

        assert len(seen) == 2, "the load must have succeeded outright, not retried"
        text = "\n".join(
            item.content if isinstance(item, TextContent) else str(item)
            for message in seen[-1]
            for part in message.parts
            if isinstance(part, UserPromptPart)
            for item in ([part.content] if isinstance(part.content, str) else part.content)
        )
        assert f"now available: `{tool_name}`" in text or tool_name in text
        assert tool_name not in offered[0], "the tool must stay hidden until the load"
        assert tool_name in offered[1], "the load must make the tool callable, not only name it"


class TestRunCodeCatalogSize:
    """`run_code.description`'s size, with everything that folds into it actually mounted.

    A regression guard, not the full picture. With the full catalog the description measures about
    15.6 KB, and the ceiling leaves about 30% headroom over that, so a real regression shows without
    chasing the exact byte count turn to turn.
    """

    async def test_the_folded_catalog_stays_within_a_reasonable_ceiling(self, tmp_path: Path) -> None:
        mounts = memory_mounts(InMemoryStore(), project_namespace="ceiling-test", global_memory_enabled=lambda: True)
        mem = (plugins.Switched(CombinedCapability(capabilities=list(mounts)), plugin="memory", state=a_state(tmp_path)),)
        captured: dict[str, AgentInfo] = {}

        def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
            captured["info"] = info
            return ModelResponse(parts=[TextPart("done")])

        agent = build_agent(
            _captured_params(function),
            WorkflowProcess(
                port=WorkflowContract[Runtime](
                    source=folder_toolset(tmp_path / "workflows"),
                    vocabulary=FOLDER,
                    expose=(),
                    registry=StepRegistry(),
                    saved_by="test",
                    save_capability_id="saving-a-workflow",
                ),
                records=records_under(tmp_path),
                reporter=SpendReporter(FakeTelemetry()),
                scheduler=Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()),
                sessions=APool(SessionPool(a_runtime())),
                topics=FakeBroadcaster(),
                run=RunSettings(),
            ),
            _NoResearch(),
            state=a_state(tmp_path),
            custom=tmp_path / "custom",
            web_search=WebSearchSettings(),
            store=a_transcript(tmp_path),
            parameters=FakeRequestParametersSink(),
            external_agents=dict,
            project=ProjectConfig(),
            memory=mem,
            tiers=ModelTiers(state=a_state(tmp_path), models=ModelList(listing=None), build=_fake_build),
        )
        await _run(agent)

        tools = {td.name: td for td in captured["info"].model_request_parameters.function_tools}
        run_code_description = tools["run_code"].description
        assert run_code_description is not None
        assert len(run_code_description.encode()) < 20_000


class TestSkillHintMount:
    def test_the_hint_mounts_beside_the_skills_behind_the_same_switch(self, tmp_path: Path) -> None:
        hint = SystemReminders[Runtime](dynamic_reminders=[lambda _ctx: None])

        agent = _build(ProjectConfig(), tmp_path, skill_hint=hint)

        mounted: list[plugins.Switched[Runtime]] = []
        agent._root_capability.apply(lambda cap: mounted.append(cap) if isinstance(cap, plugins.Switched) and cap.plugin == "skills" else None)
        assert len(mounted) == 1
        combined = mounted[0].wrapped
        assert isinstance(combined, CombinedCapability)
        assert any(isinstance(part, _SkillsForSelection) for part in combined.capabilities)
        assert hint in combined.capabilities

    def test_without_a_hint_the_skills_mount_alone(self, tmp_path: Path) -> None:
        agent = _build(ProjectConfig(), tmp_path)

        mounted: list[plugins.Switched[Runtime]] = []
        agent._root_capability.apply(lambda cap: mounted.append(cap) if isinstance(cap, plugins.Switched) and cap.plugin == "skills" else None)
        assert isinstance(mounted[0].wrapped, _SkillsForSelection)
