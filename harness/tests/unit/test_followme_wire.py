"""The chat path, end to end. A verb ends the run deferred, the continuation resumes it, and approvals gate the acts.

Every case drives the chat service over a pool of its own and encodes the turn on the wire the
route uses: drawing the response is what runs the turn, and the chunks it wrote are what the
browser would have read.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING, Any, cast

import pytest

from core.ai.workflows.contracts import StepRegistry
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.tools import DeferredToolRequests

from harness import plugins
from harness.domain.events import TurnSettled, TurnStarted
from harness.features.authoring.exceptions import PartAnsweredError
from harness.features.followme.acts import ConsoleApprovals, act_toolset
from harness.features.followme.plugin import FollowMe, master_only
from harness.features.followme.verbs import verb_toolset
from harness.plugins import Switched
from harness.session import Runtime, SessionPool
from tests.support.authoring import a_chat, a_turn, one_session
from tests.support.state import a_session_log, a_state, a_transcript
from tests.support.streaming import submit
from tests.unit.test_followme_slice import FakeConsole

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from core.ai.workflows.contracts import WorkflowLibrary
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, DeltaToolCalls

    from harness.features.authoring.service import ChatService

CONTEXT = {"url": "/harness", "page": "harness", "ids": {}, "tab": "", "trail": [], "extras": {}, "targets": []}


def _returned(messages: Sequence[ModelMessage], name: str) -> str | None:
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolReturnPart) and part.tool_name == name:
                return str(part.content)
    return None


def _agent(first_call: tuple[str, dict[str, object]], console: FakeConsole, seen: list[Sequence[ModelMessage]], tmp_path: Path) -> Agent[Runtime, str | DeferredToolRequests]:
    """Calls `first_call` once, then answers in prose naming what the tool returned."""
    name, args = first_call

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        seen.append(messages)
        returned = _returned(messages, name)
        if returned is None:
            return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id="call-1")])
        return ModelResponse(parts=[TextPart(f"done: {returned}")])

    async def stream_function(messages: Sequence[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        del info
        seen.append(messages)
        returned = _returned(messages, name)
        if returned is None:
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id="call-1")}
            return
        yield f"done: {returned}"

    return Agent(
        FunctionModel(function=function, stream_function=stream_function),
        deps_type=Runtime,
        output_type=[str, DeferredToolRequests],
        capabilities=[
            Switched(FollowMe(verbs=verb_toolset(), acts=act_toolset(console, console, console)), plugin="follow-me", state=a_state(tmp_path), only=master_only),
            ConsoleApprovals(),
        ],
    )


def _session(agent: Agent[Runtime, str | DeferredToolRequests], home: Path, *, kind: str) -> Runtime:
    return Runtime(
        registry=StepRegistry(),
        library=cast("WorkflowLibrary", None),
        agent=agent,
        session_kind=kind,
        session_log=a_session_log(home),
        store=a_transcript(home),
        state=a_state(home),
    )


def _talking_to(
    agent: Agent[Runtime, str | DeferredToolRequests], *, state: Path, kind: str = "master"
) -> tuple[ChatService, str]:
    """The chat service over one session of this test's own, and the id every turn here names."""
    pool = SessionPool(_session(agent, state, kind=kind))
    return a_chat(pool), one_session(pool)


async def test_a_verb_ends_the_run_deferred_and_the_continuation_resumes_it(tmp_path: Path) -> None:
    seen: list[Sequence[ModelMessage]] = []
    chat, session_id = _talking_to(_agent(("navigate", {"url": "/chat"}), FakeConsole(a_state(tmp_path)), seen, tmp_path), state=tmp_path)

    first = await a_turn(chat, session_id, submit("take me to chat", context=CONTEXT))
    assert any(c.get("type") == "tool-input-available" and c.get("toolName") == "navigate" for c in first)
    assert not any(c.get("type") == "data-settle" for c in first), "a deferred end never settles"
    settled = [r.event for r in a_session_log(tmp_path).events() if isinstance(r.event, TurnSettled)]
    assert settled[-1].pending == ("navigate",)

    second = await a_turn(
        chat,
        session_id,
        submit(
            "",
            context={**CONTEXT, "url": "/chat", "page": "chat"},
            tool_results=[{"tool_call_id": "call-1", "tool_name": "navigate", "output": "navigated to /chat"}],
        ),
    )

    settles = [c for c in second if c.get("type") == "data-settle"]
    assert len(settles) == 1
    assert settles[0]["data"]["message"] == "done: navigated to /chat"
    starts = [r.event for r in a_session_log(tmp_path).events() if isinstance(r.event, TurnStarted)]
    assert starts[-1].chars == 0, "a continuation is its own turn with nothing typed"
    last_request = seen[-1][-1]
    prompt = " ".join(str(getattr(part, "content", "")) for part in last_request.parts)
    assert "The person is on chat at /chat" in prompt, "the continuation carries a fresh page context"


