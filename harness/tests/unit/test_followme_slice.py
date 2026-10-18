"""The follow-me slice: four external verbs, twelve gated acts over three ports, a master-only plugin."""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, Any, cast

from core.ai.workflows.contracts import StepRegistry
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults, RunContext, ToolDefinition
from pydantic_ai.toolsets import CombinedToolset, FunctionToolset
from pydantic_ai.usage import RunUsage

from harness import plugins
from harness.features.followme.acts import ConsoleApprovals, act_toolset, needs_approval, reachable
from harness.features.followme.domain import CONSOLE_ACTS, SCHEDULE_AUTHORING_ACTS, TRUST_ACTS, VERBS
from harness.features.followme.plugin import FollowMe, master_only
from harness.features.followme.verbs import verb_toolset
from harness.plugins import Switched
from harness.session import ANSWERABLE, NoSessionLog, Runtime, answerable, not_answerable
from harness.settings import StateSettings
from tests.support.state import a_state, a_throwaway_state

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from pydantic_ai.toolsets import AbstractToolset

    from harness.adapters.model_tiers import Tier


class FakeConsole:
    """All three ports at once, each act answering a sentence and remembering the call.

    An act with a side effect the rest of the system can see performs it here too. The real console
    toggles the plugin it is asked about, so this one does as well: a fake that only remembers being
    asked lets a test pass while the behaviour it is checking never happens.
    """

    def __init__(self, state: StateSettings | None = None) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.state = state if state is not None else StateSettings()

    async def set_model(self, model_id: str, tier: Tier = "orchestrator") -> str:
        self.calls.append(("set_model", (model_id, tier)))
        return f"{tier} set to {model_id}."

    async def select_profile(self, name: str | None) -> str:
        self.calls.append(("select_profile", (name,)))
        return "Profile cleared." if name is None else f"Profile {name} selected."

    async def toggle_plugin(self, name: str, *, on: bool) -> str:
        self.calls.append(("toggle_plugin", (name, on)))
        plugins.set_plugin(self.state, name, enabled=on)
        return f"Plugin {name} is {'on' if on else 'off'}."

    async def pause(self, session_id: str) -> str:
        self.calls.append(("pause", (session_id,)))
        return "Paused."

    async def resume(self, session_id: str) -> str:
        self.calls.append(("resume", (session_id,)))
        return "Resumed."

    async def stop(self, session_id: str) -> str:
        self.calls.append(("stop", (session_id,)))
        return "Stopped."

    async def set_automode(self, session_id: str, *, on: bool) -> str:
        self.calls.append(("automode", (session_id, on)))
        return "Automode on." if on else "Automode off."

    async def decide(self, session_id: str, *, approve: bool, feedback: str) -> str:
        self.calls.append(("decide", (session_id, approve, feedback)))
        return "Approved." if approve else "Rejected."

    async def archive(self, workflow_id: str) -> str:
        self.calls.append(("archive", (workflow_id,)))
        return f"Archived {workflow_id}."

    async def restore(self, workflow_id: str) -> str:
        self.calls.append(("restore", (workflow_id,)))
        return f"Restored {workflow_id}."

    async def redescribe(self, workflow_id: str, *, title: str, summary: str, applicability: str) -> str:
        self.calls.append(("redescribe", (workflow_id, title, summary, applicability)))
        return f"Redescribed {workflow_id}."


def _ctx(kind: str = "master") -> RunContext[Runtime]:
    runtime = Runtime(registry=StepRegistry(), library=cast("Any", None), agent=cast("Any", None), session_kind=kind, session_log=NoSessionLog(), state=a_throwaway_state())
    return RunContext(deps=runtime, model=TestModel(), usage=RunUsage())


async def _names(toolset: AbstractToolset[Runtime]) -> list[str]:
    return sorted(await toolset.get_tools(_ctx()))


