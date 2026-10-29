"""Unit specs for the guarded, tracing toolset one evaluation trial runs behind.

Two promises are pinned here. What the model reads is exactly what the store holds, byte for byte,
down to the framework's own rendering of a tool return. And a reference the case withholds never
reaches the model, whether the model asked for it by name or a result happened to carry it.

The wrapped toolset is a real `FunctionToolset` over scripted results, so the dispatch, the
serialization and the guard are the real ones. Every scripted result carries the next-move block the
shipped builder would have written for it, because that block is where a removed row's reference
would otherwise survive. Results are already bounded when they arrive, the way the retrieval tools
bound their own answers before returning.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from typing import TYPE_CHECKING

import pytest

from core.ai.disclosure.contracts import ChildSummary, DisclosureEnvelope
from core.ai.evaluation.contracts import FileDeliveredEvidenceStore, TraceRecorder
from core.ai.evaluation.domain.evidence import DELIVERED_PAYLOAD_BYTES
from core.ai.evaluation.exceptions import DeliveredEvidenceBoundError, DeliveredEvidencePersistenceError
from core.ai.retrieval.contracts import (
    FetchResult,
    FoldedUnit,
    GraphEdge,
    GraphNode,
    GraphPath,
    GraphPathsResult,
    GraphResult,
    NextSteps,
    ResultWindow,
    SearchHit,
    SearchResult,
)
from pydantic_ai import FunctionToolset, ModelRetry, RunContext
from pydantic_ai.messages import ToolReturnPart, tool_return_ta
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from engram.evals.outcomes.contracts import GuardedTracingToolset, WithheldEvidenceLeakError, WithheldRefs
from engram.evals.outcomes.domain.withheld import refusal_text
from engram.evals.shared.serve_bridge import (
    FetchEnvelope,
    envelope_next_steps,
    graph_next_steps,
    paths_next_steps,
    search_next_steps,
    search_result_status,
)
from engram.retrieval.budget import clip_text

if TYPE_CHECKING:
    from pathlib import Path

_COLLECTION = "research"
_SAFE = "page:research/safe"
_WITHHELD = "page:research/withheld"
_CLAIM = "unit:research/withheld-claim"
# The four tools the scripted surface serves, which is what the real builders are told a next-move
# block may send the model to.
_SERVED = frozenset({"search", "fetch", "graph_expand", "find_paths"})


# -- scripted retrieval surface ----------------------------------------------


class ScriptedTools:
    """A retrieval surface whose four tools answer with whatever the spec scripted.

    `calls` records every tool the model actually reached, so a spec can prove a refusal happened
    before the inner call rather than after it.
    """

    def __init__(self) -> None:
        self.search_result: object = _search_result((_SAFE,))
        self.fetch_result: object = _fetch_result(_SAFE)
        self.graph_result: object = _graph_result((_node(_SAFE),))
        self.paths_result: object = _paths_result(())
        self.failure: Exception | None = None
        self.calls: list[str] = []

    def toolset(self) -> FunctionToolset[object]:
        """The real `FunctionToolset` the guard wraps."""
        toolset: FunctionToolset[object] = FunctionToolset()

        async def search(query: str, bm25_only: bool = False) -> object:
            return self._answer("search", self.search_result)

        async def fetch(unit_id: str) -> object:
            return self._answer("fetch", self.fetch_result)

        async def graph_expand(seed_ids: list[str]) -> object:
            return self._answer("graph_expand", self.graph_result)

        async def find_paths(from_id: str, to_id: str) -> object:
            return self._answer("find_paths", self.paths_result)

        toolset.add_function(search, takes_ctx=False, name="search")
        toolset.add_function(fetch, takes_ctx=False, name="fetch")
        toolset.add_function(graph_expand, takes_ctx=False, name="graph_expand")
        toolset.add_function(find_paths, takes_ctx=False, name="find_paths")
        return toolset

    def _answer(self, name: str, result: object) -> object:
        self.calls.append(name)
        if self.failure is not None:
            raise self.failure
        return result


def _hit(ref: str, *, snippet: str | None = None, semantic: bool = True) -> SearchHit:
    """One ranked hit as the search tool projects it, carrying the legs that selected it.

    `semantic=False` is a hit the keyword leg alone selected, which the projection labels `lexical`
    however the call itself was run.
    """
    return SearchHit(
        id=ref.rsplit("/", maxsplit=1)[-1],
        ref=ref,
        hit_level="page",
        score=1.0,
        snippet=snippet,
        answered_by="hybrid" if semantic else "lexical",
        lexical_rank=1,
        vector_rank=1 if semantic else None,
        support="lexical_and_semantic" if semantic else "lexical_only",
    )


def _search_result(
    refs: tuple[str, ...], *, snippet: str | None = None, query: str = "anything", semantic: bool = True, hybrid: bool = True
) -> SearchResult:
    """A bounded search answer over `refs`, with the next-move block the shipped builder writes.

    `hybrid` is the leg the call ran, which the tool knows and writes into its own honesty line.
    `semantic` is whether the vector leg selected these particular hits, which is a different fact.
    """
    hits = tuple(_hit(ref, snippet=snippet, semantic=semantic) for ref in refs)
    status = search_result_status(hits)
    return SearchResult(
        hits=hits,
        status=status,
        window=ResultWindow(returned_items=len(hits)),
        next_steps=search_next_steps(
            hits, query=query, knowledge_base=_COLLECTION, hybrid=hybrid, status=status, map_collection=_COLLECTION
        ),
    )


def _fetch_result(ref: str, *, body: str = "the unit text") -> FetchResult:
    """One fetched unit."""
    return FetchResult(id=ref.rsplit("/", maxsplit=1)[-1], ref=ref, level="page", title="A page", body=body)


def _node(ref: str) -> GraphNode:
    """One graph node carrying its reference."""
    return GraphNode(id=ref.rsplit("/", maxsplit=1)[-1], ref=ref, type="page", title="A page", status="active", superseded=False)


def _edge(src: str, dst: str, *, provenance: str | None = None) -> GraphEdge:
    """One typed relation, optionally naming the claim that asserts it."""
    return GraphEdge(src_id=src, dst_id=dst, rel="cites", provenance_claim=provenance, provenance_ref=provenance)


def _graph_result(
    nodes: tuple[GraphNode, ...], edges: tuple[GraphEdge, ...] = (), *, seed_ids: tuple[str, ...] = ("safe",)
) -> GraphResult:
    """A reached subgraph with the next-move block the shipped builder writes for it."""
    return GraphResult(
        nodes=nodes,
        edges=edges,
        window=ResultWindow(returned_items=len(nodes)),
        next_steps=graph_next_steps(nodes, edges, truncated=False, tools=_SERVED, seed_ids=seed_ids),
    )


def _paths_result(paths: tuple[GraphPath, ...], *, from_id: str = "safe", to_id: str = "other") -> GraphPathsResult:
    """A ranked route list with the next-move block the shipped builder writes for it."""
    return GraphPathsResult(
        paths=paths,
        window=ResultWindow(returned_items=len(paths)),
        next_steps=paths_next_steps(paths, from_id=from_id, to_id=to_id, truncated=False, tools=_SERVED),
    )


def _disclosed(ref: str, children: tuple[ChildSummary, ...]) -> FetchEnvelope:
    """A disclosed unit with the next-move block the leveled fetch writes for it."""
    envelope = DisclosureEnvelope(ref=ref, kind="page", title="A page", content="the disclosed text", children=children)
    return FetchEnvelope(**envelope.model_dump(), next_steps=envelope_next_steps(envelope, _COLLECTION))


def _child(ref: str, title: str) -> ChildSummary:
    """One child stub under a disclosed unit."""
    return ChildSummary(ref=ref, kind="section", title=title, summary="one line")


def _run_context() -> RunContext[object]:
    """The context the toolset dispatch needs. The guard reads nothing from it."""
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


class Trial:
    """One trial's guarded toolset, its recorder and the scripted surface behind it."""

    def __init__(
        self,
        root: Path,
        trial_id: str = "trial-1",
        withheld: frozenset[str] = frozenset(),
        *,
        search_hybrid: bool | None = None,
        browsable: bool = False,
    ) -> None:
        self.tools = ScriptedTools()
        self.store = FileDeliveredEvidenceStore(root)
        self.recorder = TraceRecorder()
        self.guarded = GuardedTracingToolset[object](
            wrapped=self.tools.toolset(),
            store=self.store,
            sink=self.recorder,
            trial_id=trial_id,
            withheld=WithheldRefs(_COLLECTION, withheld),
            search_hybrid=search_hybrid,
            browsable=browsable,
        )

    async def call(self, name: str, **arguments: object) -> object:
        """Make one guarded tool call the way the framework makes it."""
        ctx = _run_context()
        tools = await self.guarded.get_tools(ctx)
        return await self.guarded.call_tool(name, dict(arguments), ctx, tools[name])

    async def stored(self, ordinal: int = 0) -> str:
        """The payload filed at `ordinal`, read back through the store."""
        return (await self.store.read(self.recorder.trace().tool_calls[ordinal].delivered_evidence[0])).payload_json


