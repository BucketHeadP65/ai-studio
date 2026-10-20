r"""Unit specs for the three write-capable MCP recommendation tools.

Three OPT-IN tools the Claude Code client drives over MCP to act as the recommendation loop's
"controller brain", gated under the same `engram mcp --authoring` opt-in as the tuning tools
so the default surface stays read-only:

* `diagnose_structure(dataset, policy_json, model, collection)` runs the recommend pipeline's read half
  (resolve policy, run the baseline eval, diagnose it, then the three detectors over the knowledge base
  gateway, remapping case identifiers from questions to dataset case names) and returns the
  `StructuralDiagnosis` as JSON, so the brain sees the schema-level weaknesses before it drafts;
* `what_if_edges(edges_json, pairs_json, max_hops, collection)` measures the graph counterfactual
  (reachability before vs after injecting hypothetical typed edges) and returns a
  `CounterfactualResult` as JSON, a judge-free upper bound on an add-relation fix;
* `submit_recommendation(recommendation_json, name)` validates the recommendation at the boundary, re-validates every
  proposal op against the knowledge base's current spec via `apply_op`, and saves the `<name>.yaml` artifact
  plus its MADR-shaped `<name>.adr.md` sibling, so a fabricated or invalid edit is rejected regardless
  of what the client sends.

The tests pin the contract DI-friendly and transport-free, mirroring `test_mcp_tuning.py`: build
an in-memory `MCPServer`, register the recommendation tools against fake gateway / evaluator factories, and
drive the tools through the SDK's in-memory client to server session. The gate is the headline: an
invalid recommendation JSON, an op that fails `apply_op`, and an unsafe name are each rejected without a
write.
"""

from __future__ import annotations

import json

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from engram.config import RecommendationSettings
from engram.evals.generation.contracts import AuthoredCase, build_dataset, save_dataset
from engram.evals.recommendation.contracts import (
    BaselineSnapshot,
    CandidateEdge,
    KnowledgeBaseStamp,
    SectionStat,
    StructuralDiagnosis,
    load_recommendation,
    register_recommendation_tools,
)
from engram.evals.recommendation.domain.recommendation import (
    CounterfactualResult,
    Prediction,
    ProposalEvidence,
    Recommendation,
    RestructureSectionsOp,
    SchemaEditProposal,
)
from engram.evals.recommendation.infrastructure.mcp_recommendation import DIAGNOSE_STRUCTURE, SUBMIT_RECOMMENDATION, WHAT_IF_EDGES
from engram.evals.shared.domain import Archetype, ExpectedQa, QaCaseMeta, QaInput, RetrievalAnswer
from engram.evals.tuning.policy import RetrievalPolicy
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence
    from pathlib import Path

    from mcp.types import CallToolResult

    from engram.evals.commands import RecommendationKnowledgeBaseGateway
    from engram.evals.generation.contracts import EvaluationDataset
    from engram.evals.rca.contracts import ReportLike
    from engram.evals.recommendation.contracts import MinedLink, OversizedSection
    from engram.evals.recommendation.domain.recommendation import SchemaEditOp
    from engram.evals.tuning.internal_contracts import PolicyEvaluator

# A restructure whose sections keep the claim section `findings` so the rebuilt research-default spec
# re-validates (a `paper` page type carries claims). The op is valid by construction against `apply_op`.
_NEW_SECTIONS = ("tl_dr", "context", "approach", "evaluation_setup", "findings", "findings_results", "limitations", "practical_notes")


# -- structural report stand-ins (the diagnose / detector inputs, mirror test_handlers_recommend) --


@dataclass(frozen=True)
class _Score:
    """A stand-in for one `EvaluationResult`; the readers read only `.value`."""

    value: float


@dataclass(frozen=True)
class _Case:
    """A stand-in for one `ReportCase`; carries the slice the pipeline folds over plus the case name the port requires."""

    name: str
    inputs: QaInput
    metadata: QaCaseMeta | None
    expected_output: RetrievalAnswer | None
    output: RetrievalAnswer
    scores: dict[str, _Score]
    metrics: dict[str, float | int]
    total_duration: float


@dataclass(frozen=True)
class _Report:
    """A stand-in for an `EvaluationReport`; the readers read only `.cases`."""

    cases: list[_Case]


def _case(question: str, gold: str) -> _Case:
    """One failing pinpoint report case: recall 0, the gold page present, nothing cited."""
    return _Case(
        name=gold,
        inputs=QaInput(question=question),
        metadata=QaCaseMeta(anchor_page_id=gold, difficulty="easy", archetype=Archetype.PINPOINT),
        expected_output=RetrievalAnswer(answer="ref", cited_source_ids=[gold]),
        output=RetrievalAnswer(answer="got", cited_source_ids=[]),
        scores={"recall@5": _Score(0.0), "n_tool_calls": _Score(2.0)},
        metrics={"input_tokens": 100.0},
        total_duration=0.2,
    )


