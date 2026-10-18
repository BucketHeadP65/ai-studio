"""The worker roster and the two delegation capabilities: one worker, three tiers, bounded budgets.

`build` is the seam: a recording factory stands in for the gateway, so the tier table is proved
against which id a tier asks for once it actually runs, with no credentials and no network. A
`TierModel` builds on first request, not at composition, so the factory records nothing until a
test runs one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from core.ai.shell import PathGuardedShell
from pydantic_ai import Agent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness import DynamicWorkflow, SubAgents
from pydantic_ai_harness.filesystem import FileSystem
from pydantic_ai_harness.shell import Shell
from pydantic_ai_harness.spend import Budget, SpendLimits
from pydantic_ai_harness.subagents import ModelOption

from harness.adapters.model_list import ModelList
from harness.adapters.model_tiers import ModelTiers, TierModel
from harness.features.authoring import workers
from harness.plugins import Switched
from harness.session import Runtime
from tests.support.runtime import a_runtime
from tests.support.state import a_state

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from core.thinking import Thinking
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models import Model
    from pydantic_ai.models.function import AgentInfo


@dataclass
class _NoReads(AbstractCapability[Runtime]):
    """Stands in for the knowledge bases' read plugin."""


@dataclass
class _RecordingModels:
    """Answers every id with a fresh `TestModel` and remembers which ids were asked for."""

    asked: list[str] = field(default_factory=list)
    built: dict[str, Model] = field(default_factory=dict)

    def __call__(self, model_id: str, *, thinking: Thinking) -> Model:
        del thinking
        self.asked.append(model_id)
        return self.built.setdefault(model_id, TestModel())


def _spend() -> SpendLimits[Runtime]:
    return SpendLimits(budgets=[Budget(usd=Decimal(1), window="run")])


def _tiers(home: Path, build: _RecordingModels) -> ModelTiers:
    """Tier resolution under `home`, over the pinned catalog alone, building through `build`."""
    return ModelTiers(state=a_state(home), models=ModelList(listing=None), build=build)


def _collect[T](agent: Agent[Runtime, str], kind: type[T]) -> list[T]:
    found: list[T] = []
    agent._root_capability.apply(lambda cap: found.append(cap) if isinstance(cap, kind) else None)
    return found


class TestTiers:
    def test_the_three_tiers_in_order(self) -> None:
        assert [tier.key for tier in workers.TIERS] == ["quick", "standard", "deep"]

    def test_the_three_keys_map_onto_the_three_tiers(self) -> None:
        assert {tier.key: tier.tier for tier in workers.TIERS} == {"quick": "helper", "standard": "worker", "deep": "orchestrator"}

    def test_no_tier_names_a_model_or_a_level(self) -> None:
        for tier in workers.TIERS:
            assert not hasattr(tier, "model_id")
            assert not hasattr(tier, "settings")

    def test_standard_is_the_default(self) -> None:
        assert workers.DEFAULT_TIER == "standard"

    def test_the_keys_sort_work_by_how_hard_it_is(self) -> None:
        described = {tier.key: tier.description for tier in workers.TIERS}
        assert described["quick"].startswith("mechanical edits")
        assert described["standard"].startswith("ordinary implementation")
        assert described["deep"].startswith("design judgment")