# -- serialization identity ---------------------------------------------------


class TestStoredPayloadIsWhatTheModelReads:
    async def test_the_stored_bytes_are_the_frameworks_own_tool_return_rendering(self, tmp_path: Path):
        trial = Trial(tmp_path)

        value = await trial.call("search", query="anything")

        expected = ToolReturnPart(tool_name="search", content=value, tool_call_id="call-1").model_response_str().encode("utf-8")
        assert (await trial.stored()).encode("utf-8") == expected
        assert expected == tool_return_ta.dump_json(value, by_alias=True)

    async def test_the_reference_hashes_the_exact_stored_bytes(self, tmp_path: Path):
        trial = Trial(tmp_path)

        await trial.call("search", query="anything")

        ref = trial.recorder.trace().tool_calls[0].delivered_evidence[0]
        payload = (await trial.stored()).encode("utf-8")
        assert ref.payload_sha256 == hashlib.sha256(payload).hexdigest()
        assert ref.payload_bytes == len(payload)

    async def test_the_value_returned_to_the_model_is_the_value_that_was_stored(self, tmp_path: Path):
        trial = Trial(tmp_path)

        value = await trial.call("search", query="anything")

        assert json.loads(await trial.stored()) == json.loads(tool_return_ta.dump_json(value, by_alias=True).decode("utf-8"))

    async def test_a_result_the_tool_already_shortened_is_stored_as_it_was_shortened(self, tmp_path: Path):
        # The tool bounds its own answer before returning, so the guard sees the final text. What is
        # stored is that text, never the longer one the tool started from.
        trial = Trial(tmp_path)
        full = "the complete matched passage, far longer than the budget allows"
        trial.tools.search_result = _search_result((_SAFE,), snippet=clip_text(full, 20))

        await trial.call("search", query="anything")

        clipped = clip_text(full, 20)
        payload = await trial.stored()
        assert clipped is not None
        assert clipped in payload
        assert full not in payload