def _report() -> _Report:
    """A two-case report, both failing pinpoint cases whose gold pages carry over-budget sections."""
    return _Report(cases=[_case("q-paper.a", "paper.a"), _case("q-paper.b", "paper.b")])


# -- the faked measurement port + factory ------------------------------------


class _FakeEvaluator:
    """A `PolicyEvaluator` returning a scripted report (no model, no network), recording each policy."""

    def __init__(self, report: _Report) -> None:
        self.report = report
        self.evaluate_calls: list[RetrievalPolicy] = []

    async def evaluate(self, policy: RetrievalPolicy, *, dataset: EvaluationDataset) -> ReportLike:
        del dataset  # the fake scores from its canned report, not the dataset's cases
        self.evaluate_calls.append(policy)
        return self.report


@dataclass
class _FakeEvaluatorFactory:
    """A `PolicyEvaluatorFactory`: a callable returning `evaluator`, recording each model spec seen."""

    evaluator: _FakeEvaluator
    models: list[str | None] = field(default_factory=list)

    def __call__(self, model: str | None) -> PolicyEvaluator:
        self.models.append(model)
        return self.evaluator


# -- the faked knowledge base gateway ------------------------------------------------


@dataclass
class _FakeGateway:
    """A structural `RecommendationKnowledgeBaseGateway`: canned section / page-kind reads and a before/after graph.

    `reachable_pairs` returns `reachable_after` when candidate edges are injected and `reachable_before`
    otherwise, so `what_if_edges` measures a real reachability delta. The other four reads hand back
    their canned results so `diagnose_structure` can fold a `StructuralDiagnosis`.
    """

    sections_over_budget_result: Sequence[SectionStat] = ()
    page_types_result: dict[str, str] = field(default_factory=dict)
    reachable_before: Sequence[bool] = ()
    reachable_after: Sequence[bool] = ()
    unlifted_links_result: Sequence[MinedLink] = ()
    stamp_result: KnowledgeBaseStamp = field(
        default_factory=lambda: KnowledgeBaseStamp(collection="research", spec_hash="", manifest_digest="", n_pages=0)
    )
    reachable_calls: list[tuple[tuple[tuple[str, str], ...], tuple[CandidateEdge, ...]]] = field(default_factory=list)

    async def sections_over_budget(self, page_ids: Sequence[str], *, token_budget: int, collection: str) -> Sequence[SectionStat]:
        del page_ids, token_budget, collection
        return self.sections_over_budget_result

    async def page_types(self, page_ids: Sequence[str], *, collection: str) -> dict[str, str]:
        del page_ids, collection
        return self.page_types_result

    async def reachable_pairs(
        self, pairs: Sequence[tuple[str, str]], *, max_hops: int, collection: str, extra_edges: Sequence[CandidateEdge] = ()
    ) -> Sequence[bool]:
        del max_hops, collection
        self.reachable_calls.append((tuple(pairs), tuple(extra_edges)))
        return self.reachable_after if extra_edges else self.reachable_before

    async def unlifted_links(self, *, collection: str) -> Sequence[MinedLink]:
        del collection
        return self.unlifted_links_result

    async def stamp(self, *, collection: str) -> KnowledgeBaseStamp:
        del collection
        return self.stamp_result

    async def changed_paths(self, since: datetime, *, collection: str) -> Sequence[str]:
        del since, collection
        return ()

    async def oversized_section_bodies(self, page_ids: Sequence[str], *, token_budget: int, collection: str) -> Sequence[OversizedSection]:
        del page_ids, token_budget, collection
        return ()

    async def snippet_ranks(self, query: str, snippets: Sequence[str], *, collection: str) -> Sequence[int]:
        del query, collection
        return [1] * len(snippets)


def _gateway_factory(gateway: RecommendationKnowledgeBaseGateway):
    """A `KnowledgeBaseGatewayFactory`: an async-CM yielding `gateway` (one gateway per command, the real shape)."""

    @asynccontextmanager
    async def factory() -> AsyncGenerator[RecommendationKnowledgeBaseGateway]:
        yield gateway

    return factory


# -- builders ----------------------------------------------------------------


