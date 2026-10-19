"""The guarded, tracing retrieval toolset one evaluation trial runs behind.

Per call, in order, it refuses a call whose own arguments name a withheld reference, runs the wrapped
tool, refuses a result that is itself a withheld unit, removes withheld rows from a structured result
and rebuilds that result's next-move block from the kept rows by the builders that wrote it.
A relation whose asserting claim is withheld is dropped with the rows, even when both endpoints survive.
It then renders the payload with the same function the framework uses for the model, checks that text
names nothing withheld, stores those bytes as delivered evidence, records the call, and returns the value.
A refused call returns a plain refusal and its trace row carries `withheld_exposure`, the contamination
signal, while a removed row is never exposure.
A storage failure, a leaked reference or a size bound raises instead, and the payload never reaches the model.
A reference is never recorded for a payload that was not stored.
Every `DeliveredSource` records only its canonical reference, with no content hash and no version.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING

from core.ai.disclosure.contracts import DisclosureEnvelope
from core.ai.evaluation.contracts import DeliveredSource, ToolCallTrace
from core.ai.retrieval.contracts import FetchResult, GraphEdge, GraphPath, GraphPathsResult, GraphResult, SearchHit, SearchResult
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.toolsets import WrapperToolset

from engram.evals.outcomes.domain.withheld import WithheldRefs, refusal_text
from engram.evals.outcomes.exceptions import WithheldEvidenceLeakError
from engram.evals.shared.contracts import (
    FetchEnvelope,
    envelope_next_steps,
    graph_next_steps,
    paths_next_steps,
    search_next_steps,
    search_result_status,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from core.ai.evaluation.contracts import DeliveredEvidenceStore, TraceSink
    from pydantic_ai import RunContext
    from pydantic_ai.toolsets import ToolsetTool


@dataclass
class GuardedTracingToolset[AgentDepsT](WrapperToolset[AgentDepsT]):
    """Stores the exact result of every wrapped tool call and keeps withheld units out of it.

    One instance belongs to one trial: `trial_id` names the evidence it accumulates, and `sink`
    holds that trial's ordinals. Running two trials at once means two instances, which never share
    ordinals or an evidence total.
    """

    store: DeliveredEvidenceStore
    sink: TraceSink
    trial_id: str
    withheld: WithheldRefs
    search_hybrid: bool | None = None
    """The leg this run fixed its `search` tool to, and `None` when the model picks it per call.

    A forced-leg `search` takes no argument saying which leg ran, so rebuilding its next-move block
    would otherwise have to guess from the hits, and a forced-hybrid search whose every hit came off
    the keyword leg would be restated as a keyword-only search.
    """
    browsable: bool = False
    """Whether the wrapped `fetch` opens a map. A rebuilt `search` answer offers the map only when it does."""

    async def call_tool(self, name: str, tool_args: dict[str, object], ctx: RunContext[AgentDepsT], tool: ToolsetTool[AgentDepsT]) -> object:
        """Run one wrapped tool call, storing what it delivers before the model can read it.

        Args:
            name: The name of the tool the model called.
            tool_args: The call's arguments, whatever the called tool declares, narrowed before each is read.
            ctx: The run context of the agent making the call.
            tool: The wrapped tool definition.

        Returns:
            The scrubbed result the model reads, or the refusal text when the call named a withheld unit.

        Raises:
            DeliveredEvidenceError: The delivered payload could not be stored, could not be written
                at all, or it crossed a payload or trial size bound. The value never reaches the
                model.
            WithheldEvidenceLeakError: The finished payload still named a withheld reference.
        """
        ordinal = await self.sink.claim_tool_ordinal()
        started = perf_counter()
        try:
            named = _withheld_argument(tool_args, self.withheld)
            if named is not None:
                return await self._refuse(name, ordinal, named, started)
            value = await super().call_tool(name, tool_args, ctx, tool)
            exposed = _withheld_unit(value, self.withheld)
            if exposed is not None:
                return await self._refuse(name, ordinal, exposed, started)
            scrub = _Scrub(
                withheld=self.withheld,
                tool_args=tool_args,
                served_tools=frozenset(await self.wrapped.get_tools(ctx)),
                search_hybrid=self.search_hybrid,
                browsable=self.browsable,
            )
            return await self._deliver(name, ordinal, _without_withheld(value, scrub), started)
        except Exception:
            await self.sink.record_tool_call(
                ToolCallTrace(tool_name=name, ordinal=ordinal, status="failed", response_bytes=0, duration_ms=_elapsed_ms(started))
            )
            raise

    async def _refuse(self, name: str, ordinal: int, ref: str, started: float) -> str:
        """Store and record the refusal the model receives in place of a withheld unit."""
        payload = refusal_text(ref)
        stored = await self.store.append(self.trial_id, ordinal, payload, sources=())
        await self.sink.record_tool_call(
            ToolCallTrace(
                tool_name=name,
                ordinal=ordinal,
                status="refused",
                delivered_evidence=(stored,),
                response_bytes=stored.payload_bytes,
                duration_ms=_elapsed_ms(started),
                withheld_exposure=True,
            )
        )
        return payload

    async def _deliver(self, name: str, ordinal: int, value: object, started: float) -> object:
        """Store the exact bytes `value` will become after the leak check reads them, record the call, and return `value`."""
        payload = _model_visible_payload(name, value)
        leaked = self.withheld.named_in(payload)
        if leaked is not None:
            msg = f"the {name!r} result still named the withheld reference {leaked!r} after removal"
            raise WithheldEvidenceLeakError(msg)
        sources = _delivered_sources(value, self.withheld)
        stored = await self.store.append(self.trial_id, ordinal, payload, sources=sources)
        await self.sink.record_tool_call(
            ToolCallTrace(
                tool_name=name,
                ordinal=ordinal,
                status="ok",
                delivered_evidence=(stored,),
                returned_unit_refs=tuple(source.source_ref for source in sources),
                response_bytes=stored.payload_bytes,
                duration_ms=_elapsed_ms(started),
            )
        )
        return value


def _elapsed_ms(started: float) -> int:
    """Whole milliseconds since `started`, never negative."""
    return max(round((perf_counter() - started) * 1000), 0)


def _model_visible_payload(tool_name: str, value: object) -> str:
    """Exactly the text the framework will put in the model's tool result for `value`."""
    return ToolReturnPart(tool_name=tool_name, content=value).model_response_str()