async def test_the_verb_toolset_declares_exactly_the_four_verbs_as_external() -> None:
    tools = await verb_toolset().get_tools(_ctx())
    assert sorted(tools) == sorted(VERBS) == ["navigate", "point", "prefill", "set_tab"]
    assert all(tool.tool_def.kind == "external" for tool in tools.values())
    assert set(tools["set_tab"].tool_def.parameters_json_schema["required"]) == {"target", "tab"}
    assert set(tools["prefill"].tool_def.parameters_json_schema["required"]) == {"target", "text"}


async def test_the_act_toolset_mounts_the_new_acts_and_none_of_the_schedule_ones(tmp_path: Path) -> None:
    console = FakeConsole(a_state(tmp_path))
    names = await _names(act_toolset(console, console, console))
    assert names == sorted(
        [
            "archive_workflow",
            "decide_gate",
            "redescribe_workflow",
            "restore_workflow",
            "run_control",
            "select_profile",
            "set_model",
            "toggle_plugin",
        ]
    )
    assert set(names) < set(CONSOLE_ACTS)


async def test_every_act_calls_its_port_and_answers_the_ports_sentence(tmp_path: Path) -> None:
    console = FakeConsole(a_state(tmp_path))
    toolset = act_toolset(console, console, console)
    ctx = _ctx()
    tools = await toolset.get_tools(ctx)
    said = await toolset.call_tool("run_control", {"session_id": "s-1", "act": "pause"}, ctx, tools["run_control"])
    assert said == "Paused."
    said = await toolset.call_tool("decide_gate", {"session_id": "s-1", "approve": False, "feedback": "not yet"}, ctx, tools["decide_gate"])
    assert said == "Rejected."
    said = await toolset.call_tool("toggle_plugin", {"name": "sql", "on": True}, ctx, tools["toggle_plugin"])
    assert said == "Plugin sql is on."
    assert console.calls == [("pause", ("s-1",)), ("decide", ("s-1", False, "not yet")), ("toggle_plugin", ("sql", True))]


def test_the_console_act_catalog_excludes_every_trust_act() -> None:
    assert TRUST_ACTS == ("activate", "promote", "demote", "approve", "import_path", "save_workflow")
    assert not any(trust in name for name in (*CONSOLE_ACTS, *VERBS) for trust in ("activate", "promote", "demote", "approve", "save"))
    assert {"schedule_workflow", "update_schedule", "delete_schedule", "run_schedule_now"} <= set(CONSOLE_ACTS)


async def _saved() -> str:
    """Stands in for the contract port's save tool on a surface the plugin is asked to wrap."""
    return "saved"


async def test_the_door_filters_every_trust_act_off_the_surface_it_wraps(tmp_path: Path) -> None:
    console = FakeConsole(a_state(tmp_path))
    plugin = FollowMe(verbs=verb_toolset(), acts=act_toolset(console, console, console))
    surface: FunctionToolset[Runtime] = FunctionToolset()
    surface.add_function(_saved, name="save_workflow", takes_ctx=False)
    surface.add_function(_saved, name="search_workflows", takes_ctx=False)

    assert await _names(plugin.get_wrapper_toolset(surface)) == ["search_workflows"]


def _tool_def(name: str) -> ToolDefinition:
    return ToolDefinition(name=name, parameters_json_schema={"type": "object", "properties": {}})


def test_approval_is_required_for_every_console_act_in_a_master_session(tmp_path: Path) -> None:
    assert needs_approval(_ctx("master"), _tool_def("schedule_workflow"), {})
    assert needs_approval(_ctx("master"), _tool_def("toggle_plugin"), {})
    assert not needs_approval(_ctx("master"), _tool_def("search"), {})
    assert needs_approval(_ctx("chat"), _tool_def("schedule_workflow"), {})
    # A closed plugin must not settle the acts that come after it. The plugin decides what a run mounts,
    # never whether a mounted act may skip the person, and the schedule acts are mounted either way.
    plugins.set_plugin(a_state(tmp_path), "follow-me", enabled=False)
    assert needs_approval(_ctx("master"), _tool_def("schedule_workflow"), {})


def test_schedule_workflow_needs_approval_in_an_ordinary_chat_session_with_the_door_closed(tmp_path: Path) -> None:
    plugins.set_plugin(a_state(tmp_path), "follow-me", enabled=False)
    assert needs_approval(_ctx("chat"), _tool_def("schedule_workflow"), {})


