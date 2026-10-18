"""The guarded knowledge base: every tool result both paths see went through one guard and one recorder.

A fetch targeting a withheld page is refused before it happens; a listing that mentions one
comes back scrubbed; everything is recorded into the shared trace the scoreboard reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RunUsage

from proof.instrument import REFUSAL, ToolTrace
from proof.knowledge_base import GuardedKnowledgeBase

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcp import types as mcp_types
    from pydantic_ai.tools import ToolDefinition
    from pydantic_ai.toolsets import FilteredToolset

    from harness.domain.ports import KnowledgeBaseSurface

WITHHELD = frozenset({"q.memory-eval-commensurability"})


class FakeKnowledgeBase:
    """Structurally a KnowledgeBaseSurface. Results are scriptable; calls are recorded."""

    def __init__(self) -> None:
        self.result: object = "paper.a | 0.9\nq.memory-eval-commensurability | 0.8"
        self.direct_calls: list[tuple[str, dict[str, object]]] = []

    async def list_tools(self) -> list[mcp_types.Tool]:
        return []

    def filtered(self, filter_func: Callable[[RunContext[Any], ToolDefinition], bool | Awaitable[bool]]) -> FilteredToolset[Any]:
        raise NotImplementedError  # exercised in the integration-shaped test below

    async def direct_call_tool(self, name: str, args: dict[str, object], *, metadata: object = None, use_task: bool = False) -> object:
        self.direct_calls.append((name, args))
        return self.result


async def test_direct_call_is_guarded_recorded_and_scrubbed() -> None:
    inner, trace = FakeKnowledgeBase(), ToolTrace()
    knowledge_base = GuardedKnowledgeBase(inner=inner, trace=trace, withheld=WITHHELD)
    result = await knowledge_base.direct_call_tool("search", {"query": "memory evals"})
    assert "q.memory-eval" not in str(result)
    assert "paper.a" in str(result)
    assert trace.calls[0].tool == "search"
    assert "q.memory-eval" not in trace.calls[0].result


async def test_a_call_targeting_a_withheld_page_never_reaches_the_server() -> None:
    inner, trace = FakeKnowledgeBase(), ToolTrace()
    knowledge_base = GuardedKnowledgeBase(inner=inner, trace=trace, withheld=WITHHELD)
    result = await knowledge_base.direct_call_tool("fetch", {"id": "q.memory-eval-commensurability"})
    assert result == REFUSAL
    assert inner.direct_calls == []
    assert trace.calls[0].refused


async def test_a_withheld_row_is_dropped_but_its_neighbors_survive() -> None:
    """The live symptom, pinned: compact JSON is one line, so a line scrub deleted whole payloads.

    The knowledge base's own id listing necessarily names the question pages; with the line scrub the
    preflight read it as empty and concluded the knowledge base had lost every page. Structure-aware
    scrubbing drops the withheld row and keeps its neighbors.
    """
    inner, trace = FakeKnowledgeBase(), ToolTrace()
    inner.result = {"rows": [{"id": "paper.a"}, {"id": "q.memory-eval-commensurability"}, {"id": "ent.b"}], "note": None}
    knowledge_base = GuardedKnowledgeBase(inner=inner, trace=trace, withheld=WITHHELD)
    result = await knowledge_base.direct_call_tool("run_sql", {"sql": "SELECT id FROM api.cards"})
    assert result == {"rows": [{"id": "paper.a"}, {"id": "ent.b"}], "note": None}
    assert "q.memory-eval" not in trace.calls[0].result
    assert "paper.a" in trace.calls[0].result


async def test_a_withheld_mention_in_a_scalar_string_becomes_the_refusal() -> None:
    inner, trace = FakeKnowledgeBase(), ToolTrace()
    inner.result = {"content": "the synthesis in q.memory-eval-commensurability says...", "id": "paper.a"}
    knowledge_base = GuardedKnowledgeBase(inner=inner, trace=trace, withheld=WITHHELD)
    result = await knowledge_base.direct_call_tool("fetch", {"id": "paper.a"})
    assert result == {"content": REFUSAL, "id": "paper.a"}


def test_the_guarded_knowledge_base_satisfies_the_knowledge_base_surface_seam() -> None:
    """`build_environment(engram=...)` takes a KnowledgeBaseSurface -- a GuardedKnowledgeBase must drop in unchanged."""
    surface: KnowledgeBaseSurface = GuardedKnowledgeBase(inner=FakeKnowledgeBase(), trace=ToolTrace(), withheld=WITHHELD)
    assert surface is not None  # assignment IS the assertion; pyright checks it


class FakeFilterableKnowledgeBase:
    """Structurally a KnowledgeBaseSurface whose `filtered()` is real, backed by a `FunctionToolset`.

    `FakeKnowledgeBase` above stands in for the sandbox's direct-call plugin; this one stands in for the
    plugin a worker agent's tool grant becomes, so `GuardedKnowledgeBase.filtered(...)` has a real
    `AbstractToolset` to wrap rather than the `NotImplementedError` above.
    """

    def __init__(self, toolset: FunctionToolset[None]) -> None:
        self.toolset = toolset

    async def list_tools(self) -> list[mcp_types.Tool]:
        return []

    def filtered(self, filter_func: Callable[[RunContext[Any], ToolDefinition], bool | Awaitable[bool]]) -> FilteredToolset[Any]:
        return self.toolset.filtered(filter_func)

    async def direct_call_tool(self, name: str, args: dict[str, object], *, metadata: object = None, use_task: bool = False) -> object:
        raise NotImplementedError


def _admit_everything(ctx: RunContext[Any], tool_def: ToolDefinition) -> bool:
    del ctx, tool_def
    return True


async def test_filtered_returns_a_toolset_whose_call_tool_guards_records_and_scrubs() -> None:
    async def search(query: str) -> str:
        return "paper.a | 0.9\nq.memory-eval-commensurability | 0.8"

    inner, trace = FakeFilterableKnowledgeBase(FunctionToolset([search])), ToolTrace()
    knowledge_base = GuardedKnowledgeBase(inner=inner, trace=trace, withheld=WITHHELD)
    guarded = knowledge_base.filtered(_admit_everything)

    ctx: RunContext[None] = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    tools = await guarded.get_tools(ctx)
    result = await guarded.call_tool("search", {"query": "memory evals"}, ctx, tools["search"])

    assert "q.memory-eval" not in str(result)
    assert "paper.a" in str(result)
    assert trace.calls[-1].tool == "search"
    assert "q.memory-eval" not in trace.calls[-1].result


async def test_the_guard_holds_through_a_real_agent_run() -> None:
    """The whole plugin, end to end: `Agent.run` -> `for_run` reconstruction -> `call_tool` -> scrub.

    The `call_tool` tests above hand-build a `RunContext`; this one lets pydantic-ai drive,
    because `WrapperToolset.for_run` rebuilds the toolset via `dataclasses.replace(...)` mid-run
    and a guard whose `trace`/`withheld` did not survive that rebuild would wave everything
    through -- the exact failure the dataclass shape at the top of `proof/knowledge_base.py` exists to
    prevent, asserted where it would actually bite.
    """

    async def search(query: str) -> str:
        del query
        return "paper.a | 0.9\nq.memory-eval-commensurability | 0.8"

    inner, trace = FakeFilterableKnowledgeBase(FunctionToolset([search])), ToolTrace()
    knowledge_base = GuardedKnowledgeBase(inner=inner, trace=trace, withheld=WITHHELD)
    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("search", {"query": "memory evals"})])
        seen.extend(str(part.content) for part in returns)
        return ModelResponse(parts=[TextPart("done")])

    agent: Agent[None, str] = Agent(FunctionModel(respond), output_type=str, toolsets=[knowledge_base.filtered(_admit_everything)])
    result = await agent.run("what do the memory evals disagree on?")

    assert result.output == "done"
    assert seen, "the model got a tool return back"
    assert all("q.memory-eval" not in content for content in seen), "the model never saw the withheld page"
    assert any("paper.a" in content for content in seen), "and the survivors arrived"
    assert trace.calls[-1].tool == "search"
    assert "q.memory-eval" not in trace.calls[-1].result


async def test_filtered_toolset_refuses_a_call_targeting_a_withheld_page() -> None:
    async def fetch(page_id: str) -> str:
        msg = "the wrapped tool must not run when the guard refuses"
        raise AssertionError(msg)

    inner, trace = FakeFilterableKnowledgeBase(FunctionToolset([fetch])), ToolTrace()
    knowledge_base = GuardedKnowledgeBase(inner=inner, trace=trace, withheld=WITHHELD)
    guarded = knowledge_base.filtered(_admit_everything)

    ctx: RunContext[None] = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    tools = await guarded.get_tools(ctx)
    result = await guarded.call_tool("fetch", {"page_id": "q.memory-eval-commensurability"}, ctx, tools["fetch"])

    assert result == REFUSAL
    assert trace.calls[-1].refused