def _hit_ref(hit: SearchHit) -> str:
    """A hit's own reference, falling back to its bare id when it carries none."""
    return hit.ref or hit.id


@dataclass(frozen=True)
class _Scrub:
    """The call's arguments and served tool names, which rebuilding a filtered next-move block reads."""

    withheld: WithheldRefs
    tool_args: Mapping[str, object]
    served_tools: frozenset[str]
    search_hybrid: bool | None = None
    browsable: bool = False

    @property
    def searched_collection(self) -> str:
        """The partition this call read, the caller's own when it named one."""
        named = self.tool_args.get("collection")
        return named if isinstance(named, str) and named else self.withheld.collection

    def text_argument(self, name: str) -> str:
        """One text argument of this call, empty when it carries none."""
        value = self.tool_args.get(name)
        return value if isinstance(value, str) else ""

    def list_argument(self, name: str) -> tuple[str, ...]:
        """One list-of-text argument of this call, empty when it carries none."""
        value = self.tool_args.get(name)
        return tuple(item for item in value if isinstance(item, str)) if isinstance(value, (list, tuple)) else ()


def _withheld_argument(tool_args: Mapping[str, object], withheld: WithheldRefs) -> str | None:
    """The first withheld reference the call's own arguments name, or `None` when they name none."""
    return next((named for named in (_withheld_in(value, withheld) for value in tool_args.values()) if named is not None), None)


def _withheld_in(value: object, withheld: WithheldRefs) -> str | None:
    """The first withheld reference inside one argument value, at any nesting depth."""
    if isinstance(value, str):
        return value if withheld.holds(value) else None
    if isinstance(value, Mapping):
        return _first_withheld(value.values(), withheld)
    if isinstance(value, (list, tuple)):
        return _first_withheld(value, withheld)
    return None


def _first_withheld(values: Iterable[object], withheld: WithheldRefs) -> str | None:
    """The first withheld reference across `values`."""
    return next((named for named in (_withheld_in(value, withheld) for value in values) if named is not None), None)


def _withheld_unit(value: object, withheld: WithheldRefs) -> str | None:
    """The withheld reference a whole-unit result IS, which cannot be scrubbed down to a partial one."""
    if isinstance(value, DisclosureEnvelope):
        return value.ref if withheld.holds(value.ref) else None
    if isinstance(value, FetchResult):
        ref = value.ref or value.id
        return ref if withheld.holds(ref) else None
    return None


def _without_withheld(value: object, scrub: _Scrub) -> object:
    """`value` without its withheld rows and with its next-move block rebuilt, untouched when nothing was removed."""
    if isinstance(value, SearchResult):
        return _search_without_withheld(value, scrub)
    if isinstance(value, GraphResult):
        return _graph_without_withheld(value, scrub)
    if isinstance(value, GraphPathsResult):
        return _paths_without_withheld(value, scrub)
    if isinstance(value, DisclosureEnvelope):
        return _envelope_without_withheld(value, scrub)
    if isinstance(value, list):
        return [row for row in value if not (isinstance(row, SearchHit) and scrub.withheld.holds(_hit_ref(row)))]
    return value


def _ran_hybrid(hits: tuple[SearchHit, ...], scrub: _Scrub) -> bool:
    """Whether this search ran the vector leg, from the fixed leg, then the call's arguments, then the hits."""
    if scrub.search_hybrid is not None:
        return scrub.search_hybrid
    bm25_only = scrub.tool_args.get("bm25_only")
    if isinstance(bm25_only, bool):
        return not bm25_only
    return any(hit.answered_by == "hybrid" for hit in hits)


