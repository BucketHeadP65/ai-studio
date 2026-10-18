"""The follow-me plugin as the agent mounts it: behind its own plugin, behind the session kind, and never in code mode.

The mount is proved through `build_agent`, introspecting the built agent's own capability list and the tool surface a
run actually composes, so a rewrite of the capabilities list is caught the moment it drops the plugin or widens who sees it.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from core.ai.workflows.contract_port import SAVE_TOOL
from core.ai.workflows.contracts import StepRegistry, WorkflowContract
from core.ai.workflows.infrastructure.folder import FOLDER, folder_toolset
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests

from harness import plugins
from harness.adapters.model_list import ModelList
from harness.adapters.model_tiers import ModelTiers
from harness.features.authoring.agent import _always, _in_code_mode, build_agent
from harness.features.authoring.process import WorkflowProcess
from harness.features.followme.acts import ConsoleApprovals, act_toolset
from harness.features.followme.domain import CONSOLE_ACTS, TRUST_ACTS, VERBS
from harness.features.followme.plugin import FollowMe
from harness.features.followme.verbs import verb_toolset
from harness.features.running.spend_report import SpendReporter
from harness.features.scheduling.contracts import Scheduler
from harness.features.sessions.project_config import ProjectConfig
from harness.session import NoSessionLog, Runtime, SessionPool, answerable
from harness.settings import RunSettings, WebSearchSettings
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.request_parameters import FakeRequestParametersSink
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.support.fakes.telemetry import FakeTelemetry
from tests.support.runtime import a_runtime
from tests.support.state import a_state, a_throwaway_state, a_transcript, records_under
from tests.support.streaming import streaming_function_model
from tests.unit.test_followme_slice import FakeConsole

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from core.ai.workflows.contracts import WorkflowLibrary
    from core.thinking import Thinking
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models import Model
    from pydantic_ai.models.function import AgentInfo, FunctionModel


TRUST_WORDS: tuple[str, ...] = ("activate", "promote", "demote", "approve", "save")
"""Stems of the owner's own gestures, swept over the names a run really mounts so a differently spelled tool
cannot slip past an exact-name check."""


@dataclass
class _NoResearch(AbstractCapability[Runtime]):
    """Fills `build_agent`'s required research slot, the mount under test is the follow-me plugin."""


def _follow_me() -> FollowMe:
    console = FakeConsole()
    return FollowMe(verbs=verb_toolset(), acts=act_toolset(console, console, console))


def _fake_build(model_id: str, *, thinking: Thinking) -> Model:
    """A `build` seam with no gateway: every tier answers a fresh `TestModel`."""
    del model_id, thinking
    return TestModel()


def _build(
    project: ProjectConfig, tmp_path: Path, *, model: Model | None = None, delegation: bool = False, follow_me: FollowMe | None = None
) -> Agent[Runtime, str | DeferredToolRequests]:
    port = WorkflowContract[Runtime](
        source=folder_toolset(tmp_path / "workflows"), vocabulary=FOLDER, expose=(), registry=StepRegistry(), saved_by="test"
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
        follow_me=follow_me,
    )


def _switched[OutputT](agent: Agent[Runtime, OutputT]) -> dict[str, plugins.Switched[Runtime]]:
    mounted: dict[str, plugins.Switched[Runtime]] = {}

    def _collect(cap: AbstractCapability[Runtime]) -> None:
        if isinstance(cap, plugins.Switched):
            mounted[cap.plugin] = cap

    agent._root_capability.apply(_collect)
    return mounted


def _captured_params(function: Callable[[Sequence[ModelMessage], AgentInfo], ModelResponse]) -> FunctionModel:
    return streaming_function_model(function)


def _placeholder_runtime(*, kind: str) -> Runtime:
    """A session with no real agent of its own -- only `ctx.deps` facts (like `session_kind`) are read."""
    return Runtime(
        registry=StepRegistry(),
        library=cast("WorkflowLibrary", None),
        agent=cast("Agent[Runtime, str | DeferredToolRequests]", object()),
        session_kind=kind,
        session_log=NoSessionLog(),
        state=a_throwaway_state(),
    )


async def _run[OutputT](agent: Agent[Runtime, OutputT], *, kind: str = "chat", can_answer: bool = False) -> None:
    """Run `agent` in a session of `kind`, which is what `SessionStepPersistence` writes through.

    `can_answer` runs the turn as a plugin with an approval surface, which is what offers the acts
    that wait for the person.
    """
    runtime = _placeholder_runtime(kind=kind)
    with answerable() if can_answer else nullcontext():
        await agent.run("hi", deps=runtime)