def _dataset(tmp_path: Path, name: str = "golden") -> Path:
    """A two-case golden dataset (names `paper.a-0` / `paper.b-1`) under `<tmp_path>/datasets/<name>.yaml`."""
    datasets_dir = tmp_path / "datasets"
    datasets_dir.mkdir(parents=True, exist_ok=True)
    cases = [
        AuthoredCase(
            question=QaInput(question=f"q-{gold}"),
            expected=ExpectedQa(answer="a", expected_source_ids=(gold,)),
            meta=QaCaseMeta(anchor_page_id=gold, difficulty="easy", archetype=Archetype.PINPOINT),
        )
        for gold in ("paper.a", "paper.b")
    ]
    save_dataset(build_dataset(cases), datasets_dir / f"{name}.yaml")
    return datasets_dir / f"{name}.yaml"


def _knowledge_base(tmp_path: Path) -> Path:
    """A knowledge base root carrying the research bundle's own knowledge-base-schema.json, so `load_knowledge_base_schema` resolves it."""
    root = tmp_path / "knowledge_base"
    (root / "_meta").mkdir(parents=True)
    (root / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    return root


def _server(tmp_path: Path, *, gateway: RecommendationKnowledgeBaseGateway | None = None, factory: _FakeEvaluatorFactory | None = None) -> MCPServer:
    """An in-memory server with only the three recommendation tools registered over `tmp_path` dirs."""
    server = MCPServer(name="recommendation-test")
    register_recommendation_tools(
        server,
        gateway_factory=_gateway_factory(gateway or _FakeGateway()),
        evaluator_factory=factory or _FakeEvaluatorFactory(_FakeEvaluator(_report())),
        recommendation_settings=RecommendationSettings(),
        datasets_dir=tmp_path / "datasets",
        recommendations_dir=tmp_path / "recommendations",
        knowledge_base_path=_knowledge_base(tmp_path),
    )
    return server


def _text(result: CallToolResult) -> str:
    """The first text block of a tool result (the tools return a single text payload)."""
    block = result.content[0]
    assert block.type == "text"
    return block.text


def _recommendation(op: SchemaEditOp) -> Recommendation:
    """A minimal one-proposal `Recommendation` carrying `op` (dataset-named evidence)."""
    return Recommendation(
        collection="research",
        created_at=datetime(2026, 7, 3, tzinfo=UTC),
        baseline=BaselineSnapshot(
            dataset="golden",
            run_id=None,
            policy_source="default",
            evidence_grade="raw-baseline",
            knowledge_base=KnowledgeBaseStamp(collection="research", spec_hash="h", manifest_digest="d", n_pages=1),
            overall={"recall@5": 0.4},
            cases=(),
        ),
        proposals=(
            SchemaEditProposal(
                id="restructure-paper-findings",
                op=op,
                evidence=ProposalEvidence(
                    case_names=("paper.a-0", "paper.b-1"), recall_deficit=0.2, counterfactual=None, mined_links=(), oversized_sections=()
                ),
                predictions=(
                    Prediction(
                        description="recall improves", target_cases=("paper.a-0", "paper.b-1"), metric="recall@5", direction="increase"
                    ),
                ),
                migration=(),
                rationale="split the oversized paper findings",
            ),
        ),
        findings=(),
    )


def _valid_recommendation_json() -> str:
    """A `Recommendation` JSON whose op re-validates against the research-default spec."""
    return _recommendation(RestructureSectionsOp(page_type="paper", canonical_sections=_NEW_SECTIONS)).model_dump_json()


def _fabricated_op_recommendation_json() -> str:
    """A `Recommendation` JSON whose op names a page type that does not exist (fails `apply_op`)."""
    return _recommendation(RestructureSectionsOp(page_type="nonexistent_kind", canonical_sections=("a", "b"))).model_dump_json()


# -- registration ------------------------------------------------------------


class TestRegistration:
    async def test_registers_the_three_recommendation_tools(self, tmp_path: Path):
        server = _server(tmp_path)

        async with Client(server) as client:
            listing = await client.list_tools()

        names = {tool.name for tool in listing.tools}
        assert {DIAGNOSE_STRUCTURE, WHAT_IF_EDGES, SUBMIT_RECOMMENDATION} <= names

    async def test_descriptions_orient_the_diagnose_probe_submit_loop(self, tmp_path: Path):
        server = _server(tmp_path)

        async with Client(server) as client:
            listing = await client.list_tools()

        by_name = {tool.name: (tool.description or "") for tool in listing.tools}
        assert by_name[DIAGNOSE_STRUCTURE]
        assert by_name[WHAT_IF_EDGES]
        assert by_name[SUBMIT_RECOMMENDATION]
        blob = " ".join(by_name[name].lower() for name in (DIAGNOSE_STRUCTURE, WHAT_IF_EDGES, SUBMIT_RECOMMENDATION))
        assert "recommendation" in blob  # the loop records a recommendation
        assert "spec" in blob or "schema" in blob  # the edit target is the knowledge base schema spec


# -- diagnose_structure ------------------------------------------------------


class TestDiagnoseStructure:
    async def test_returns_a_valid_structural_diagnosis_over_fakes(self, tmp_path: Path):
        _dataset(tmp_path)
        gateway = _FakeGateway(
            sections_over_budget_result=(
                SectionStat(page_id="paper.a", canon="findings", tokens=900),
                SectionStat(page_id="paper.b", canon="findings", tokens=800),
            ),
            page_types_result={"paper.a": "paper", "paper.b": "paper"},
        )
        server = _server(tmp_path, gateway=gateway)

        async with Client(server) as client:
            result = await client.call_tool(DIAGNOSE_STRUCTURE, {"dataset": "golden"})

        assert result.is_error is False
        diagnosis = StructuralDiagnosis.model_validate_json(_text(result))
        # The section-split signal fired over the over-budget gold sections, remapped to dataset names.
        assert diagnosis.section_splits
        assert diagnosis.section_splits[0].page_type == "paper"
        assert set(diagnosis.section_splits[0].case_names) == {"paper.a-0", "paper.b-1"}

    async def test_default_policy_grades_raw_baseline_and_uses_the_default_model(self, tmp_path: Path):
        _dataset(tmp_path)
        factory = _FakeEvaluatorFactory(_FakeEvaluator(_report()))
        server = _server(tmp_path, factory=factory)

        async with Client(server) as client:
            result = await client.call_tool(DIAGNOSE_STRUCTURE, {"dataset": "golden"})

        diagnosis = StructuralDiagnosis.model_validate_json(_text(result))
        assert diagnosis.evidence_grade == "raw-baseline"  # no policy supplied
        assert factory.models == ["test-model"]  # the network-free default backend

    async def test_supplying_a_policy_grades_residual_of_tuned(self, tmp_path: Path):
        _dataset(tmp_path)
        server = _server(tmp_path)
        policy_json = RetrievalPolicy(instructions="Be precise.", strategy="search-hybrid", tool_descriptions={}).model_dump_json()

        async with Client(server) as client:
            result = await client.call_tool(DIAGNOSE_STRUCTURE, {"dataset": "golden", "policy_json": policy_json})

        diagnosis = StructuralDiagnosis.model_validate_json(_text(result))
        assert diagnosis.evidence_grade == "residual-of-tuned"

    async def test_invalid_policy_json_is_returned_as_an_actionable_error(self, tmp_path: Path):
        _dataset(tmp_path)
        factory = _FakeEvaluatorFactory(_FakeEvaluator(_report()))
        server = _server(tmp_path, factory=factory)
        invalid = json.dumps({"instructions": "x", "strategy": "code-mode", "tool_descriptions": {}})

        async with Client(server) as client:
            result = await client.call_tool(DIAGNOSE_STRUCTURE, {"dataset": "golden", "policy_json": invalid})

        payload = json.loads(_text(result))
        assert "error" in payload
        assert factory.evaluator.evaluate_calls == []  # the invalid policy never reached the evaluator

    async def test_traversal_dataset_is_rejected(self, tmp_path: Path):
        factory = _FakeEvaluatorFactory(_FakeEvaluator(_report()))
        server = _server(tmp_path, factory=factory)

        async with Client(server) as client:
            result = await client.call_tool(DIAGNOSE_STRUCTURE, {"dataset": "../evil"})

        assert result.is_error is False  # a bad name is an actionable answer, not a torn-down session
        payload = json.loads(_text(result))
        assert "error" in payload
        assert factory.evaluator.evaluate_calls == []  # the guard fired before any evaluation


# -- what_if_edges -----------------------------------------------------------


class TestWhatIfEdges:
    async def test_round_trips_a_counterfactual_over_a_scripted_gateway(self, tmp_path: Path):
        gateway = _FakeGateway(reachable_before=(False, False), reachable_after=(True, False))
        server = _server(tmp_path, gateway=gateway)
        edges = json.dumps([{"src_id": "paper.a", "dst_id": "paper.b", "rel": "extends"}])
        pairs = json.dumps([["paper.a", "paper.b"], ["paper.a", "paper.c"]])

        async with Client(server) as client:
            result = await client.call_tool(WHAT_IF_EDGES, {"edges_json": edges, "pairs_json": pairs, "max_hops": 3})

        assert result.is_error is False
        counterfactual = CounterfactualResult.model_validate_json(_text(result))
        assert counterfactual.n_pairs == 2
        assert counterfactual.reachable_before == 0
        assert counterfactual.reachable_after == 1  # the injected edge connected one previously unreachable pair
        assert counterfactual.candidate_edges[0].rel == "extends"
        # The gateway saw a before call (no edges) and an after call (with the candidate edge injected).
        assert gateway.reachable_calls[0][1] == ()
        assert gateway.reachable_calls[1][1] == (CandidateEdge(src_id="paper.a", dst_id="paper.b", rel="extends"),)

    async def test_malformed_edges_json_is_returned_as_an_actionable_error(self, tmp_path: Path):
        server = _server(tmp_path)

        async with Client(server) as client:
            result = await client.call_tool(WHAT_IF_EDGES, {"edges_json": "{not json", "pairs_json": "[]", "max_hops": 3})

        assert result.is_error is False
        payload = json.loads(_text(result))
        assert "error" in payload

    async def test_malformed_pairs_json_is_returned_as_an_actionable_error(self, tmp_path: Path):
        server = _server(tmp_path)
        edges = json.dumps([{"src_id": "a", "dst_id": "b", "rel": "r"}])

        async with Client(server) as client:
            result = await client.call_tool(WHAT_IF_EDGES, {"edges_json": edges, "pairs_json": "{not a list", "max_hops": 3})

        payload = json.loads(_text(result))
        assert "error" in payload


# -- submit_recommendation ---------------------------------------------------------


class TestSubmitRecommendation:
    async def test_writes_a_round_trippable_recommendation_and_a_draft_adr_sibling(self, tmp_path: Path):
        server = _server(tmp_path)

        async with Client(server) as client:
            result = await client.call_tool(SUBMIT_RECOMMENDATION, {"recommendation_json": _valid_recommendation_json(), "name": "winner"})

        assert result.is_error is False
        payload = json.loads(_text(result))
        recommendation_path = tmp_path / "recommendations" / "winner.yaml"
        adr_path = tmp_path / "recommendations" / "winner.adr.md"
        assert payload["saved"] == str(recommendation_path)
        assert payload["adr"] == str(adr_path)
        assert recommendation_path.exists()
        assert adr_path.exists()
        assert "Draft ADR" in adr_path.read_text(encoding="utf-8")
        # The artifact round-trips back to an equal recommendation through the loader.
        loaded = load_recommendation(recommendation_path)
        assert loaded.proposals[0].id == "restructure-paper-findings"

    async def test_invalid_recommendation_json_is_rejected_without_writing(self, tmp_path: Path):
        server = _server(tmp_path)

        async with Client(server) as client:
            result = await client.call_tool(SUBMIT_RECOMMENDATION, {"recommendation_json": "{not valid recommendation", "name": "winner"})

        assert result.is_error is False
        payload = json.loads(_text(result))
        assert "error" in payload
        assert not (tmp_path / "recommendations" / "winner.yaml").exists()

    async def test_fabricated_op_is_rejected_by_the_gate_without_writing(self, tmp_path: Path):
        server = _server(tmp_path)

        async with Client(server) as client:
            result = await client.call_tool(SUBMIT_RECOMMENDATION, {"recommendation_json": _fabricated_op_recommendation_json(), "name": "winner"})

        assert result.is_error is False  # a bad op is an actionable answer, not a torn-down session
        payload = json.loads(_text(result))
        assert "error" in payload
        # The op re-validation dropped the whole recommendation (unknown page-type kind), nothing was written.
        assert not (tmp_path / "recommendations" / "winner.yaml").exists()

    async def test_traversal_name_is_rejected_without_writing_outside_the_dir(self, tmp_path: Path):
        server = _server(tmp_path)

        async with Client(server) as client:
            result = await client.call_tool(SUBMIT_RECOMMENDATION, {"recommendation_json": _valid_recommendation_json(), "name": "../evil"})

        assert result.is_error is False
        payload = json.loads(_text(result))
        assert "error" in payload
        assert not (tmp_path / "recommendations").exists() or not any((tmp_path / "recommendations").iterdir())
        assert not (tmp_path / "evil.yaml").exists()  # nothing escaped recommendations_dir

    async def test_path_separator_name_is_rejected(self, tmp_path: Path):
        server = _server(tmp_path)

        async with Client(server) as client:
            result = await client.call_tool(SUBMIT_RECOMMENDATION, {"recommendation_json": _valid_recommendation_json(), "name": "nested/evil"})

        payload = json.loads(_text(result))
        assert "error" in payload
        assert not (tmp_path / "recommendations" / "nested").exists()