def test_update_schedule_and_delete_schedule_need_approval_in_an_ordinary_chat_session_with_the_door_closed(tmp_path: Path) -> None:
    plugins.set_plugin(a_state(tmp_path), "follow-me", enabled=False)
    assert needs_approval(_ctx("chat"), _tool_def("update_schedule"), {})
    assert needs_approval(_ctx("chat"), _tool_def("delete_schedule"), {})


def test_run_schedule_now_does_not_need_approval_in_an_ordinary_chat_session() -> None:
    """It fires a schedule the owner already approved, rather than standing one up."""
    assert not needs_approval(_ctx("chat"), _tool_def("run_schedule_now"), {})


def test_every_other_console_act_keeps_its_chat_and_master_session_condition() -> None:
    for name in CONSOLE_ACTS:
        if name in SCHEDULE_AUTHORING_ACTS:
            continue
        assert not needs_approval(_ctx("chat"), _tool_def(name), {})
        assert needs_approval(_ctx("master"), _tool_def(name), {})


def test_master_only_reads_the_session_kind() -> None:
    assert master_only(_ctx("master"))
    assert not master_only(_ctx("chat"))
    assert not master_only(_ctx("runner"))


async def test_the_door_combines_both_toolsets_and_speaks_once(tmp_path: Path) -> None:
    console = FakeConsole(a_state(tmp_path))
    plugin = FollowMe(verbs=verb_toolset(), acts=act_toolset(console, console, console))
    names = await _names(plugin.get_toolset())
    assert set(VERBS) <= set(names)
    assert "toggle_plugin" in names
    voice = plugin.get_instructions()
    assert isinstance(voice, str)
    assert "approval" in voice
    assert "navigate" in voice


async def test_an_approved_act_that_turns_a_plugin_off_does_not_settle_the_acts_after_it(tmp_path: Path) -> None:
    """One run, two acts, the first of them closing the plugin the whole slice hangs off.

    A run keeps the toolset it was built with, so the second act stays mounted whatever the first one
    did to the plugin. It must still wait for the person, or approving one innocuous act would hand the
    rest of the turn away. Both turns run on a plugin that can answer, which is what offers the acts.
    """
    console = FakeConsole(a_state(tmp_path))
    calls: list[str] = []

    def model(messages: object, info: object) -> ModelResponse:
        del info
        calls.append("step")
        if len(calls) == 1:
            return ModelResponse(parts=[ToolCallPart("toggle_plugin", {"name": "follow-me", "on": False}, tool_call_id="c-1")])
        if len(calls) == 2:
            return ModelResponse(parts=[ToolCallPart("run_control", {"session_id": "s-9", "act": "stop"}, tool_call_id="c-2")])
        return ModelResponse(parts=[TextPart("done")])

    plugins.set_plugin(a_state(tmp_path), "follow-me", enabled=True)
    agent = Agent(
        FunctionModel(cast("Any", model)),
        deps_type=Runtime,
        output_type=[str, DeferredToolRequests],
        capabilities=[
            Switched(FollowMe(verbs=verb_toolset(), acts=act_toolset(console, console, console)), plugin="follow-me", state=a_state(tmp_path), only=master_only),
            ConsoleApprovals(),
        ],
    )
    session = Runtime(registry=StepRegistry(), library=cast("Any", None), agent=cast("Any", None), session_kind="master", session_log=NoSessionLog(), state=a_state(tmp_path))

    with answerable():
        first = await agent.run("close the plugin then stop s-9", deps=session)
        assert isinstance(first.output, DeferredToolRequests)
        assert [part.tool_name for part in first.output.approvals] == ["toggle_plugin"]

        second = await agent.run(
            deps=session, message_history=first.all_messages(), deferred_tool_results=DeferredToolResults(approvals={"c-1": True})
        )
    assert isinstance(second.output, DeferredToolRequests), "the second act must ask, not run"
    assert [part.tool_name for part in second.output.approvals] == ["run_control"]
    assert console.calls == [("toggle_plugin", ("follow-me", False))], "only the act the person actually approved has run"