def _answers_done() -> tuple[dict[str, AgentInfo], FunctionModel]:
    """A model that answers "done" without calling anything, keeping the request's tool surface."""
    captured: dict[str, AgentInfo] = {}

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        captured["info"] = info
        return ModelResponse(parts=[TextPart("done")])

    return captured, _captured_params(function)


class TestFollowMeMount:
    def test_the_door_is_switched_and_kind_gated(self, tmp_path: Path) -> None:
        agent = _build(ProjectConfig(), tmp_path, follow_me=_follow_me())

        mounted = _switched(agent)

        assert isinstance(mounted["follow-me"].wrapped, FollowMe)
        assert mounted["follow-me"].only is not None

    def test_no_door_mounts_when_the_composition_root_gives_none(self, tmp_path: Path) -> None:
        agent = _build(ProjectConfig(), tmp_path)

        assert "follow-me" not in _switched(agent)

    async def test_a_master_session_sees_the_verbs_and_the_acts_and_a_chat_session_sees_neither(self, tmp_path: Path) -> None:
        captured, model = _answers_done()
        agent = _build(ProjectConfig(), tmp_path, model=model, follow_me=_follow_me())

        await _run(agent, kind="master", can_answer=True)
        master_tools = {td.name for td in captured["info"].model_request_parameters.function_tools}
        await _run(agent, kind="chat")
        chat_tools = {td.name for td in captured["info"].model_request_parameters.function_tools}

        assert set(VERBS) <= master_tools
        assert {"set_model", "toggle_plugin", "run_control", "decide_gate", "archive_workflow"} <= master_tools
        assert not (set(VERBS) & chat_tools)
        assert "toggle_plugin" not in chat_tools

    async def test_a_closed_door_returns_the_master_to_read_only(self, tmp_path: Path) -> None:
        captured, model = _answers_done()
        agent = _build(ProjectConfig(), tmp_path, model=model, follow_me=_follow_me())

        plugins.set_plugin(a_state(tmp_path), "follow-me", enabled=False)
        await _run(agent, kind="master")

        tools = {td.name for td in captured["info"].model_request_parameters.function_tools}
        assert not (set(VERBS) & tools)
        assert "toggle_plugin" not in tools

    async def test_no_mounted_tool_is_a_trust_act(self, tmp_path: Path) -> None:
        captured, model = _answers_done()
        agent = _build(ProjectConfig(), tmp_path, model=model, delegation=True, follow_me=_follow_me())

        await _run(agent, kind="master")

        tools = {td.name for td in captured["info"].model_request_parameters.function_tools}
        assert not set(TRUST_ACTS) & tools
        assert not any(word in name for name in tools for word in TRUST_WORDS)

    async def test_the_save_tool_leaves_the_master_surface_and_stays_in_every_other_session(self, tmp_path: Path) -> None:
        captured, model = _answers_done()
        agent = _build(ProjectConfig(), tmp_path, model=model, delegation=True, follow_me=_follow_me())

        await _run(agent, kind="master")
        master_tools = {td.name for td in captured["info"].model_request_parameters.function_tools}
        await _run(agent, kind="chat")
        chat_tools = {td.name for td in captured["info"].model_request_parameters.function_tools}

        assert SAVE_TOOL in chat_tools, "the authoring agent saves in an ordinary session"
        assert SAVE_TOOL not in master_tools

    def test_console_acts_and_verbs_are_never_code_mode(self) -> None:
        for name in (*VERBS, *CONSOLE_ACTS):
            assert not _in_code_mode(name, _always), name
        assert _in_code_mode("schedules", _always), "the schedule listing is a read and folds"

    def test_the_approval_gate_is_always_mounted_once(self, tmp_path: Path) -> None:
        agent = _build(ProjectConfig(), tmp_path)

        gates: list[ConsoleApprovals] = []
        agent._root_capability.apply(lambda cap: gates.append(cap) if isinstance(cap, ConsoleApprovals) else None)

        assert len(gates) == 1

    async def test_a_master_run_that_calls_a_gated_act_ends_deferred_not_raised(self, tmp_path: Path) -> None:
        """A console act asks the person first: the turn ends with the call outstanding, it does not raise."""

        def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
            del messages, info
            return ModelResponse(parts=[ToolCallPart(tool_name="set_model", args={"model_id": "gpt-5"}, tool_call_id="call-1")])

        agent = _build(ProjectConfig(), tmp_path, model=_captured_params(function), follow_me=_follow_me())
        runtime = _placeholder_runtime(kind="master")
        with answerable():
            result = await agent.run("switch models", deps=runtime)
        assert isinstance(result.output, DeferredToolRequests)
        assert result.output.approvals[0].tool_name == "set_model"