async def test_an_act_streams_an_approval_request_and_approval_runs_it(tmp_path: Path) -> None:
    console = FakeConsole(a_state(tmp_path))
    seen: list[Sequence[ModelMessage]] = []
    chat, session_id = _talking_to(_agent(("toggle_plugin", {"name": "sql", "on": True}), console, seen, tmp_path), state=tmp_path)

    first = await a_turn(chat, session_id, submit("open sql", context=CONTEXT))
    approvals = [c for c in first if c.get("type") == "tool-approval-request"]
    assert approvals, "an act in a master session asks the person first"
    assert approvals[0]["toolCallId"] == "call-1"
    assert console.calls == [], "nothing runs before the person answers"

    second = await a_turn(
        chat, session_id, submit("", tool_results=[{"tool_call_id": "call-1", "tool_name": "toggle_plugin", "approved": True}])
    )

    assert console.calls == [("toggle_plugin", ("sql", True))]
    assert next(c for c in second if c.get("type") == "data-settle")["data"]["message"] == "done: Plugin sql is on."


async def test_a_denial_reaches_the_model_and_no_act_runs(tmp_path: Path) -> None:
    console = FakeConsole(a_state(tmp_path))
    seen: list[Sequence[ModelMessage]] = []
    chat, session_id = _talking_to(_agent(("toggle_plugin", {"name": "sql", "on": True}), console, seen, tmp_path), state=tmp_path)

    await a_turn(chat, session_id, submit("open sql", context=CONTEXT))
    second = await a_turn(
        chat,
        session_id,
        submit("", tool_results=[{"tool_call_id": "call-1", "tool_name": "toggle_plugin", "approved": False, "reason": "superseded"}]),
    )

    assert console.calls == []
    assert next(c for c in second if c.get("type") == "data-settle")["data"]["message"] == "done: superseded"


def _unknown_tool(chunks: list[dict[str, Any]], name: str) -> bool:
    """Whether the run refused `name` as a tool it does not have.

    A call the run cannot place is what a verb looks like when the session never mounted it, and it is what
    separates "no verb reached the browser" from "the verb reached it under another chunk name".
    """
    refusals = [c for c in chunks if c.get("type") == "tool-input-error" and c.get("toolName") == name]
    return bool(refusals) and "Unknown tool name" in str(refusals[0].get("errorText", ""))


async def test_a_chat_session_never_defers_because_it_holds_no_verbs(tmp_path: Path) -> None:
    seen: list[Sequence[ModelMessage]] = []
    chat, session_id = _talking_to(
        _agent(("navigate", {"url": "/chat"}), FakeConsole(a_state(tmp_path)), seen, tmp_path), state=tmp_path, kind="chat"
    )

    chunks = await a_turn(chat, session_id, submit("take me to chat"))

    assert any(c.get("type") in {"error", "data-settle"} for c in chunks)
    assert not any(c.get("type") == "tool-input-available" and c.get("toolName") == "navigate" for c in chunks)
    assert _unknown_tool(chunks, "navigate"), "the verbs are mounted for the master session only"
    settled = [r.event for r in a_session_log(tmp_path).events() if isinstance(r.event, TurnSettled)]
    assert not any(event.pending for event in settled), "nothing was ever handed to the browser"


async def test_a_closed_door_leaves_the_master_read_only(tmp_path: Path) -> None:
    seen: list[Sequence[ModelMessage]] = []
    plugins.set_plugin(a_state(tmp_path), "follow-me", enabled=False)
    chat, session_id = _talking_to(_agent(("navigate", {"url": "/chat"}), FakeConsole(a_state(tmp_path)), seen, tmp_path), state=tmp_path)

    chunks = await a_turn(chat, session_id, submit("take me to chat", context=CONTEXT))

    assert not any(c.get("type") == "tool-input-available" and c.get("toolName") == "navigate" for c in chunks)
    assert _unknown_tool(chunks, "navigate"), "a closed plugin mounts no verb at all"
    settled = [r.event for r in a_session_log(tmp_path).events() if isinstance(r.event, TurnSettled)]
    assert not any(event.pending for event in settled), "nothing was ever handed to the browser"


async def test_a_message_without_tool_results_is_byte_identical_to_before(tmp_path: Path) -> None:
    seen: list[Sequence[ModelMessage]] = []
    chat, session_id = _talking_to(
        _agent(("navigate", {"url": "/chat"}), FakeConsole(a_state(tmp_path)), seen, tmp_path), state=tmp_path, kind="chat"
    )

    await a_turn(chat, session_id, submit("hello"))

    request = seen[0][0]
    assert str(getattr(request.parts[0], "content", "")) == "hello"