# -- delivered sources --------------------------------------------------------


class TestDeliveredSources:
    async def test_every_source_ref_is_recorded_in_payload_order(self, tmp_path: Path):
        trial = Trial(tmp_path)
        trial.tools.search_result = _search_result(("page:research/first", "page:research/second", "page:research/third"))

        await trial.call("search", query="anything")

        ref = trial.recorder.trace().tool_calls[0].delivered_evidence[0]
        assert [source.source_ref for source in ref.sources] == ["page:research/first", "page:research/second", "page:research/third"]

    async def test_a_source_hash_and_version_no_result_carries_stay_unknown(self, tmp_path: Path):
        trial = Trial(tmp_path)

        await trial.call("search", query="anything")

        source = trial.recorder.trace().tool_calls[0].delivered_evidence[0].sources[0]
        assert source.source_content_sha256 is None
        assert source.source_version is None

    async def test_a_bare_id_is_recorded_as_its_collection_qualified_reference(self, tmp_path: Path):
        trial = Trial(tmp_path)
        trial.tools.search_result = SearchResult(
            hits=(SearchHit(id="loose-page", hit_level="page", score=1.0),), next_steps=NextSteps(summary="fetch it")
        )

        await trial.call("search", query="anything")

        ref = trial.recorder.trace().tool_calls[0].delivered_evidence[0]
        assert [source.source_ref for source in ref.sources] == ["page:research/loose-page"]

    async def test_graph_nodes_and_path_nodes_are_sources_too(self, tmp_path: Path):
        trial = Trial(tmp_path)
        trial.tools.graph_result = _graph_result((_node("page:research/a"), _node("page:research/b")))
        trial.tools.paths_result = _paths_result(
            (GraphPath(nodes=(_node("page:research/a"), _node("page:research/c")), edges=(), score=1.0),)
        )

        await trial.call("graph_expand", seed_ids=["a"])
        await trial.call("find_paths", from_id="a", to_id="c")

        calls = trial.recorder.trace().tool_calls
        assert [source.source_ref for source in calls[0].delivered_evidence[0].sources] == ["page:research/a", "page:research/b"]
        assert [source.source_ref for source in calls[1].delivered_evidence[0].sources] == ["page:research/a", "page:research/c"]


# -- withheld references ------------------------------------------------------