class TestWorker:
    def test_a_worker_holds_shell_files_and_reads_behind_the_parents_doors(self, tmp_path: Path) -> None:
        reads = _NoReads()
        spend = _spend()

        worker = workers.worker_agent(
            "worker", model=None, settings=None, description="d", reads=reads, spend=spend, state=a_state(tmp_path)
        )

        assert worker.name == "worker"
        switched = {plugin.plugin: plugin.wrapped for plugin in _collect(worker, Switched)}
        assert isinstance(switched["shell"], PathGuardedShell)
        assert isinstance(switched["shell"].shell, Shell)
        assert isinstance(switched["files"], FileSystem)
        assert switched["knowledge-base"] is reads
        assert _collect(worker, SpendLimits) == [spend]

    def test_a_worker_cannot_delegate(self, tmp_path: Path) -> None:
        worker = workers.worker_agent(
            "worker", model=None, settings=None, description="d", reads=None, spend=_spend(), state=a_state(tmp_path)
        )

        assert _collect(worker, SubAgents) == []
        assert _collect(worker, DynamicWorkflow) == []

    def test_a_worker_without_a_read_door_mounts_no_knowledge_base_door(self, tmp_path: Path) -> None:
        worker = workers.worker_agent(
            "worker", model=None, settings=None, description="d", reads=None, spend=_spend(), state=a_state(tmp_path)
        )

        assert {plugin.plugin for plugin in _collect(worker, Switched)} == {"shell", "files"}


class TestWorkerShellDeniesTheWorkerFilesPaths:
    """A worker's shell refuses a command naming a path its files plugin denies, before the command runs."""

    def test_the_shell_carries_the_files_plugins_deny_list(self, tmp_path: Path) -> None:
        worker = workers.worker_agent(
            "worker", model=None, settings=None, description="d", reads=None, spend=_spend(), state=a_state(tmp_path)
        )

        switched = {plugin.plugin: plugin.wrapped for plugin in _collect(worker, Switched)}
        shell, files = switched["shell"], switched["files"]
        assert isinstance(shell, PathGuardedShell)
        assert isinstance(files, FileSystem)
        assert tuple(shell.denied_path_patterns) == tuple(files.denied_patterns) == workers.FILES_DENIED_PATTERNS

    async def test_a_command_naming_a_notes_sidecar_is_refused_through_the_worker(self, tmp_path: Path) -> None:
        def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
            del info
            for message in messages:
                for part in message.parts:
                    if isinstance(part, RetryPromptPart | ToolReturnPart):
                        return ModelResponse(parts=[TextPart(str(part.content))])
            return ModelResponse(parts=[ToolCallPart("run_command", {"command": "cat no-such-probe/x.md.threads.json"})])

        state = a_state(tmp_path)
        spend = SpendLimits[Runtime](budgets=[Budget(tokens=1_000_000, window="run")])
        worker = workers.worker_agent(
            "worker", model=FunctionModel(function), settings=None, description="d", reads=None, spend=spend, state=state
        )
        result = await worker.run("read the notes", deps=a_runtime(state=state), workspace=LocalWorkspaceBackend(tmp_path))

        assert result.output == "Path 'no-such-probe/x.md.threads.json' is denied by pattern '**/*.threads.json'."

    async def test_a_command_naming_the_settings_file_is_refused_through_the_worker(self, tmp_path: Path) -> None:
        output = await self._first_return(tmp_path, ToolCallPart("run_command", {"command": "cat engram/config/application_settings.json"}))

        assert "denied" in output.lower()
        assert "application_settings.json" in output

    async def test_reading_the_settings_file_with_the_file_tool_is_refused_through_the_worker(self, tmp_path: Path) -> None:
        output = await self._first_return(tmp_path, ToolCallPart("read_file", {"path": "engram/config/application_settings.json"}))

        assert "denied" in output.lower()

    @staticmethod
    async def _first_return(tmp_path: Path, call: ToolCallPart) -> str:
        def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
            del info
            for message in messages:
                for part in message.parts:
                    if isinstance(part, RetryPromptPart | ToolReturnPart):
                        return ModelResponse(parts=[TextPart(str(part.content))])
            return ModelResponse(parts=[call])

        state = a_state(tmp_path)
        spend = SpendLimits[Runtime](budgets=[Budget(tokens=1_000_000, window="run")])
        worker = workers.worker_agent(
            "worker", model=FunctionModel(function), settings=None, description="d", reads=None, spend=spend, state=state
        )
        result = await worker.run("read the credentials", deps=a_runtime(state=state), workspace=LocalWorkspaceBackend(tmp_path))
        assert isinstance(result.output, str)
        return result.output