def _two_act_agent(console: FakeConsole, tmp_path: Path) -> Agent[Runtime, str | DeferredToolRequests]:
    """Calls a verb and an act in one response, then answers in prose. Two calls wait together."""

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        if _returned(messages, "navigate") is None:
            return ModelResponse(
                parts=[
                    ToolCallPart("navigate", {"url": "/chat"}, tool_call_id="call-1"),
                    ToolCallPart("toggle_plugin", {"name": "sql", "on": True}, tool_call_id="call-2"),
                ]
            )
        return ModelResponse(parts=[TextPart("done: both")])

    async def stream_function(messages: Sequence[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        del info
        if _returned(messages, "navigate") is None:
            yield {
                0: DeltaToolCall(name="navigate", json_args=json.dumps({"url": "/chat"}), tool_call_id="call-1"),
                1: DeltaToolCall(name="toggle_plugin", json_args=json.dumps({"name": "sql", "on": True}), tool_call_id="call-2"),
            }
            return
        yield "done: both"

    return Agent(
        FunctionModel(function=function, stream_function=stream_function),
        deps_type=Runtime,
        output_type=[str, DeferredToolRequests],
        capabilities=[
            Switched(FollowMe(verbs=verb_toolset(), acts=act_toolset(console, console, console)), plugin="follow-me", state=a_state(tmp_path), only=master_only),
            ConsoleApprovals(),
        ],
    )


async def test_a_continuation_answering_only_some_waiting_calls_is_refused_whole(tmp_path: Path) -> None:
    console = FakeConsole(a_state(tmp_path))
    chat, session_id = _talking_to(_two_act_agent(console, tmp_path), state=tmp_path)

    first = await a_turn(chat, session_id, submit("go to chat and open sql", context=CONTEXT))
    assert any(c.get("type") == "tool-input-available" and c.get("toolName") == "navigate" for c in first)
    assert any(c.get("type") == "tool-approval-request" for c in first)

    with pytest.raises(PartAnsweredError):
        await a_turn(
            chat, session_id, submit("", tool_results=[{"tool_call_id": "call-1", "tool_name": "navigate", "output": "navigated to /chat"}])
        )

    assert console.calls == [], "the act the person never answered must not run"

    # Refusing must not eat the turn guard, or every later request would be refused as busy.
    both = await a_turn(
        chat,
        session_id,
        submit(
            "",
            tool_results=[
                {"tool_call_id": "call-1", "tool_name": "navigate", "output": "navigated to /chat"},
                {"tool_call_id": "call-2", "tool_name": "toggle_plugin", "approved": True},
            ],
        ),
    )

    assert console.calls == [("toggle_plugin", ("sql", True))], "the complete answer runs the act"
    assert next(c for c in both if c.get("type") == "data-settle")["data"]["message"] == "done: both"


async def test_the_person_s_own_text_reaches_the_model_untrimmed(tmp_path: Path) -> None:
    seen: list[Sequence[ModelMessage]] = []
    typed = "  def f():\n      return 1  "
    chat, session_id = _talking_to(
        _agent(("navigate", {"url": "/chat"}), FakeConsole(a_state(tmp_path)), seen, tmp_path), state=tmp_path, kind="chat"
    )

    await a_turn(chat, session_id, submit(typed))

    request = seen[0][0]
    assert str(getattr(request.parts[0], "content", "")) == typed, "a pasted block keeps the whitespace it was pasted with"


async def test_typing_while_a_verb_is_still_running_supersedes_it_instead_of_failing_the_turn(tmp_path: Path) -> None:
    """The composer stays live while a verb runs, so a person can type into that gap.

    The calls left waiting are closed here rather than handed to the run, which refuses a fresh prompt
    while any are outstanding and words that refusal for whoever wrote the caller.
    """
    seen: list[Sequence[ModelMessage]] = []
    chat, session_id = _talking_to(_agent(("navigate", {"url": "/chat"}), FakeConsole(a_state(tmp_path)), seen, tmp_path), state=tmp_path)

    first = await a_turn(chat, session_id, submit("take me to chat", context=CONTEXT))
    assert any(c.get("type") == "tool-input-available" and c.get("toolName") == "navigate" for c in first)

    # Nothing answered. The person just types something else.
    second = await a_turn(chat, session_id, submit("actually, never mind", context=CONTEXT))

    settles = [c for c in second if c.get("type") == "data-settle"]
    assert len(settles) == 1, "the new message is answered rather than dying on an internal error"
    assert not any(c.get("type") == "error" for c in second)
    assert "superseded" in str(_returned(seen[-1], "navigate")), "the waiting verb is told it was superseded"


async def test_a_continuation_with_no_answers_at_all_is_refused_rather_than_erroring(tmp_path: Path) -> None:
    chat, session_id = _talking_to(_two_act_agent(FakeConsole(a_state(tmp_path)), tmp_path), state=tmp_path)

    await a_turn(chat, session_id, submit("go to chat and open sql", context=CONTEXT))

    with pytest.raises(PartAnsweredError):
        await a_turn(chat, session_id, submit("", tool_results=[]))
