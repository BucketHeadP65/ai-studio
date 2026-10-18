"""The pydantic-ai capability that shows disclosable tool results as a ref and a summary and loads them by ref.

`ResultDisclosure` replaces each disclosable instance in a tool result with a stub of its ref and summary, plus a title
when the title role is bound. The stubs arrive as one `DisclosureEnvelope`, so anything that reads envelopes applies.
The full instances stay held for the run, and the added `load_result(ref)` tool loads one on demand.
It collapses every disclosable result and counts nothing.

A model binds the summary role (required) and the title role (optional) in one of two ways.
It exposes a `disclosure_summary` attribute and optionally `disclosure_title`, or an explicit `Binding` names its own
fields, as in `ResultDisclosure(bindings={Animal: Binding(summary="blurb", title="name")})`.
An explicit binding for a type or any of its bases wins over the attribute convention.
An entry with no bound title has no title, whatever fields the model has.

A result collapses when it is one disclosable instance, or a non-empty `list`, `tuple` or `set` whose every item is
disclosable. Either may be bare or the `return_value` of a `ToolReturn`, whose `content` and `metadata` pass through.
Anything else passes through unchanged, whichever tool produced it.

Refs are opaque and valid only within the run. Records already loaded stay in history as ordinary tool returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Protocol, TypedDict, runtime_checkable
from uuid import uuid4

import pydantic

from pydantic import TypeAdapter
from pydantic_ai import ToolReturn
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry, UserError
from pydantic_ai.tools import AgentDepsT, ToolDefinition
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from core import observability

from .domain import ChildSummary, DisclosureEnvelope

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic_ai import RunContext
    from pydantic_ai.toolsets import AbstractToolset


LOAD_RESULT_TOOL_NAME = "load_result"

LOAD_RESULT_TOOL_DESCRIPTION = (
    "A result shown as a summary with a ref is disclosed. Pass its ref here to load the full record. Load only what you need."
)


@runtime_checkable
class Disclosable(Protocol):
    """A model that exposes `disclosure_summary` binds the summary role with no configuration."""

    @property
    def disclosure_summary(self) -> str:
        """The one-line summary shown in place of the full record."""
        ...


@dataclass(frozen=True)
class Binding:
    """Maps a producer's own field names onto the protocol's roles for one model type.

    `summary` names the attribute playing the summary role and is required.
    `title` names the attribute playing the title role, or is `None` for no title. A title is never guessed.
    """

    summary: str
    title: str | None = None


_STRUCTURAL_SUMMARY = "disclosure_summary"
_STRUCTURAL_TITLE = "disclosure_title"


class _LoadResultArgs(TypedDict):
    """Typed arguments for a `load_result` tool call."""

    ref: Annotated[str, pydantic.Field(description="The ref of the disclosed result to load.")]


_load_result_args_ta = TypeAdapter(_LoadResultArgs)
_LOAD_RESULT_SCHEMA = _load_result_args_ta.json_schema()
_LOAD_RESULT_SCHEMA["title"] = "LoadResultArgs"

_RESULTS_TEXT = (
    "## Disclosed results\n"
    "Some tool results arrive disclosed: each item shows as a ref plus a one-line summary instead "
    f"of its full record. When a summary is not enough, call `{LOAD_RESULT_TOOL_NAME}` with the "
    "item's ref to load the full record into context. Trust the summary when it suffices, because loading "
    "costs tokens. Refs are valid only within this run."
)


@dataclass
class ResultDisclosure(AbstractCapability[AgentDepsT]):
    """Collapses disclosable tool results to ref and summary stubs and loads full records through `load_result`."""

    bindings: Mapping[type, Binding] = field(default_factory=dict)
    """Explicit role bindings per model type. An entry for a type or any base wins over the
    structural `disclosure_summary` convention."""

    def get_instructions(self) -> Callable[[RunContext[AgentDepsT]], str]:
        """Give the agent the disclosed-results protocol text.

        Returns:
            A function of the run context that returns the protocol text.
        """

        def compose(_ctx: RunContext[AgentDepsT]) -> str:
            return _RESULTS_TEXT

        return compose

    def get_wrapper_toolset(self, toolset: AbstractToolset[AgentDepsT]) -> AbstractToolset[AgentDepsT] | None:
        """Wrap the agent's assembled toolset in a collapse wrapper for one run, with its own empty store.

        Args:
            toolset: The agent's assembled toolset.

        Returns:
            The toolset that collapses results and serves `load_result`.
        """
        return _DisclosedResults(wrapped=toolset, bindings=self.bindings, store={}, loaded=set())


@dataclass
class _DisclosedResults(WrapperToolset[AgentDepsT]):
    """Collapses disclosable results for one run, holds the full records and serves `load_result`."""

    bindings: Mapping[type, Binding]
    store: dict[str, object]
    loaded: set[str]

    async def get_tools(self, ctx: RunContext[AgentDepsT]) -> dict[str, ToolsetTool[AgentDepsT]]:
        """The wrapped tools plus the framework-managed `load_result`, whose name is reserved."""
        all_tools = await self.wrapped.get_tools(ctx)
        if LOAD_RESULT_TOOL_NAME in all_tools:
            msg = f"Tool name {LOAD_RESULT_TOOL_NAME!r} is reserved for disclosed-result loading. Rename your tool to avoid conflicts."
            raise UserError(msg)
        load_tool_def = ToolDefinition(
            name=LOAD_RESULT_TOOL_NAME,
            description=LOAD_RESULT_TOOL_DESCRIPTION,
            parameters_json_schema=_LOAD_RESULT_SCHEMA,
        )
        load_tool: ToolsetTool[AgentDepsT] = ToolsetTool(
            toolset=self,
            tool_def=load_tool_def,
            max_retries=1,
            args_validator=_load_result_args_ta.validator,  # pyright: ignore[reportArgumentType]
        )
        return {LOAD_RESULT_TOOL_NAME: load_tool, **all_tools}

    async def call_tool(
        self, name: str, tool_args: dict[str, object], ctx: RunContext[AgentDepsT], tool: ToolsetTool[AgentDepsT]
    ) -> object:
        """Serve `load_result` from the held store, and collapse any other tool's disclosable result."""
        if name == LOAD_RESULT_TOOL_NAME and tool.toolset is self:
            return self._load(tool_args)
        result = await super().call_tool(name, tool_args, ctx, tool)
        return self._collapse_result(result, tool_name=name)

    def _load(self, tool_args: dict[str, object]) -> object:
        """One held record by ref. Refuses an unknown ref or one already loaded."""
        ref = str(tool_args["ref"])
        if ref not in self.store:
            msg = f"No disclosed result with ref {ref!r}. Refs are valid only within this run, so re-run the tool that produced it."
            raise ModelRetry(msg)
        if ref in self.loaded:
            msg = f"Disclosed result {ref!r} is already loaded. Use the record already in context."
            raise ModelRetry(msg)
        self.loaded.add(ref)
        with observability.span("disclosed result load", ref=ref) as active:
            record = self.store[ref]
            active.set_attribute("disclosure.kind", type(record).__name__)
        return record

    def _collapse_result(self, result: object, *, tool_name: str) -> object:
        """The collapsed form of `result`, or `result` untouched when it carries nothing disclosable."""
        inner = result.return_value if isinstance(result, ToolReturn) else result
        entries = self._entries(inner)
        if entries is None:
            return result
        children = tuple(self._stub(item, binding) for item, binding in entries)
        envelope = DisclosureEnvelope(
            ref=f"results:{uuid4().hex[:8]}",
            kind="results",
            title=f"{len(children)} disclosed result(s)",
            children=children,
        )
        with observability.span("disclose results", tool=tool_name) as active:
            active.set_attribute("disclosure.count", len(children))
        if isinstance(result, ToolReturn):
            return ToolReturn(return_value=envelope, content=result.content, metadata=result.metadata)
        return envelope

    def _entries(self, inner: object) -> list[tuple[object, Binding]] | None:
        """`(item, binding)` per disclosable item, or `None` when the result must pass through."""
        if isinstance(inner, (list, tuple, set)):
            if not inner:
                return None
            entries: list[tuple[object, Binding]] = []
            for item in inner:
                binding = self._binding_for(item)
                if binding is None:
                    return None
                entries.append((item, binding))
            return entries
        binding = self._binding_for(inner)
        return None if binding is None else [(inner, binding)]

    def _binding_for(self, item: object) -> Binding | None:
        """The explicit binding for `item`'s type (or a base), else the structural convention, else `None`."""
        for klass in type(item).__mro__:
            binding = self.bindings.get(klass)
            if binding is not None:
                return binding
        if isinstance(item, Disclosable):
            title = _STRUCTURAL_TITLE if hasattr(item, _STRUCTURAL_TITLE) else None
            return Binding(summary=_STRUCTURAL_SUMMARY, title=title)
        return None

    def _stub(self, item: object, binding: Binding) -> ChildSummary:
        """Park `item` under a fresh ref and return its collapsed stub."""
        ref = f"result:{uuid4().hex[:8]}"
        while ref in self.store:
            ref = f"result:{uuid4().hex[:8]}"
        self.store[ref] = item
        title = str(getattr(item, binding.title)) if binding.title is not None else ""
        return ChildSummary(ref=ref, kind=type(item).__name__, title=title, summary=str(getattr(item, binding.summary)))
