"""A knowledge base surface whose write tools carry their real argument schemas, recording every tool it ran."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mcp import types as mcp_types
from pydantic import JsonValue  # noqa: TC002 (FunctionToolset reads the tools' annotations at runtime)
from pydantic_ai.toolsets import FunctionToolset

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pydantic_ai import RunContext
    from pydantic_ai.tools import ToolDefinition
    from pydantic_ai.toolsets import AbstractToolset


CHECKED = "ok, no findings"
"""What this surface's `check_note` answers."""


@dataclass
class FakeWritingSurface:
    """`list_tools`, `filtered` and `direct_call_tool` over plain functions named after engram's tools.

    `list_tools` answers each tool's real argument schema, so a stand-in that checks arguments against it refuses what
    the real tool would. `ran` records every tool that ran, by any path, in order. `direct_calls` records each direct
    call with its arguments.
    """

    ran: list[str] = field(default_factory=list[str])
    direct_calls: list[tuple[str, dict[str, object]]] = field(default_factory=list[tuple[str, dict[str, object]]])
    tools: FunctionToolset[object] = field(init=False)
    _functions: dict[str, Callable[..., str]] = field(init=False)

    def __post_init__(self) -> None:
        """Build the tools once, each recording itself in `ran`."""
        ran = self.ran

        def write_note(knowledge_base: str, payload: dict[str, JsonValue], path: str = "") -> str:
            """Write one validated page into a knowledge base and index it."""
            ran.append("write_note")
            return "talk.a-talk"

        def check_note(knowledge_base: str, payload: dict[str, JsonValue], path: str = "") -> str:
            """Run write_note's checks on a page payload without writing."""
            ran.append("check_note")
            return CHECKED

        def request_source(knowledge_base: str, url: str, found_via: str = "", reason: str = "") -> str:
            """Queue a URL for a knowledge base's next sync."""
            ran.append("request_source")
            return "requested"

        def search(query: str) -> str:
            """Ranked search over the knowledge base."""
            ran.append("search")
            return "paper.one :: a hit"

        functions: list[Callable[..., str]] = [write_note, check_note, request_source, search]
        self.tools = FunctionToolset[object](functions)
        self._functions = {function.__name__: function for function in functions}

    async def list_tools(self) -> list[mcp_types.Tool]:
        """Every tool, with its description and its real argument schema."""
        return [
            mcp_types.Tool(name=tool.name, description=tool.description, input_schema=dict(tool.tool_def.parameters_json_schema))
            for tool in self.tools.tools.values()
        ]

    def filtered(self, filter_func: Callable[[RunContext[object], ToolDefinition], bool | Awaitable[bool]]) -> AbstractToolset[object]:
        """The tools `filter_func` admits."""
        return self.tools.filtered(filter_func)

    async def direct_call_tool(
        self, name: str, args: dict[str, object], *, metadata: dict[str, object] | None = None, use_task: bool = False
    ) -> object:
        """Run one tool by name with these arguments, validated against its schema first."""
        self.direct_calls.append((name, dict(args)))
        tool = self.tools.tools[name]
        validated = tool.function_schema.validator.validate_python(dict(args))
        return self._functions[name](**validated)
