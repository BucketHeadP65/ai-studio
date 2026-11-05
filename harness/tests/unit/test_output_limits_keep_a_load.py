"""Upstream `ToolOutputLimits` returns a capability load whole, so the capability's tools unlock under any `tool_filter`.

The chat agent and every step mount the upstream capability with a `tool_filter` that leaves `load_capability` out, so
their own tests pass whether or not the upstream exemption holds. This test is the bug's own repro with nothing left out:
every tool limited, a band shorter than the load's return, and the tool called after the load. A load returns a short
message and its instructions follow apart from it, so the band is shorter than that message, not than the instructions.
"""

from __future__ import annotations

from types import NoneType
from typing import TYPE_CHECKING

from pydantic_ai import Agent
from pydantic_ai.capabilities import Capability
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.tool_output_limits import Band, ToolOutputLimits, Truncate

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo

INSTRUCTIONS = "Follow every step of the procedure before you answer. " * 80
"""The deferred capability's instructions, about 4,400 characters."""

BAND = 20
"""Characters past which every tool return is truncated, fewer than the load's own return message holds."""


def ping() -> str:
    """Answer pong."""
    return "pong"


async def test_a_load_longer_than_every_band_still_reveals_its_tool() -> None:
    offered: list[set[str]] = []
    returns: list[ToolReturnPart] = []

    def model(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered.append({tool.name for tool in info.function_tools})
        returns.extend(part for message in messages for part in message.parts if isinstance(part, ToolReturnPart))
        if len(offered) == 1:
            return ModelResponse(parts=[ToolCallPart("load_capability", {"id": "procedure"})])
        if len(offered) == 2:
            return ModelResponse(parts=[ToolCallPart("ping", {})])
        return ModelResponse(parts=[TextPart("done")])

    agent: Agent[None, str] = Agent(
        FunctionModel(model),
        deps_type=NoneType,
        capabilities=[
            Capability[None](id="procedure", description="Brings ping.", defer_loading=True, instructions=INSTRUCTIONS, tools=[ping]),
            ToolOutputLimits[None](bands=[Band(over=BAND, action=Truncate(max_chars=BAND))], tool_filter="all"),
        ],
    )

    await agent.run("go")

    assert "ping" not in offered[0], "the tool stays hidden until the load"
    assert "ping" in offered[1], "the load is returned whole, so the tool is offered after it"
    assert [part.content for part in returns if part.tool_name == "ping"][-1] == "pong", "and the call to it runs"