def _search_without_withheld(value: SearchResult, scrub: _Scrub) -> SearchResult:
    """The same answer without its withheld hits, its status, returned count and next-move block rebuilt."""
    kept = tuple(hit for hit in value.hits if not scrub.withheld.holds(_hit_ref(hit)))
    if len(kept) == len(value.hits):
        return value
    status = search_result_status(kept)
    collection = scrub.searched_collection
    return value.model_copy(
        update={
            "hits": kept,
            "status": status,
            "window": value.window.model_copy(update={"returned_items": len(kept)}),
            "next_steps": search_next_steps(
                kept,
                query=scrub.text_argument("query"),
                knowledge_base=collection,
                hybrid=_ran_hybrid(value.hits, scrub),
                status=status,
                map_collection=collection if scrub.browsable else None,
            ),
        }
    )


def _withheld_provenance(edge: GraphEdge, withheld: WithheldRefs) -> bool:
    """Whether the claim asserting this relation is a withheld unit."""
    asserted = edge.provenance_ref or edge.provenance_claim
    return asserted is not None and withheld.holds(asserted)


def _graph_without_withheld(value: GraphResult, scrub: _Scrub) -> GraphResult:
    """The same subgraph without its withheld nodes, the edges that touched one, and the relations a withheld claim asserts."""
    kept = tuple(node for node in value.nodes if not scrub.withheld.holds(node.ref or node.id))
    reachable = {node.id for node in kept}
    edges = tuple(
        edge
        for edge in value.edges
        if edge.src_id in reachable and edge.dst_id in reachable and not _withheld_provenance(edge, scrub.withheld)
    )
    if len(kept) == len(value.nodes) and len(edges) == len(value.edges):
        return value
    steps = (
        None
        if value.next_steps is None
        else graph_next_steps(
            kept, edges, truncated=value.window.truncated, tools=scrub.served_tools, seed_ids=scrub.list_argument("seed_ids")
        )
    )
    return value.model_copy(
        update={
            "nodes": kept,
            "edges": edges,
            "window": value.window.model_copy(update={"returned_items": len(kept)}),
            "reach": None,
            "next_steps": steps,
        }
    )


def _route_reaches_withheld(path: GraphPath, withheld: WithheldRefs) -> bool:
    """Whether a route runs through a withheld node or rests on a relation a withheld claim asserts."""
    return any(withheld.holds(node.ref or node.id) for node in path.nodes) or any(
        _withheld_provenance(edge, withheld) for edge in path.edges
    )


def _paths_without_withheld(value: GraphPathsResult, scrub: _Scrub) -> GraphPathsResult:
    """The same routes minus every route that reaches a withheld unit, which has no partial form."""
    kept = tuple(path for path in value.paths if not _route_reaches_withheld(path, scrub.withheld))
    if len(kept) == len(value.paths):
        return value
    return value.model_copy(
        update={
            "paths": kept,
            "window": value.window.model_copy(update={"returned_items": len(kept)}),
            "reach": None,
            "next_steps": paths_next_steps(
                kept,
                from_id=scrub.text_argument("from_id"),
                to_id=scrub.text_argument("to_id"),
                truncated=value.window.truncated,
                tools=scrub.served_tools,
            ),
        }
    )


def _envelope_without_withheld(value: DisclosureEnvelope, scrub: _Scrub) -> DisclosureEnvelope:
    """The same disclosed unit without the child stubs naming withheld units, restated for what is left."""
    kept = tuple(child for child in value.children if not scrub.withheld.holds(child.ref))
    if len(kept) == len(value.children):
        return value
    filtered = value.model_copy(update={"children": kept})
    if not isinstance(filtered, FetchEnvelope):
        return filtered
    return filtered.model_copy(update={"next_steps": envelope_next_steps(filtered, scrub.searched_collection)})


def _delivered_sources(value: object, withheld: WithheldRefs) -> tuple[DeliveredSource, ...]:
    """Every source the payload carries, canonical and in payload order, first mention only."""
    seen: dict[str, None] = {}
    for ref in _payload_refs(value):
        seen.setdefault(withheld.canonical(ref), None)
    return tuple(DeliveredSource(source_ref=ref) for ref in seen)


def _payload_refs(value: object) -> tuple[str, ...]:
    """The references a typed result names, in the order they appear in it."""
    if isinstance(value, SearchResult):
        return tuple(_hit_ref(hit) for hit in value.hits)
    if isinstance(value, GraphResult):
        return tuple(node.ref or node.id for node in value.nodes)
    if isinstance(value, GraphPathsResult):
        return tuple(node.ref or node.id for path in value.paths for node in path.nodes)
    return _unit_refs(value)


def _unit_refs(value: object) -> tuple[str, ...]:
    """The references a single-unit or bare-hit-list result names, in the order they appear in it."""
    if isinstance(value, DisclosureEnvelope):
        return (value.ref, *(child.ref for child in value.children))
    if isinstance(value, FetchResult):
        return (value.ref or value.id,)
    if isinstance(value, list):
        return tuple(_hit_ref(row) for row in value if isinstance(row, SearchHit))
    return ()
