"""Stand-ins for the write tools a step's case run reaches: each checks one call, records it and lands nothing.

A stand-in takes the real tool's name, and the model sees the real tool's description and argument schema. `write_note`'s
stand-in runs `check_note` with the same arguments. Every other write's stand-in checks the arguments against the real
tool's own schema. A call that fails its check is refused and recorded nowhere.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from pydantic import JsonValue, TypeAdapter
from pydantic_ai import ModelRetry, Tool
from pydantic_ai.toolsets import FunctionToolset
from pydantic_core import to_jsonable_python

from harness.adapters.environment import as_text
from harness.domain.contracts import CASE_RUN_TOOLS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from pydantic_ai import RunContext
    from pydantic_ai.tools import ToolDefinition

    from harness.domain.contracts import KnowledgeBaseSurface, ToolCaller

CHECK_TOOL = "check_note"
"""The engram tool `write_note`'s stand-in runs in its place, which runs every check `write_note` runs and writes nothing."""

WRITE_TOOLS: frozenset[str] = frozenset(name for name, treatment in CASE_RUN_TOOLS.items() if treatment != "read")
"""Every write tool a case run serves with a stand-in."""

_UNSHAPED: dict[str, JsonValue] = {"type": "object", "additionalProperties": True}
"""The schema a stand-in is built with, replaced by the real tool's before any model sees it."""

_ARGUMENTS = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True)
class StandInCall:
    """One write call a stand-in answered in place of the real tool."""

    tool: str
    """The write tool's name."""
    arguments: dict[str, JsonValue]
    """The arguments the step passed."""
    answer: str
    """What the stand-in answered the step."""


StandInCalls = list[StandInCall]
"""Every write call one environment's stand-ins answered, in order. The stand-ins append to it and a case run reads it."""


@dataclass(frozen=True)
class _Definition:
    """The real tool's description and argument schema."""

    description: str | None
    schema: dict[str, JsonValue]


class _StandIns:
    """The stand-ins over one surface, recording into one list, with the real definitions read once."""

    def __init__(self, engram: KnowledgeBaseSurface, calls: StandInCalls, grounding: FunctionToolset[object] | None) -> None:
        self._engram = engram
        self._calls = calls
        self._grounding = grounding
        self._definitions: Mapping[str, _Definition] | None = None

    async def definition(self, name: str) -> _Definition | None:
        """The real tool `name`'s definition, or `None` when neither the surface nor the grounding tools offer it."""
        if self._definitions is None:
            found = {tool.name: _Definition(tool.description, dict(tool.input_schema)) for tool in await self._engram.list_tools()}
            if self._grounding is not None:
                for tool_name, tool in self._grounding.tools.items():
                    found.setdefault(tool_name, _Definition(tool.description, dict(tool.tool_def.parameters_json_schema)))
            self._definitions = found
        return self._definitions.get(name)

    async def prepare(self, _ctx: RunContext[object], tool_def: ToolDefinition) -> ToolDefinition | None:
        """The stand-in's definition with the real tool's description and schema, or `None` to hide one nothing offers."""
        real = await self.definition(tool_def.name)
        if real is None:
            return None
        return replace(tool_def, description=real.description, parameters_json_schema=real.schema)

    async def answer(self, name: str, arguments: Mapping[str, object]) -> str:
        """Check one write call, record it and answer that nothing landed."""
        real = await self.definition(name)
        if real is None:
            msg = f"{name} is not offered here, so it cannot be called. Do the work without it."
            raise ModelRetry(msg)
        values = _ARGUMENTS.validate_python(to_jsonable_python(dict(arguments)))
        if (problem := best_match(Draft202012Validator(real.schema).iter_errors(values))) is not None:
            msg = f"{name} was not recorded: its arguments do not fit the tool's schema: {problem.message}. Fix the arguments and call it again."
            raise ModelRetry(msg)
        if CASE_RUN_TOOLS[name] == "checked_write":
            answer = f"checked, not written: {as_text(await self._engram.direct_call_tool(CHECK_TOOL, dict(values)))}"
        else:
            answer = "recorded, not done"
        self._calls.append(StandInCall(tool=name, arguments=values, answer=answer))
        return answer

    def function(self, name: str) -> Callable[..., Awaitable[str]]:
        """The stand-in for `name` as a function of its arguments by keyword."""

        async def stand_in(**arguments: JsonValue) -> str:
            return await self.answer(name, arguments)

        return stand_in


def stand_in_toolset(engram: KnowledgeBaseSurface, calls: StandInCalls, *, grounding: FunctionToolset[object] | None = None) -> FunctionToolset[object]:
    """Every write tool's stand-in as one toolset, each under the real tool's name.

    Each stand-in shows the model the real tool's description and argument schema, read from `engram` or `grounding`,
    and is hidden when neither offers the real tool. A call that fits the schema is appended to `calls`. Narrow the
    toolset to one step's grant with `.filtered(...)`.

    Args:
        engram: The surface the real write tools are offered on. `write_note`'s stand-in calls its `check_note`.
        calls: Where each answered call is appended.
        grounding: The grounding tools, which offer `skip_digest` and `remember_belief`. `None` hides those two.

    Returns:
        The stand-ins, one tool per write a case run serves.
    """
    stand_ins = _StandIns(engram, calls, grounding)
    toolset = FunctionToolset[object]()
    for name in sorted(WRITE_TOOLS):
        tool = Tool[object].from_schema(stand_ins.function(name), name=name, description=None, json_schema=_UNSHAPED)
        tool.prepare = stand_ins.prepare
        toolset.add_tool(tool)
    return toolset


def stand_in_caller(engram: KnowledgeBaseSurface, calls: StandInCalls) -> ToolCaller:
    """How a program calls its tools in a case run: each write through its stand-in, everything else through `engram`.

    Args:
        engram: The surface every read runs on, and whose tool schemas the stand-ins check against.
        calls: Where each answered write call is appended.

    Returns:
        The caller. It raises `ModelRetry` for a write whose arguments do not fit the real tool's schema.
    """
    stand_ins = _StandIns(engram, calls, None)

    async def call(name: str, args: dict[str, object], /) -> object:
        if name in WRITE_TOOLS:
            return await stand_ins.answer(name, args)
        return await engram.direct_call_tool(name, args)

    return call
