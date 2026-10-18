r"""Unit specs for the `RecommendStructureHandler`.

The handler is the CQRS seam wrapping the recommendation loop behind the message bus: it runs one
baseline eval, diagnoses it structurally over the knowledge base gateway, remaps the diagnosis's case
identifiers from questions to dataset case names, drafts and gates a recommendation, and persists the run
with populated per-case rows (unlike tuning's empty tuple). Every collaborator is faked
network-free: a `_FakeEvaluator` returns a scripted report of two failing pinpoint cases, a
`FakeKnowledgeBaseGateway` hands back over-budget sections and page kinds, and a `FakeRecommendationProposer`
returns a scripted `DraftRecommendation` and records the diagnosis it saw. The only IO is the dataset load
and the recommendation / draft-ADR writes under `tmp_path`, over a bare tmp knowledge base carrying the research
bundle's own knowledge-base-schema.json.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from core.cqrs.contracts import MessageBus

from engram.config import RecommendationSettings
from engram.evals.commands import RecommendationCompleted, RecommendationReport, RecommendationStarted, RecommendStructure
from engram.evals.generation.contracts import AuthoredCase, build_dataset, save_dataset
from engram.evals.handlers import RecommendStructureHandler
from engram.evals.recommendation.contracts import DraftRecommendation, OversizedSection, SectionStat, load_recommendation
from engram.evals.recommendation.domain.recommendation import (
    DraftProposal,
    PageWorkOrder,
    Prediction,
    RestructureSectionsOp,
    StructuralFinding,
)
from engram.evals.shared.domain import Archetype, ExpectedQa, QaCaseMeta, QaInput, RetrievalAnswer
from engram.evals.tuning.policy import baseline_policy, save_policy
from engram.testing.fakes.recommendation import FakeKnowledgeBaseGateway, FakeRecommendationProposer
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence
    from pathlib import Path

    from engram.evals.generation.contracts import EvaluationDataset
    from engram.evals.shared.read_models import EvaluationCaseResultRow, EvaluationRunSummary
    from engram.evals.tuning.policy import RetrievalPolicy

# The two gold papers the scripted dataset and report share (case names become `paper.a-0` / `paper.b-1`).
_GOLD = ("paper.a", "paper.b")
# A restructure that keeps the claim section `findings` so the rebuilt spec re-validates (paper has claims).
_NEW_SECTIONS = ("tl_dr", "context", "approach", "evaluation_setup", "findings", "findings_results", "limitations", "practical_notes")


@dataclass(frozen=True)
class _Score:
    """A stand-in for one `EvaluationResult` - the reader reads only `.value`."""

    value: float


@dataclass(frozen=True)
class _Case:
    """A stand-in for one `ReportCase` - carries the slice the handler folds over plus the case name the port requires."""

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
    """A stand-in for an `EvaluationReport` - the handler reads only `.cases`."""

    cases: list[_Case]


class _FakeEvaluator:
    """A `PolicyEvaluator` returning a scripted report (no model, no network), recording each call."""

    def __init__(self, report: _Report) -> None:
        self.report = report
        self.calls: list[RetrievalPolicy] = []

    async def evaluate(self, policy: RetrievalPolicy, *, dataset: EvaluationDataset) -> _Report:
        del dataset
        self.calls.append(policy)
        return self.report


class _FakeEvaluatorFactory:
    """A `PolicyEvaluatorFactory` yielding one shared `_FakeEvaluator` (readable after the run)."""

    def __init__(self, report: _Report) -> None:
        self.evaluator = _FakeEvaluator(report)

    def __call__(self, model: str | None) -> _FakeEvaluator:
        del model
        return self.evaluator


class FakeRunRepository:
    """A `SupportsRunPersistence` recording each `add_run` (the summary + its populated case rows)."""

    def __init__(self) -> None:
        self.add_run_calls: list[tuple[EvaluationRunSummary, tuple[EvaluationCaseResultRow, ...]]] = []

    async def add_run(self, run: EvaluationRunSummary, case_results: Sequence[EvaluationCaseResultRow]) -> None:
        self.add_run_calls.append((run, tuple(case_results)))


class FakeRunRepositoryFactory:
    """A `RunRepositoryFactory` yielding one canned repo, recording its scope open / close."""

    def __init__(self, repo: FakeRunRepository) -> None:
        self.repo = repo
        self.entered = 0
        self.exited = 0

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[FakeRunRepository]:
        self.entered += 1
        try:
            yield self.repo
        finally:
            self.exited += 1


class FakeGatewayFactory:
    """A `KnowledgeBaseGatewayFactory` yielding one canned gateway, recording its scope open / close."""

    def __init__(self, gateway: FakeKnowledgeBaseGateway) -> None:
        self.gateway = gateway
        self.entered = 0
        self.exited = 0

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[FakeKnowledgeBaseGateway]:
        self.entered += 1
        try:
            yield self.gateway
        finally:
            self.exited += 1


class StubClock:
    """A scripted monotonic clock: each call returns the next pre-loaded tick (deterministic duration)."""

    def __init__(self, ticks: list[float]) -> None:
        self.ticks = ticks
        self.index = 0

    def __call__(self) -> float:
        tick = self.ticks[self.index]
        self.index += 1
        return tick


def _proposer_factory(proposer: FakeRecommendationProposer):
    """An `RecommendationProposerFactory` that ignores the model spec and returns the supplied proposer."""

    def _factory(model: str | None) -> FakeRecommendationProposer:
        del model
        return proposer

    return _factory


def _case(question: str, gold: str) -> _Case:
    """One failing pinpoint report case: recall 0, gold page present, nothing cited."""
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


def _gateway() -> FakeKnowledgeBaseGateway:
    """A gateway scripted with two over-budget `findings` sections and their (paper) kinds."""
    return FakeKnowledgeBaseGateway(
        sections_over_budget_result=(
            SectionStat(page_id="paper.a", canon="findings", tokens=900),
            SectionStat(page_id="paper.b", canon="findings", tokens=800),
        ),
        page_types_result={"paper.a": "paper", "paper.b": "paper"},
    )


def _draft() -> DraftRecommendation:
    """A scripted draft: one restructure proposal on `paper` (dataset-named), plus one machinery finding."""
    op = RestructureSectionsOp(page_type="paper", canonical_sections=_NEW_SECTIONS)
    proposal = DraftProposal(
        id="restructure-paper-findings",
        op=op,
        case_names=("paper.a-0", "paper.b-1"),
        rationale="the paper findings sections exceed the retrieval budget",
        predictions=(
            Prediction(description="recall improves", target_cases=("paper.a-0", "paper.b-1"), metric="recall@5", direction="increase"),
        ),
        migration=(
            PageWorkOrder(page_id="paper.a", note_path="Memory/paper/a.md", action="split findings at Results"),
            PageWorkOrder(page_id="paper.b", note_path="Memory/paper/b.md", action="split findings at Results"),
        ),
    )
    finding = StructuralFinding(
        finding_type="machinery", title="section-level embeddings", body="consider finer embed grain", related_cases=("paper.a-0",)
    )
    return DraftRecommendation(proposals=(proposal,), findings=(finding,), rationale="split the oversized paper findings")


def _dataset_path(tmp_path: Path) -> Path:
    """Write a two-case golden dataset (names `paper.a-0` / `paper.b-1`, questions `q-paper.a` / `q-paper.b`)."""
    cases = [
        AuthoredCase(
            question=QaInput(question=f"q-{gold}"),
            expected=ExpectedQa(answer="a", expected_source_ids=(gold,)),
            meta=QaCaseMeta(anchor_page_id=gold, difficulty="easy", archetype=Archetype.PINPOINT),
        )
        for gold in _GOLD
    ]
    path = tmp_path / "golden.yaml"
    save_dataset(build_dataset(cases), path)
    return path


def _knowledge_base(tmp_path: Path) -> Path:
    """A knowledge base root carrying the research bundle's own knowledge-base-schema.json, so `load_knowledge_base_schema` resolves it."""
    root = tmp_path / "knowledge_base"
    for required in ("Units", "_meta"):
        (root / required).mkdir(parents=True)
    (root / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    return root


def _handler(
    tmp_path: Path,
    *,
    proposer: FakeRecommendationProposer,
    gateway: FakeKnowledgeBaseGateway,
    report: _Report,
    clock: StubClock | None = None,
    budget: int | None = None,
) -> tuple[RecommendStructureHandler, _FakeEvaluatorFactory, FakeRunRepositoryFactory]:
    """Build the recommend handler with faked evaluator / gateway / proposer / run-repo and a tmp knowledge_base."""
    make_evaluator = _FakeEvaluatorFactory(report)
    make_gateway = FakeGatewayFactory(gateway)
    make_run_repo = FakeRunRepositoryFactory(FakeRunRepository())
    ids = (f"run-{index}" for index in range(100))
    fixed_now = datetime(2026, 7, 3, tzinfo=UTC)
    handler = RecommendStructureHandler(
        make_evaluator,
        make_gateway,
        _proposer_factory(proposer),
        make_run_repo,
        RecommendationSettings() if budget is None else RecommendationSettings(section_token_budget=budget),
        _knowledge_base(tmp_path),
        clock=clock or StubClock([0.0, 0.0]),
        now=lambda: fixed_now,
        new_run_id=lambda: next(ids),
    )
    return handler, make_evaluator, make_run_repo


class TestRecommendStructureHandler:
    async def test_writes_the_recommendation_and_a_draft_adr_sibling(self, tmp_path: Path):
        proposer = FakeRecommendationProposer(draft=_draft())
        handler, _, _ = _handler(tmp_path, proposer=proposer, gateway=_gateway(), report=_report())
        out = tmp_path / "research.yaml"

        report = await handler(RecommendStructure(collection="research", dataset=str(_dataset_path(tmp_path)), out=str(out)))

        assert isinstance(report, RecommendationReport)
        assert out.exists()
        recommendation = load_recommendation(out)
        assert len(recommendation.proposals) == 1
        adr_path = out.with_name("research.adr.md")
        assert adr_path.exists()
        assert "Draft ADR" in adr_path.read_text(encoding="utf-8")
        assert report.recommendation_path == str(out)
        assert report.adr_path == str(adr_path)
        assert report.n_proposals == 1
        assert report.n_findings == 1
        assert report.top_proposals == ("restructure-paper-findings",)

    async def test_persists_the_run_with_populated_case_rows(self, tmp_path: Path):
        proposer = FakeRecommendationProposer(draft=_draft())
        handler, _, make_run_repo = _handler(tmp_path, proposer=proposer, gateway=_gateway(), report=_report())
        out = tmp_path / "research.yaml"

        report = await handler(RecommendStructure(collection="research", dataset=str(_dataset_path(tmp_path)), model="gpt-4.1", out=str(out)))

        assert (make_run_repo.entered, make_run_repo.exited) == (1, 1)  # one run-repo scope, cleanly closed
        summary, case_rows = make_run_repo.repo.add_run_calls[0]
        assert summary.run_id == report.run_id
        assert summary.model_name == "gpt-4.1"
        assert summary.variant["evidence_grade"] == "raw-baseline"
        # The case rows are populated (dataset-named) - not tuning's empty tuple.
        assert {row.case_name for row in case_rows} == {"paper.a-0", "paper.b-1"}

    async def test_remaps_case_identifiers_from_questions_to_dataset_names(self, tmp_path: Path):
        proposer = FakeRecommendationProposer(draft=_draft())
        handler, _, _ = _handler(tmp_path, proposer=proposer, gateway=_gateway(), report=_report())
        out = tmp_path / "research.yaml"

        await handler(RecommendStructure(collection="research", dataset=str(_dataset_path(tmp_path)), out=str(out)))

        # The proposer saw the diagnosis already in the dataset-name namespace (remap before proposer).
        diagnosis, _spec = proposer.received[0]
        assert diagnosis.section_splits[0].case_names == ("paper.a-0", "paper.b-1")
        # The gated recommendation's evidence and predictions land on dataset names (verify joins on them).
        recommendation = load_recommendation(out)
        assert recommendation.proposals[0].evidence.case_names == ("paper.a-0", "paper.b-1")
        assert recommendation.proposals[0].predictions[0].target_cases == ("paper.a-0", "paper.b-1")
        # The baseline snapshot's per-case rows carry dataset names too.
        assert {case.case_name for case in recommendation.baseline.cases} == {"paper.a-0", "paper.b-1"}

    async def test_evidence_grade_is_raw_baseline_without_a_policy(self, tmp_path: Path):
        proposer = FakeRecommendationProposer(draft=_draft())
        handler, _, _ = _handler(tmp_path, proposer=proposer, gateway=_gateway(), report=_report())
        out = tmp_path / "research.yaml"

        report = await handler(RecommendStructure(collection="research", dataset=str(_dataset_path(tmp_path)), out=str(out)))

        assert report.evidence_grade == "raw-baseline"
        assert load_recommendation(out).baseline.policy_source == "default"

    async def test_evidence_grade_is_residual_with_an_explicit_policy(self, tmp_path: Path):
        proposer = FakeRecommendationProposer(draft=_draft())
        handler, _, _ = _handler(tmp_path, proposer=proposer, gateway=_gateway(), report=_report())
        out = tmp_path / "research.yaml"
        policy_path = tmp_path / "policy.yaml"
        save_policy(baseline_policy(), policy_path)

        report = await handler(RecommendStructure(collection="research", dataset=str(_dataset_path(tmp_path)), out=str(out), policy=str(policy_path)))

        assert report.evidence_grade == "residual-of-tuned"
        assert load_recommendation(out).baseline.policy_source == "explicit"

    async def test_evidence_grade_is_residual_with_an_active_policy(self, tmp_path: Path):
        proposer = FakeRecommendationProposer(draft=_draft())
        handler, _, _ = _handler(tmp_path, proposer=proposer, gateway=_gateway(), report=_report())
        # The handler's knowledge base root is `tmp_path/knowledge_base`; drop an active policy under its `_meta/`.
        save_policy(baseline_policy(), tmp_path / "knowledge_base" / "_meta" / "active-policy.yaml")
        out = tmp_path / "research.yaml"

        report = await handler(RecommendStructure(collection="research", dataset=str(_dataset_path(tmp_path)), out=str(out)))

        assert report.evidence_grade == "residual-of-tuned"
        assert load_recommendation(out).baseline.policy_source == "active-policy"

    async def test_stamps_the_knowledge_base_and_baseline_metrics(self, tmp_path: Path):
        proposer = FakeRecommendationProposer(draft=_draft())
        handler, _, _ = _handler(tmp_path, proposer=proposer, gateway=_gateway(), report=_report())
        out = tmp_path / "research.yaml"

        await handler(RecommendStructure(dataset=str(_dataset_path(tmp_path)), collection="research", out=str(out)))

        recommendation = load_recommendation(out)
        assert recommendation.collection == "research"
        assert recommendation.baseline.knowledge_base.collection == "minimal"  # FakeKnowledgeBaseGateway's scripted stamp default
        assert recommendation.baseline.overall["recall@5"] == pytest.approx(0.0)

    async def test_emits_started_and_completed_events(self, tmp_path: Path):
        proposer = FakeRecommendationProposer(draft=_draft())
        handler, _, _ = _handler(tmp_path, proposer=proposer, gateway=_gateway(), report=_report())
        out = tmp_path / "research.yaml"

        result = await handler(RecommendStructure(collection="research", dataset=str(_dataset_path(tmp_path)), out=str(out)))

        assert [type(event) for event in handler.events] == [RecommendationStarted, RecommendationCompleted]
        started, completed = handler.events
        assert isinstance(started, RecommendationStarted)
        assert started.out == str(out)
        assert isinstance(completed, RecommendationCompleted)
        assert completed.report == result

    async def test_duration_measured_by_injected_clock(self, tmp_path: Path):
        proposer = FakeRecommendationProposer(draft=_draft())
        handler, _, _ = _handler(tmp_path, proposer=proposer, gateway=_gateway(), report=_report(), clock=StubClock([1.0, 4.5]))
        out = tmp_path / "research.yaml"

        report = await handler(RecommendStructure(collection="research", dataset=str(_dataset_path(tmp_path)), out=str(out)))

        assert report.duration_seconds == pytest.approx(3.5)

    async def test_simulate_enriches_the_restructure_evidence_and_counts_in_the_report(self, tmp_path: Path):
        # Bodies of two paragraphs (~8 tokens each) against a budget of 8: the split yields two parts,
        # and the scripted ranks say the whole body would rank 9 while the best part would rank 3.
        body = "alpha beta gamma delta epsilon zeta\n\ntheta iota kappa lambda mu nu"
        gateway = _gateway()
        gateway.oversized_section_bodies_result = (
            OversizedSection(page_id="paper.a", canon="findings", tokens=900, body=body),
            OversizedSection(page_id="paper.b", canon="findings", tokens=800, body=body),
        )
        gateway.snippet_ranks_result = (9, 3, 5)
        proposer = FakeRecommendationProposer(draft=_draft())
        handler, _, _ = _handler(tmp_path, proposer=proposer, gateway=gateway, report=_report(), budget=8)
        out = tmp_path / "research.yaml"

        report = await handler(RecommendStructure(collection="research", dataset=str(_dataset_path(tmp_path)), out=str(out), simulate=True))

        assert report.n_simulated == 1
        simulation = load_recommendation(out).proposals[0].evidence.simulation
        assert simulation is not None
        assert simulation.n_measured == 2
        assert simulation.mean_rank_before == pytest.approx(9.0)
        assert simulation.mean_rank_after == pytest.approx(3.0)
        # The ranking queries carry the cases' ORIGINAL question text, not the remapped dataset names.
        assert {call.query for call in gateway.snippet_ranks_calls} == {"q-paper.a", "q-paper.b"}

    async def test_simulate_off_touches_no_simulation_reads(self, tmp_path: Path):
        gateway = _gateway()
        proposer = FakeRecommendationProposer(draft=_draft())
        handler, _, _ = _handler(tmp_path, proposer=proposer, gateway=gateway, report=_report())
        out = tmp_path / "research.yaml"

        report = await handler(RecommendStructure(collection="research", dataset=str(_dataset_path(tmp_path)), out=str(out)))

        assert report.n_simulated == 0
        assert gateway.oversized_section_bodies_calls == []
        assert gateway.snippet_ranks_calls == []
        assert load_recommendation(out).proposals[0].evidence.simulation is None

    async def test_dispatch_through_real_message_bus(self, tmp_path: Path):
        proposer = FakeRecommendationProposer(draft=_draft())
        handler, _, _ = _handler(tmp_path, proposer=proposer, gateway=_gateway(), report=_report())
        bus = MessageBus({}, {})
        bus.register(RecommendStructure, handler)
        out = tmp_path / "research.yaml"

        result = await bus.handle(RecommendStructure(collection="research", dataset=str(_dataset_path(tmp_path)), out=str(out)))

        assert isinstance(result, RecommendationReport)
        assert handler.events == []  # the bus drained the handler's events after dispatch

    async def test_wrong_command_type_raises_type_error(self, tmp_path: Path):
        proposer = FakeRecommendationProposer(draft=_draft())
        handler, _, _ = _handler(tmp_path, proposer=proposer, gateway=_gateway(), report=_report())

        class OtherCommand(RecommendStructure.__bases__[0]):
            pass

        with pytest.raises(TypeError):
            await handler(OtherCommand())
