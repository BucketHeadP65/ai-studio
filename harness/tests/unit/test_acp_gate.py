"""The gate's editor plugin, tested against a fake ACP client.

What matters here is the translation: a gate question becomes one `session/request_permission`
with exactly two options (no "always" -- a gate reviews THIS run's value), the person's click
becomes the verdict, and a cancelled turn becomes a pause rather than a verdict.
"""

from __future__ import annotations

import asyncio
import json
import warnings

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest

from acp import Client, schema, text_block
from core.ai.workflows.contracts import StepRegistry
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai_harness.experimental import HarnessExperimentalWarning

from harness.features.followme.acts import ConsoleApprovals
from harness.features.running.infrastructure.acp_gate import AcpGateAsker
from harness.session import NoSessionLog, Runtime, answerable
from tests.support.state import a_throwaway_state

warnings.filterwarnings("ignore", category=HarnessExperimentalWarning)

# The ACP module warns on import, so it comes after the filter that silences that warning.
from pydantic_ai_harness.experimental.acp import PydanticAIACPAgent  # noqa: E402 -- see the filter above

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo


@dataclass(frozen=True)
class Asked:
    """One permission request as the fake client received it."""

    session_id: str
    tool_call: schema.ToolCallUpdate
    options: list[schema.PermissionOption]


@dataclass
class FakeAcpClient:
    """Answers `request_permission` from a script and records what was asked."""

    outcomes: list[str]
    requests: list[Asked] = field(default_factory=list)

    async def request_permission(
        self, session_id: str, tool_call: schema.ToolCallUpdate, options: list[schema.PermissionOption], **kwargs: object
    ) -> schema.RequestPermissionResponse:
        del kwargs
        self.requests.append(Asked(session_id=session_id, tool_call=tool_call, options=options))
        outcome = self.outcomes.pop(0)
        if outcome == "cancelled":
            return schema.RequestPermissionResponse.model_validate({"outcome": {"outcome": "cancelled"}})
        return schema.RequestPermissionResponse.model_validate({"outcome": {"outcome": "selected", "optionId": outcome}})


def _asker(outcomes: list[str]) -> tuple[AcpGateAsker, FakeAcpClient]:
    client = FakeAcpClient(outcomes=outcomes)
    return AcpGateAsker(client=client, session_id="sess-1"), client  # type: ignore[arg-type]


async def test_the_click_is_the_verdict() -> None:
    asker, client = _asker(["allow_once", "reject_once"])
    assert await asker.ask("approve", "the draft report") is True
    assert await asker.ask("approve", "the draft report") is False

    first = client.requests[0]
    assert first.session_id == "sess-1"
    assert first.tool_call.tool_call_id.startswith("gate:approve:")
    assert "approve to release" in str(first.tool_call.title)
    assert [option.option_id for option in first.options] == ["allow_once", "reject_once"], "no 'always' for a gate"


async def test_a_cancelled_turn_is_a_pause_not_a_verdict() -> None:
    asker, _client = _asker(["cancelled"])
    with pytest.raises(asyncio.CancelledError, match="cancelled the turn"):
        await asker.ask("approve", "the draft report")


async def test_a_huge_review_is_truncated_with_a_note() -> None:
    asker, client = _asker(["allow_once"])
    await asker.ask("approve", "x" * 10_000)
    content = client.requests[0].tool_call.content
    assert content is not None
    block = content[0]
    assert isinstance(block, schema.ContentToolCallContent)
    text = block.content
    assert isinstance(text, schema.TextContentBlock)
    assert len(text.text) < 5_000
    assert "truncated" in text.text


