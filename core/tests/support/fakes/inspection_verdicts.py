"""A model response held until another attribution attempt has settled the lead."""

import asyncio

from dataclasses import dataclass, field
from typing import Literal

from pydantic import JsonValue
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo


@dataclass
class HeldUndecided:
    """An uncertain model answer released by the test after a concurrent attempt finishes."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        """Wait for release, then answer that evidence is missing."""
        del messages
        self.entered.set()
        await self.release.wait()
        tool = next(item for item in info.output_tools if item.name.endswith("Undecided"))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args={"reason": "missing evidence"})])


@dataclass
class HeldVerdict:
    """A scripted final verdict released after a competing attempt commits."""

    output: Literal["Attribution", "NoIssue"]
    args: dict[str, JsonValue]
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        """Wait for release, then return the configured output."""
        del messages
        self.entered.set()
        await self.release.wait()
        tool = next(item for item in info.output_tools if item.name.endswith(self.output))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args=self.args)])