class TestDelegateDoor:
    def test_the_menu_is_the_tier_table(self, tmp_path: Path) -> None:
        factory = _RecordingModels()

        delegate, _ = workers.delegation_plugins(tiers=_tiers(tmp_path, factory), reads=None, spend=_spend(), state=a_state(tmp_path))

        assert list(delegate.models) == ["quick", "standard", "deep"]
        for tier in workers.TIERS:
            option = delegate.models[tier.key]
            assert isinstance(option, ModelOption)
            assert isinstance(option.model, TierModel)
            assert option.model.tier == tier.tier
            assert option.settings is None
            assert option.description == tier.description
        assert factory.asked == [], "naming a tier on the menu must never build a model"

    async def test_a_run_asks_the_build_seam_for_the_tiers_setting(self, tmp_path: Path) -> None:
        factory = _RecordingModels()
        delegate, _ = workers.delegation_plugins(tiers=_tiers(tmp_path, factory), reads=None, spend=_spend(), state=a_state(tmp_path))

        option = delegate.models["quick"]
        assert isinstance(option, ModelOption)
        probe = Agent(option.model)
        await probe.run("hello")

        assert factory.asked == ["glm-5.3-flash"]

    def test_one_worker_default_standard_and_bounded(self, tmp_path: Path) -> None:
        delegate, _ = workers.delegation_plugins(
            tiers=_tiers(tmp_path, _RecordingModels()), reads=None, spend=_spend(), state=a_state(tmp_path)
        )

        assert [sub.resolved_name for sub in delegate.agents] == ["worker"]
        (worker,) = delegate.agents
        assert worker.models is not None
        assert worker.models[0] == "standard"
        assert set(worker.models) == {"quick", "standard", "deep"}
        assert worker.usage_limits is not None
        assert worker.usage_limits.request_limit == 40
        assert worker.timeout_seconds == 600
        assert worker.max_calls == 8
        assert worker.contain_errors is True

    def test_no_disk_agents_and_no_shared_usage(self, tmp_path: Path) -> None:
        delegate, _ = workers.delegation_plugins(
            tiers=_tiers(tmp_path, _RecordingModels()), reads=None, spend=_spend(), state=a_state(tmp_path)
        )

        assert delegate.agent_folders is None
        assert delegate.forward_usage is False
        assert delegate.tool_name == workers.DELEGATE_TOOL == "delegate_task"


class TestScriptDoor:
    def test_the_catalog_is_the_three_tier_workers(self, tmp_path: Path) -> None:
        factory = _RecordingModels()

        _, scripts = workers.delegation_plugins(tiers=_tiers(tmp_path, factory), reads=None, spend=_spend(), state=a_state(tmp_path))

        names = [agent.name for agent in scripts.agents if isinstance(agent, Agent)]
        assert names == ["quick", "standard", "deep"]
        by_name = {agent.name: agent for agent in scripts.agents if isinstance(agent, Agent)}
        for tier in workers.TIERS:
            model = by_name[tier.key].model
            assert isinstance(model, TierModel)
            assert model.tier == tier.tier
            assert by_name[tier.key].model_settings is None
        assert factory.asked == [], "mounting the catalog must never build a model"

    def test_the_tool_is_renamed_deferred_and_bounded(self, tmp_path: Path) -> None:
        _, scripts = workers.delegation_plugins(
            tiers=_tiers(tmp_path, _RecordingModels()), reads=None, spend=_spend(), state=a_state(tmp_path)
        )

        assert scripts.tool_name == workers.SCRIPT_TOOL == "run_agent_script"
        assert scripts.defer_loading is True
        assert scripts.id == "agent-scripts"
        assert scripts.forward_usage is False
        assert scripts.max_agent_calls == 12
        assert scripts.sub_agent_usage_limits is not None
        assert scripts.sub_agent_usage_limits.request_limit == 40
