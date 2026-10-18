"""The guarded knowledge base: every tool result both paths see went through one guard and one recorder.

`GuardedKnowledgeBase` implements `KnowledgeBaseSurface` -- the same three-member slice `build_environment` takes --
so it drops into the `engram=` seam unchanged. Two doors, one guard: `direct_call_tool` is what a
sandboxed program dispatches through, and `filtered(...)` is what a worker agent's tool grant
becomes, wrapped in `_GuardedToolset` so its own `call_tool` guards and records too. A call
targeting a withheld page never reaches the server; a result that mentions one comes back with
that row dropped (`scrub_payload` -- structure-aware, because compact JSON is one line and a
line-based scrub would delete a whole payload for one withheld row); every call, refused or not,
lands in the one trace the scoreboard reads.

`_GuardedToolset` is a plain `@dataclass` over `WrapperToolset`, exactly like the library's own
`FilteredToolset`/`ApprovalRequiredToolset` -- not a hand-rolled `__init__` -- because
`WrapperToolset.for_run`/`for_run_step` reconstruct the toolset via `dataclasses.replace(self,
wrapped=...)` when the thing underneath changes identity between runs. That only carries `trace`
and `withheld` along if they are real dataclass fields; a subclass with a custom `__init__` would
lose them the first time `replace()` fired.
"""

from __future__ import annotations

import json

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

from pydantic_ai.tools import AgentDepsT
from pydantic_ai.toolsets import AbstractToolset, WrapperToolset
from pydantic_core import to_jsonable_python

from proof.instrument import REFUSAL, ToolCall, ToolTrace, guard_args, scrub_result

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcp import types as mcp_types
    from pydantic_ai.mcp import MCPToolset
    from pydantic_ai.tools import RunContext, ToolDefinition
    from pydantic_ai.toolsets import ToolsetTool

    from harness.domain.contracts import KnowledgeBaseSurface


def as_text(value: object) -> str:
    """One tool result, as the trace records it. A string stays a string; anything else is dumped."""
    return value if isinstance(value, str) else json.dumps(to_jsonable_python(value), default=str)


def _mentions(node: object, withheld: frozenset[str]) -> bool:
    text = node if isinstance(node, str) else json.dumps(node, default=str)
    return any(page in text for page in withheld)


def scrub_payload(value: object, withheld: frozenset[str]) -> tuple[object, bool]:
    """Structure-aware scrub: a withheld ROW disappears; its neighbors survive.

    Compact JSON puts a whole result on one line, so the line-based scrub deletes an entire
    payload the moment one withheld row appears anywhere in it -- observed live when the knowledge base's
    own id listing (which necessarily names the question pages) came back as a single line and
    the preflight concluded the knowledge base was empty. A ranked search would have died the same way:
    one withheld hit, ten results gone. Walking the structure keeps the granularity honest: a
    list entry that mentions a withheld page is dropped, a string that mentions one becomes the
    refusal, and everything beside them survives untouched. Plain strings keep the line scrub.
    """
    if isinstance(value, str):
        return scrub_result(value, withheld)
    removed = False

    def walk(node: object) -> object:
        nonlocal removed
        if isinstance(node, list):
            kept = []
            for item in node:
                if _mentions(item, withheld):
                    removed = True
                else:
                    kept.append(walk(item))
            return kept
        if isinstance(node, dict):
            return {key: walk(item) for key, item in node.items()}
        if isinstance(node, str) and _mentions(node, withheld):
            removed = True
            return REFUSAL
        return node

    return walk(to_jsonable_python(value)), removed


@dataclass
class _GuardedToolset(WrapperToolset[AgentDepsT]):
    """The guard, at the layer worker agents call through."""

    trace: ToolTrace
    withheld: frozenset[str]

    async def call_tool(self, name: str, tool_args: dict[str, Any], ctx: RunContext[AgentDepsT], tool: ToolsetTool[AgentDepsT]) -> Any:
        """Guard, delegate, scrub and record one call a worker agent made through this toolset."""
        args_text = json.dumps(tool_args, default=str)
        if (refusal := guard_args(args_text, self.withheld)) is not None:
            self.trace.calls.append(ToolCall(tool=name, args=args_text, result=refusal, refused=True))
            return refusal
        result = await super().call_tool(name, tool_args, ctx, tool)
        payload, removed = scrub_payload(result, self.withheld)
        self.trace.calls.append(ToolCall(tool=name, args=args_text, result=as_text(payload)))
        return payload if removed else result


@dataclass
class GuardedKnowledgeBase:
    """KnowledgeBaseSurface over the live one: same tools, every call guarded and recorded."""

    inner: KnowledgeBaseSurface | MCPToolset[Any]
    trace: ToolTrace
    withheld: frozenset[str]

    async def list_tools(self) -> list[mcp_types.Tool]:
        """The tools the wrapped surface currently offers -- unfiltered; the guard applies at call time."""
        return await self.inner.list_tools()

    def filtered(self, filter_func: Callable[[RunContext[Any], ToolDefinition], bool | Awaitable[bool]]) -> _GuardedToolset[Any]:
        """A view of the wrapped surface admitting only `filter_func`'s tools, guarded and recorded."""
        return _GuardedToolset(self.inner.filtered(filter_func), trace=self.trace, withheld=self.withheld)

    async def direct_call_tool(self, name: str, args: dict[str, Any], *, metadata: dict[str, Any] | None = None, use_task: bool = False) -> Any:
        """Guard, delegate, scrub and record one call a sandboxed program made directly."""
        args_text = json.dumps(args, default=str)
        if (refusal := guard_args(args_text, self.withheld)) is not None:
            self.trace.calls.append(ToolCall(tool=name, args=args_text, result=refusal, refused=True))
            return refusal
        result = await self.inner.direct_call_tool(name, args, metadata=metadata, use_task=use_task)
        payload, removed = scrub_payload(result, self.withheld)
        self.trace.calls.append(ToolCall(tool=name, args=args_text, result=as_text(payload)))
        return payload if removed else result

    async def __aenter__(self) -> Self:
        if isinstance(self.inner, AbstractToolset):
            await self.inner.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> bool | None:
        if isinstance(self.inner, AbstractToolset):
            return await self.inner.__aexit__(*exc)
        return None