def _gated_surface(console: FakeConsole) -> AbstractToolset[Runtime]:
    """The chat agent's own surface as the gate sees it, the schedule tools beside the plugin's acts.

    `search_workflows` rides along as a tool nobody ever has to approve, so a test can tell "the
    gated verbs are gone" from "the toolset came back empty."
    """
    schedules: FunctionToolset[Runtime] = FunctionToolset()
    for name in (*SCHEDULE_AUTHORING_ACTS, "run_schedule_now", "search_workflows"):
        schedules.add_function(_saved, name=name, takes_ctx=False)
    return CombinedToolset([schedules, act_toolset(console, console, console)])


async def _offered(toolset: AbstractToolset[Runtime], kind: str) -> set[str]:
    return set(await toolset.get_tools(_ctx(kind)))


async def test_a_turn_nobody_can_answer_is_not_offered_the_schedule_acts(tmp_path: Path) -> None:
    """The rule in one line, a plugin that cannot answer an approval does not get the verbs that ask."""
    wrapped = ConsoleApprovals().get_wrapper_toolset(_gated_surface(FakeConsole(a_state(tmp_path))))

    unanswerable = await _offered(wrapped, "chat")
    with answerable():
        offered = await _offered(wrapped, "chat")

    assert not (set(SCHEDULE_AUTHORING_ACTS) & unanswerable), "nobody can answer, so nothing may ask"
    assert set(SCHEDULE_AUTHORING_ACTS) <= offered
    assert {"run_schedule_now", "search_workflows"} <= unanswerable, "only the verbs that ask are hidden"


async def test_a_master_turn_nobody_can_answer_is_not_offered_the_console_acts_either(tmp_path: Path) -> None:
    """In a master session every console act waits for the person, so every one of them hides the same way."""
    wrapped = ConsoleApprovals().get_wrapper_toolset(_gated_surface(FakeConsole(a_state(tmp_path))))

    unanswerable = await _offered(wrapped, "master")
    with answerable():
        offered = await _offered(wrapped, "master")

    gated = {name for name in CONSOLE_ACTS if needs_approval(_ctx("master"), _tool_def(name), {})}
    assert not (gated & unanswerable)
    assert gated <= offered


def test_the_filter_reads_the_same_predicate_the_gate_does() -> None:
    """One rule, two uses, so a verb can never be hidden and asked for at once, or offered and never asked."""
    for name in (*CONSOLE_ACTS, "search"):
        for kind in ("chat", "master"):
            asked = needs_approval(_ctx(kind), _tool_def(name), {})
            assert reachable(_ctx(kind), _tool_def(name)) is not asked
            with answerable():
                assert reachable(_ctx(kind), _tool_def(name))


async def test_a_block_abandoned_mid_stream_is_closed_from_another_task_without_raising() -> None:
    """A streamed turn's generator can be closed by a task other than the one that opened it.

    That is what a client hanging up mid-stream steps behind, and CPython finalizes the suspended
    generator from a fresh task whose context is a copy of the one the block ran in. Closing it
    must finish rather than raise, and no turn outside the abandoned one may be left marked.
    """

    async def held() -> AsyncGenerator[bool]:
        with answerable():
            yield ANSWERABLE.get()

    stream = held()

    async def open_it() -> bool:
        return await anext(stream)

    async def close_it() -> None:
        await stream.aclose()

    assert await asyncio.create_task(open_it()) is True, "the block marks the turn that draws it"
    await asyncio.create_task(close_it())

    assert ANSWERABLE.get() is False, "no turn outside the abandoned one is marked"


def test_a_turn_nobody_attends_stays_unanswerable_inside_a_door_that_can_answer() -> None:
    """A headless turn fired from inside an answering plugin must not inherit that plugin's marker."""
    with answerable():
        assert ANSWERABLE.get() is True
        with not_answerable():
            assert ANSWERABLE.get() is False
        assert ANSWERABLE.get() is True, "the plugin's own turn is answerable again after"
    assert ANSWERABLE.get() is False