class _StubConnection(Client):
    """A whole ACP client that answers one permission request and records what it was asked.

    Every other client capability raises, so an adapter reaching for one this test does not
    expect fails loudly rather than passing quietly.
    """

    def __init__(self) -> None:
        self.asked: list[schema.ToolCallUpdate] = []

    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        del session_id, update, kwargs

    def on_connect(self, conn: object) -> None:
        del conn

    async def request_permission(
        self, session_id: str, tool_call: schema.ToolCallUpdate, options: list[schema.PermissionOption], **kwargs: object
    ) -> schema.RequestPermissionResponse:
        del session_id, options, kwargs
        self.asked.append(tool_call)
        return schema.RequestPermissionResponse.model_validate({"outcome": {"outcome": "selected", "optionId": "allow_once"}})

    async def complete_elicitation(self, elicitation_id: str, **kwargs: object) -> None:
        raise NotImplementedError

    async def create_elicitation(self, message: str, mode: object, **kwargs: object) -> schema.CreateElicitationResponse:
        raise NotImplementedError

    async def create_terminal(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[schema.EnvVariable] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: object,
    ) -> schema.CreateTerminalResponse:
        raise NotImplementedError

    async def ext_method(self, method: str, params: dict[str, object]) -> dict[str, object]:
        raise NotImplementedError

    async def ext_notification(self, method: str, params: dict[str, object]) -> None:
        raise NotImplementedError

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: object) -> schema.KillTerminalResponse | None:
        raise NotImplementedError

    async def read_text_file(
        self, session_id: str, path: str, line: int | None = None, limit: int | None = None, **kwargs: object
    ) -> schema.ReadTextFileResponse:
        raise NotImplementedError

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: object) -> schema.ReleaseTerminalResponse | None:
        raise NotImplementedError

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: object) -> schema.TerminalOutputResponse:
        raise NotImplementedError

    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **kwargs: object) -> schema.WaitForTerminalExitResponse:
        raise NotImplementedError

    async def write_text_file(self, session_id: str, path: str, content: str, **kwargs: object) -> schema.WriteTextFileResponse | None:
        raise NotImplementedError


def _scheduling_agent(fired: list[str]) -> Agent[Runtime, str]:
    """The studio agent as ACP composes it, holding the one act that always waits for the person."""

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        del info
        last = messages[-1]
        if isinstance(last, ModelRequest) and any(isinstance(part, ToolReturnPart) for part in last.parts):
            yield "scheduled it"
            return
        yield {0: DeltaToolCall(name="schedule_workflow", json_args=json.dumps({"workflow_id": "wf.nightly"}))}

    async def schedule_workflow(workflow_id: str) -> str:
        """Stand up a schedule for a workflow. Needs the person's approval."""
        fired.append(workflow_id)
        return f"scheduled {workflow_id}"

    toolset: FunctionToolset[Runtime] = FunctionToolset()
    toolset.add_function(schedule_workflow, name="schedule_workflow", takes_ctx=False)
    return Agent(
        FunctionModel(stream_function=stream), deps_type=Runtime, output_type=str, toolsets=[toolset], capabilities=[ConsoleApprovals()]
    )


async def test_an_acp_turn_asks_the_editor_for_a_gated_verb_and_finishes() -> None:
    """The editor is the person a deferred end asks, so the verb is offered and the turn ends on the answer.

    Driven against `PydanticAIACPAgent`, the layer under `run_acp_stdio` that composes the agent
    and resolves approvals; `run_acp_stdio` itself only adds a stdio pair around it.
    """
    fired: list[str] = []
    session = Runtime(registry=StepRegistry(), library=cast("Any", None), agent=cast("Any", None), session_log=NoSessionLog(), state=a_throwaway_state())
    adapter = PydanticAIACPAgent(_scheduling_agent(fired), deps=session)
    connection = _StubConnection()
    adapter.on_connect(connection)
    await adapter.initialize(protocol_version=1)
    opened = await adapter.new_session(cwd=".")

    with answerable():
        response = await adapter.prompt(prompt=[text_block("schedule the nightly one")], session_id=opened.session_id)

    assert response.stop_reason == "end_turn", "the turn ends on the editor's answer rather than raising"
    assert [asked.title for asked in connection.asked] == ["schedule_workflow"]
    assert fired == ["wf.nightly"], "the approved act runs once the editor says yes"