class TestWithheldReferences:
    async def test_a_withheld_row_reaches_neither_the_store_nor_the_model(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.search_result = _search_result((_SAFE, _WITHHELD))

        value = await trial.call("search", query="anything")

        assert isinstance(value, SearchResult)
        assert [hit.ref for hit in value.hits] == [_SAFE]
        assert _WITHHELD not in await trial.stored()

    async def test_removing_a_row_corrects_the_count_the_model_is_told(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.search_result = _search_result((_SAFE, _WITHHELD))

        value = await trial.call("search", query="anything")

        assert isinstance(value, SearchResult)
        assert value.window.returned_items == 1

    async def test_naming_a_withheld_reference_is_refused_before_the_tool_runs(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))

        value = await trial.call("fetch", unit_id=_WITHHELD)

        assert trial.tools.calls == []
        assert value == refusal_text(_WITHHELD)
        assert await trial.stored() == refusal_text(_WITHHELD)

    async def test_a_refusal_marks_the_trial_contaminated(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))

        await trial.call("fetch", unit_id=_WITHHELD)

        trace = trial.recorder.trace()
        assert trace.tool_calls[0].status == "refused"
        assert trace.withheld_exposures == trace.tool_calls

    async def test_a_bare_id_cannot_reach_a_withheld_unit_the_long_way_round(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))

        value = await trial.call("fetch", unit_id="withheld")

        assert trial.tools.calls == []
        assert value == refusal_text("withheld")

    async def test_a_withheld_reference_inside_a_list_argument_is_refused(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))

        value = await trial.call("graph_expand", seed_ids=["safe", "withheld"])

        assert trial.tools.calls == []
        assert value == refusal_text("withheld")

    async def test_a_result_that_is_itself_a_withheld_unit_is_refused(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.fetch_result = _fetch_result(_WITHHELD, body="the withheld text")

        value = await trial.call("fetch", unit_id="something-else")

        assert value == refusal_text(_WITHHELD)
        assert "the withheld text" not in await trial.stored()

    async def test_a_withheld_graph_node_takes_its_edges_with_it(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.graph_result = _graph_result((_node(_SAFE), _node(_WITHHELD)), (_edge("safe", "withheld"),))

        value = await trial.call("graph_expand", seed_ids=["safe"])

        assert isinstance(value, GraphResult)
        assert [node.ref for node in value.nodes] == [_SAFE]
        assert value.edges == ()

    async def test_a_route_through_a_withheld_node_is_dropped_whole(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.paths_result = _paths_result(
            (
                GraphPath(nodes=(_node(_SAFE), _node(_WITHHELD)), edges=(), score=1.0),
                GraphPath(nodes=(_node(_SAFE), _node("page:research/other")), edges=(), score=0.5),
            )
        )

        value = await trial.call("find_paths", from_id="safe", to_id="other")

        assert isinstance(value, GraphPathsResult)
        assert len(value.paths) == 1
        assert _WITHHELD not in await trial.stored()

    async def test_a_withheld_child_stub_is_removed_from_a_disclosed_unit(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.fetch_result = _disclosed(
            "page:research/parent",
            (_child(_WITHHELD, "Withheld section"), _child("section:research/parent#b", "Safe section")),
        )

        value = await trial.call("fetch", unit_id="page:research/parent")

        assert isinstance(value, DisclosureEnvelope)
        assert [child.ref for child in value.children] == ["section:research/parent#b"]

    async def test_withholding_nothing_changes_nothing(self, tmp_path: Path):
        trial = Trial(tmp_path)
        trial.tools.search_result = _search_result((_SAFE, _WITHHELD))

        value = await trial.call("search", query="anything")

        assert isinstance(value, SearchResult)
        assert [hit.ref for hit in value.hits] == [_SAFE, _WITHHELD]


_WITHHELD_SNIPPET = "The held back passage says the vote count moved."
_KEPT_SNIPPET = "The kept passage says the count was set."


def _folding(spelling: str | None) -> SearchResult:
    """A search answer whose one kept hit folds the withheld unit, its ref spelled `spelling` or absent, and one safe unit."""
    hit = _hit(_SAFE).model_copy(
        update={
            "folded_units": (
                FoldedUnit(id="withheld", hit_level="section", ref=spelling, snippet=_WITHHELD_SNIPPET),
                FoldedUnit(id="other", hit_level="section", ref="section:research/other", snippet=_KEPT_SNIPPET),
            )
        }
    )
    status = search_result_status((hit,))
    return SearchResult(
        hits=(hit,),
        status=status,
        window=ResultWindow(returned_items=1),
        next_steps=search_next_steps((hit,), query="anything", knowledge_base=_COLLECTION, hybrid=True, status=status, map_collection=_COLLECTION),
    )


class TestAWithheldFoldedUnitLeavesItsHit:
    """A withheld unit folded under a kept hit is removed from that hit, under every spelling, and the call goes through."""

    @pytest.mark.parametrize("spelling", [_WITHHELD, "section:research/withheld", "page:withheld", None], ids=["ref", "other_kind", "unqualified", "id_only"])
    async def test_the_withheld_folded_unit_is_removed_and_the_call_does_not_raise(self, tmp_path: Path, spelling: str | None):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.search_result = _folding(spelling)

        value = await trial.call("search", query="anything")

        assert isinstance(value, SearchResult)
        assert [hit.ref for hit in value.hits] == [_SAFE]
        assert [unit.id for unit in value.hits[0].folded_units] == ["other"]
        assert "research/withheld" not in await trial.stored()

    async def test_a_removed_folded_unit_is_no_exposure_and_no_folded_unit_counts_as_delivered(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.search_result = _folding(_WITHHELD)

        await trial.call("search", query="anything")

        [row] = trial.recorder.trace().tool_calls
        assert row.status == "ok"
        assert not row.withheld_exposure
        assert row.returned_unit_refs == (_SAFE,)

    async def test_the_withheld_folded_units_snippet_leaves_with_it(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.search_result = _folding(_WITHHELD)

        value = await trial.call("search", query="anything")

        assert isinstance(value, SearchResult)
        assert [unit.snippet for unit in value.hits[0].folded_units] == [_KEPT_SNIPPET]
        stored = await trial.stored()
        assert _WITHHELD_SNIPPET not in stored
        assert _KEPT_SNIPPET in stored

    async def test_a_bare_hit_list_loses_its_withheld_folded_unit_too(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.search_result = list(_folding(_WITHHELD).hits)

        value = await trial.call("search", query="anything")

        assert isinstance(value, list)
        [hit] = value
        assert isinstance(hit, SearchHit)
        assert [unit.id for unit in hit.folded_units] == ["other"]


class TestAWithheldUnitIsRefusedUnderEverySpelling:
    """A unit withheld under one spelling stays withheld under every spelling that reaches the same unit."""

    _CLAIM_ID = "paper.x#c1"

    @pytest.mark.parametrize(
        "spelling",
        [
            "paper.x#c1",
            "page:paper.x#c1",
            "section:paper.x#c1",
            "unit:paper.x#c1",
            "page:research/paper.x#c1",
            "section:research/paper.x#c1",
            "unit:research/paper.x#c1",
        ],
    )
    async def test_a_claim_withheld_as_a_unit_ref_is_refused_under_every_spelling(self, tmp_path: Path, spelling: str):
        trial = Trial(tmp_path, withheld=frozenset({f"unit:{_COLLECTION}/{self._CLAIM_ID}"}))

        value = await trial.call("fetch", unit_id=spelling)

        assert trial.tools.calls == []
        assert value == refusal_text(spelling)

    async def test_the_same_id_in_another_collection_is_another_unit(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({f"unit:{_COLLECTION}/{self._CLAIM_ID}"}))

        await trial.call("fetch", unit_id=f"unit:elsewhere/{self._CLAIM_ID}")

        assert trial.tools.calls == ["fetch"]

    async def test_a_result_spelling_the_withheld_unit_another_way_is_refused(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({f"unit:{_COLLECTION}/{self._CLAIM_ID}"}))
        trial.tools.fetch_result = _fetch_result(f"section:{_COLLECTION}/{self._CLAIM_ID}", body="the withheld text")

        value = await trial.call("fetch", unit_id="safe")

        assert value == refusal_text(f"section:{_COLLECTION}/{self._CLAIM_ID}")
        assert "the withheld text" not in await trial.stored()

    def test_free_text_naming_the_unit_under_another_prefix_is_a_leak(self):
        withheld = WithheldRefs(_COLLECTION, frozenset({f"unit:{_COLLECTION}/{self._CLAIM_ID}"}))

        assert withheld.named_in(f"read section:{_COLLECTION}/{self._CLAIM_ID} next") is not None
        assert withheld.named_in(f"read page:{_COLLECTION}/{self._CLAIM_ID} next") is not None
        assert withheld.named_in(f"read page:{_COLLECTION}/{self._CLAIM_ID}2 next") is None


class TestARebuiltSearchOffersAMapOnlyWhereFetchOpensOne:
    """A search answer rebuilt after its withheld hits are removed offers the map only when the run's `fetch` opens one."""

    async def test_a_run_whose_fetch_opens_no_map_is_offered_none(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.search_result = _search_result((_WITHHELD,))

        value = await trial.call("search", query="anything")

        assert isinstance(value, SearchResult)
        assert value.hits == ()
        assert "collection:" not in " ".join(value.next_steps.options)

    async def test_a_run_whose_fetch_opens_a_map_is_still_offered_it(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}), browsable=True)
        trial.tools.search_result = _search_result((_WITHHELD,))

        value = await trial.call("search", query="anything")

        assert isinstance(value, SearchResult)
        assert f"fetch(unit_id='collection:{_COLLECTION}')" in " ".join(value.next_steps.options)


# -- nothing names a withheld reference --------------------------------------


def _names_withheld(value: object, payload: str) -> bool:
    """Whether the withheld handle survives anywhere in what the model reads or what was stored."""
    rendered = ToolReturnPart(tool_name="probe", content=value).model_response_str()
    return _WITHHELD in rendered or _WITHHELD in payload


class TestNothingNamesAWithheldReference:
    """Removing a row is not enough. The block written from that row has to go with it."""

    async def test_a_search_block_no_longer_sends_the_model_to_the_removed_hit(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.search_result = _search_result((_WITHHELD, _SAFE))
        assert _WITHHELD in str(trial.tools.search_result)  # the tool really did write the ref into its own block

        value = await trial.call("search", query="anything")

        assert isinstance(value, SearchResult)
        assert not _names_withheld(value, await trial.stored())
        assert any(_SAFE in option for option in value.next_steps.options)

    async def test_the_rebuilt_search_block_still_names_the_leg_that_answered(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.search_result = _search_result((_WITHHELD, _SAFE), query="a real question")

        value = await trial.call("search", query="a real question")

        assert isinstance(value, SearchResult)
        assert "Answered by hybrid" in value.next_steps.summary
        assert "a real question" in value.next_steps.summary

    async def test_a_forced_leg_rebuild_states_the_leg_the_run_fixed_not_the_one_the_hits_suggest(self, tmp_path: Path):
        # A forced `search` takes no argument saying which leg ran, and every one of these hits came
        # off the keyword leg. The run knows it ran hybrid, so the rebuilt block has to say so, and
        # has to be what the tool itself would have written for the hits that remain.
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}), search_hybrid=True)
        trial.tools.search_result = _search_result((_WITHHELD, _SAFE), semantic=False, hybrid=True)

        value = await trial.call("search", query="anything")

        kept = (_hit(_SAFE, semantic=False),)
        expected = search_next_steps(
            kept,
            query="anything",
            knowledge_base=_COLLECTION,
            hybrid=True,
            status=search_result_status(kept),
            map_collection=_COLLECTION,
        )
        assert isinstance(value, SearchResult)
        assert value.next_steps == expected
        assert "Answered by hybrid" in value.next_steps.summary

    async def test_a_forced_hybrid_search_that_ran_keyword_only_rebuilds_its_block_as_lexical(self, tmp_path: Path):
        # The query could not be embedded, so the search ran keyword only and its note says why.
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}), search_hybrid=True)
        searched = _search_result((_WITHHELD, _SAFE), semantic=False, hybrid=False)
        trial.tools.search_result = searched.model_copy(update={"note": "(embedding unavailable, ran BM25-only: Connection error.)"})

        value = await trial.call("search", query="anything")

        assert isinstance(value, SearchResult)
        assert "Answered by lexical" in value.next_steps.summary
        assert value.note == "(embedding unavailable, ran BM25-only: Connection error.)"

    async def test_a_toolset_told_no_leg_reads_it_off_the_hits(self, tmp_path: Path):
        # Without the run's own answer there is only the hits' evidence, which is what the search the
        # model steers carries in its arguments instead.
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.search_result = _search_result((_WITHHELD, _SAFE), semantic=False, hybrid=True)

        value = await trial.call("search", query="anything")

        assert isinstance(value, SearchResult)
        assert "Answered by lexical" in value.next_steps.summary

    async def test_a_steered_search_reads_its_leg_off_its_own_argument(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.search_result = _search_result((_WITHHELD, _SAFE), semantic=False, hybrid=True)

        value = await trial.call("search", query="anything", bm25_only=False)

        assert isinstance(value, SearchResult)
        assert "Answered by hybrid" in value.next_steps.summary

    async def test_a_search_that_loses_every_hit_reads_as_a_search_that_found_none(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.search_result = _search_result((_WITHHELD,))

        value = await trial.call("search", query="anything")

        assert isinstance(value, SearchResult)
        assert value.hits == ()
        assert value.status == "no_hits"
        assert not _names_withheld(value, await trial.stored())

    async def test_a_graph_block_no_longer_sends_the_model_to_the_removed_node(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.graph_result = _graph_result((_node(_WITHHELD), _node(_SAFE)), (_edge("withheld", "safe"),))
        assert _WITHHELD in str(trial.tools.graph_result)

        value = await trial.call("graph_expand", seed_ids=["safe"])

        assert isinstance(value, GraphResult)
        assert not _names_withheld(value, await trial.stored())

    async def test_a_relation_a_withheld_claim_asserts_is_dropped_though_both_ends_survive(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_CLAIM}))
        trial.tools.graph_result = _graph_result(
            (_node(_SAFE), _node("page:research/other")), (_edge("safe", "other", provenance=_CLAIM),)
        )
        assert _CLAIM in str(trial.tools.graph_result)

        value = await trial.call("graph_expand", seed_ids=["safe"])

        assert isinstance(value, GraphResult)
        assert [node.ref for node in value.nodes] == [_SAFE, "page:research/other"]
        assert value.edges == ()
        payload = await trial.stored()
        assert _CLAIM not in payload
        assert _CLAIM not in ToolReturnPart(tool_name="probe", content=value).model_response_str()

    async def test_a_paths_block_no_longer_sends_the_model_to_a_removed_route(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.paths_result = _paths_result(
            (
                GraphPath(nodes=(_node(_SAFE), _node(_WITHHELD)), edges=(_edge("safe", "withheld"),), score=1.0),
                GraphPath(nodes=(_node(_SAFE), _node("page:research/other")), edges=(_edge("safe", "other"),), score=0.5),
            )
        )
        assert _WITHHELD in str(trial.tools.paths_result)

        value = await trial.call("find_paths", from_id="safe", to_id="other")

        assert isinstance(value, GraphPathsResult)
        assert len(value.paths) == 1
        assert not _names_withheld(value, await trial.stored())

    async def test_a_route_resting_on_a_withheld_claim_is_dropped_whole(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_CLAIM}))
        trial.tools.paths_result = _paths_result(
            (GraphPath(nodes=(_node(_SAFE), _node("page:research/other")), edges=(_edge("safe", "other", provenance=_CLAIM),), score=1.0),)
        )

        value = await trial.call("find_paths", from_id="safe", to_id="other")

        assert isinstance(value, GraphPathsResult)
        assert value.paths == ()
        assert _CLAIM not in await trial.stored()

    async def test_an_envelope_block_no_longer_sends_the_model_to_a_removed_child(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.fetch_result = _disclosed(
            "page:research/parent", (_child(_WITHHELD, "Withheld section"), _child("section:research/parent#b", "Safe section"))
        )
        assert _WITHHELD in str(trial.tools.fetch_result)

        value = await trial.call("fetch", unit_id="page:research/parent")

        assert isinstance(value, FetchEnvelope)
        assert not _names_withheld(value, await trial.stored())
        assert "Withheld section" not in ToolReturnPart(tool_name="probe", content=value).model_response_str()

    async def test_a_result_that_still_names_a_withheld_reference_fails_the_call(self, tmp_path: Path):
        # A shape the guard does not know how to filter reaches the last check, which refuses rather
        # than handing the model a payload that names what the case withholds.
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.search_result = {"note": f"read {_WITHHELD} for the answer"}

        with pytest.raises(WithheldEvidenceLeakError, match="still named the withheld reference"):
            await trial.call("search", query="anything")

        assert trial.recorder.trace().tool_calls[0].status == "failed"
        assert list(tmp_path.rglob("*.json")) == []

    async def test_a_longer_reference_that_merely_starts_with_a_withheld_one_is_not_a_leak(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({"page:research/a"}))
        trial.tools.search_result = {"note": "page:research/abc is a different page"}

        value = await trial.call("search", query="anything")

        assert value == {"note": "page:research/abc is a different page"}

    async def test_removing_a_row_is_not_an_exposure(self, tmp_path: Path):
        trial = Trial(tmp_path, withheld=frozenset({_WITHHELD}))
        trial.tools.search_result = _search_result((_WITHHELD, _SAFE))

        await trial.call("search", query="anything")

        trace = trial.recorder.trace()
        assert trace.tool_calls[0].status == "ok"
        assert trace.withheld_exposures == ()


# -- the trace rows -----------------------------------------------------------


class TestTraceRows:
    async def test_a_delivered_call_records_its_name_position_status_and_size(self, tmp_path: Path):
        trial = Trial(tmp_path)

        await trial.call("search", query="anything")

        call = trial.recorder.trace().tool_calls[0]
        assert (call.tool_name, call.ordinal, call.status) == ("search", 0, "ok")
        assert call.response_bytes == len((await trial.stored()).encode("utf-8"))
        assert call.duration_ms >= 0

    async def test_no_tool_call_is_charged_a_model_turns_price(self, tmp_path: Path):
        trial = Trial(tmp_path)

        await trial.call("search", query="anything")

        assert trial.recorder.trace().tool_calls[0].direct_cost_usd is None

    async def test_the_returned_references_are_recorded_beside_the_payload(self, tmp_path: Path):
        trial = Trial(tmp_path)
        trial.tools.search_result = _search_result((_SAFE, "page:research/other"))

        await trial.call("search", query="anything")

        assert trial.recorder.trace().tool_calls[0].returned_unit_refs == (_SAFE, "page:research/other")

    async def test_calls_are_recorded_at_successive_positions(self, tmp_path: Path):
        trial = Trial(tmp_path)

        await trial.call("search", query="one")
        await trial.call("fetch", unit_id="safe")

        assert [call.ordinal for call in trial.recorder.trace().tool_calls] == [0, 1]

    async def test_a_tool_that_raises_is_recorded_as_failed_and_raises_on(self, tmp_path: Path):
        trial = Trial(tmp_path)
        trial.tools.failure = ModelRetry("no unit found, re-ground the id")

        with pytest.raises(ModelRetry):
            await trial.call("fetch", unit_id="missing")

        call = trial.recorder.trace().tool_calls[0]
        assert call.status == "failed"
        assert call.delivered_evidence == ()


# -- size bounds fail the trial ------------------------------------------------


class TestBoundsFailTheTrial:
    async def test_a_payload_over_the_payload_bound_never_reaches_the_model(self, tmp_path: Path):
        trial = Trial(tmp_path)
        trial.tools.fetch_result = _fetch_result(_SAFE, body="x" * (DELIVERED_PAYLOAD_BYTES + 1))

        with pytest.raises(DeliveredEvidenceBoundError, match="262144 byte payload bound"):
            await trial.call("fetch", unit_id="safe")

        assert trial.recorder.trace().tool_calls[0].status == "failed"

    async def test_crossing_the_trial_bound_fails_the_call_that_would_cross_it(self, tmp_path: Path):
        trial = Trial(tmp_path)
        trial.tools.fetch_result = _fetch_result(_SAFE, body="x" * (DELIVERED_PAYLOAD_BYTES - 200))
        for _ in range(4):
            await trial.call("fetch", unit_id="safe")

        trial.tools.fetch_result = _fetch_result(_SAFE, body="y" * (DELIVERED_PAYLOAD_BYTES - 200))
        with pytest.raises(DeliveredEvidenceBoundError, match="1048576 byte trial bound"):
            await trial.call("fetch", unit_id="safe")


class TestStorageFailuresAreTyped:
    async def test_a_state_root_that_cannot_hold_evidence_fails_the_call_with_a_typed_error(self, tmp_path: Path):
        # A caller folding a trial's evidence to unknown catches the evidence error, so a filesystem
        # error must never be what reaches it.
        occupied = tmp_path / "root"
        occupied.write_text("this is a file, not a state root", encoding="utf-8")
        trial = Trial(occupied)

        with pytest.raises(DeliveredEvidencePersistenceError):
            await trial.call("search", query="anything")

        assert trial.recorder.trace().tool_calls[0].status == "failed"

    async def test_nothing_reaches_the_model_when_the_store_cannot_write(self, tmp_path: Path):
        occupied = tmp_path / "root"
        occupied.write_text("this is a file, not a state root", encoding="utf-8")
        trial = Trial(occupied)

        with pytest.raises(DeliveredEvidencePersistenceError):
            await trial.call("fetch", unit_id="safe")

        assert trial.recorder.trace().tool_calls[0].delivered_evidence == ()


# -- evidence outlives the source ---------------------------------------------


class TestEvidenceOutlivesTheSource:
    async def test_a_later_answer_does_not_change_what_an_earlier_reference_reads(self, tmp_path: Path):
        trial = Trial(tmp_path)
        trial.tools.fetch_result = _fetch_result(_SAFE, body="the original text")
        await trial.call("fetch", unit_id="safe")
        first = trial.recorder.trace().tool_calls[0].delivered_evidence[0]

        trial.tools.fetch_result = _fetch_result(_SAFE, body="the rewritten text")
        await trial.call("fetch", unit_id="safe")

        assert "the original text" in (await trial.store.read(first)).payload_json
        assert "the rewritten text" not in (await trial.store.read(first)).payload_json


# -- concurrent trials ---------------------------------------------------------


class TestConcurrentTrials:
    async def test_two_trials_under_one_store_keep_separate_positions_and_evidence(self, tmp_path: Path):
        first = Trial(tmp_path, trial_id="trial-a")
        second = Trial(tmp_path, trial_id="trial-b")
        first.tools.search_result = _search_result(("page:research/a",))
        second.tools.search_result = _search_result(("page:research/b",))

        await asyncio.gather(
            *(trial.call("search", query="anything") for trial in (first, second) for _ in range(3)),
        )

        assert [call.ordinal for call in first.recorder.trace().tool_calls] == [0, 1, 2]
        assert [call.ordinal for call in second.recorder.trace().tool_calls] == [0, 1, 2]
        assert "page:research/b" not in await first.stored()
        assert "page:research/a" not in await second.stored()
        assert len(list((tmp_path / "delivered-evidence").iterdir())) == 2
