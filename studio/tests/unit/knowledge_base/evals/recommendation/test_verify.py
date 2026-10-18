"""Unit specs for `engram.evals.recommendation.domain.verify`, the mechanized post-apply protocol.

These specs pin the verify contract: the retained/dropped partition keeps only baseline cases whose
gold pages still exist and which the re-run measured, per-prediction verdicts are judged mechanically
(`increase` needs a positive mean delta over the retained targets, `no_regression` tolerates none),
targeted and untargeted deltas are reported separately, and knowledge base drift is attributed through the
index manifest, content changes outside the migration briefs raise a loud unattributed-drift
warning. The markdown rendering is deterministic and cites the verdicts verbatim.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from engram.evals.generation.contracts import AuthoredCase, build_dataset, save_dataset
from engram.evals.handlers import VerifyRecommendationHandler
from engram.evals.recommendation.contracts import (
    BaselineSnapshot,
    CaseSnapshot,
    KnowledgeBaseStamp,
    render_verify_markdown,
    save_recommendation,
    verify_recommendation,
)
from engram.evals.recommendation.domain.recommendation import (
    PageWorkOrder,
    Prediction,
    ProposalEvidence,
    Recommendation,
    RestructureSectionsOp,
    SchemaEditProposal,
)
from engram.evals.shared.domain import Archetype, ExpectedQa, QaCaseMeta, QaInput, RetrievalAnswer
from engram.testing.fakes.recommendation import FakeKnowledgeBaseGateway
from typer.testing import CliRunner

from studio.knowledge_base.cli import app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence
    from pathlib import Path

    from engram.evals.generation.contracts import EvaluationDataset
    from engram.evals.shared.read_models import EvaluationCaseResultRow, EvaluationRunSummary
    from engram.evals.tuning.policy import RetrievalPolicy

runner = CliRunner()


def _stamp(*, spec_hash: str = "spec-1", manifest_digest: str = "manifest-1", n_pages: int = 2) -> KnowledgeBaseStamp:
    return KnowledgeBaseStamp(collection="research", spec_hash=spec_hash, manifest_digest=manifest_digest, n_pages=n_pages)


def _proposal(*, predictions: Sequence[Prediction], briefs: Sequence[PageWorkOrder] = ()) -> SchemaEditProposal:
    return SchemaEditProposal(
        id="restructure-paper-sections",
        op=RestructureSectionsOp(page_type="paper", canonical_sections=("summary", "findings-a", "findings-b")),
        evidence=ProposalEvidence(case_names=("c1", "c2"), recall_deficit=0.5, counterfactual=None, mined_links=(), oversized_sections=()),
        predictions=tuple(predictions),
        migration=tuple(briefs) or (PageWorkOrder(page_id="p:1", note_path="Memory/Papers/a.md", action="split findings"),),
        rationale="oversized findings",
    )


def _recommendation(*, predictions: Sequence[Prediction], briefs: Sequence[PageWorkOrder] = ()) -> Recommendation:
    return Recommendation(
        collection="research",
        created_at=datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
        baseline=BaselineSnapshot(
            dataset="ds.yaml",
            run_id="run-0",
            policy_source="default",
            evidence_grade="raw-baseline",
            knowledge_base=_stamp(),
            overall={"recall@5": 0.4},
            cases=(
                CaseSnapshot(case_name="c1", recall=0.0, gold_ids=("p:1",), cited_ids=()),
                CaseSnapshot(case_name="c2", recall=0.5, gold_ids=("p:1", "p:2"), cited_ids=("p:1",)),
                CaseSnapshot(case_name="c3", recall=1.0, gold_ids=("p:3",), cited_ids=("p:3",)),
            ),
        ),
        proposals=(_proposal(predictions=predictions, briefs=briefs),),
        findings=(),
    )


def _increase(targets: tuple[str, ...] = ("c1",)) -> Prediction:
    return Prediction(description="targets recover", target_cases=targets, metric="recall@5", direction="increase")


def _no_regression(targets: tuple[str, ...] = ("c3",)) -> Prediction:
    return Prediction(description="others hold", target_cases=targets, metric="recall@5", direction="no_regression")


class TestRetainedDroppedPartition:
    def test_a_case_whose_gold_page_vanished_is_dropped(self):
        outcome = verify_recommendation(
            _recommendation(predictions=[_increase()]),
            current_recalls={"c1": 1.0, "c2": 0.5, "c3": 1.0},
            existing_pages={"p:1", "p:2"},
            current_stamp=_stamp(),
            changed_paths=(),
        )

        assert sorted(delta.case_name for delta in outcome.retained) == ["c1", "c2"]
        assert outcome.dropped == ("c3",)

    def test_a_case_missing_from_the_rerun_is_dropped(self):
        outcome = verify_recommendation(
            _recommendation(predictions=[_increase()]),
            current_recalls={"c1": 1.0, "c3": 1.0},
            existing_pages={"p:1", "p:2", "p:3"},
            current_stamp=_stamp(),
            changed_paths=(),
        )

        assert sorted(delta.case_name for delta in outcome.retained) == ["c1", "c3"]
        assert outcome.dropped == ("c2",)

    def test_targeted_and_untargeted_deltas_are_partitioned(self):
        outcome = verify_recommendation(
            _recommendation(predictions=[_increase(("c1",))]),
            current_recalls={"c1": 1.0, "c2": 0.5, "c3": 0.5},
            existing_pages={"p:1", "p:2", "p:3"},
            current_stamp=_stamp(),
            changed_paths=(),
        )

        by_name = {delta.case_name: delta for delta in outcome.retained}
        assert by_name["c1"].targeted is True
        assert by_name["c2"].targeted is False
        assert outcome.targeted_mean_delta == 1.0
        assert outcome.untargeted_mean_delta == -0.25


class TestPredictionVerdicts:
    def test_an_increase_prediction_holds_on_a_positive_mean_delta(self):
        outcome = verify_recommendation(
            _recommendation(predictions=[_increase(("c1", "c2"))]),
            current_recalls={"c1": 1.0, "c2": 0.5, "c3": 1.0},
            existing_pages={"p:1", "p:2", "p:3"},
            current_stamp=_stamp(),
            changed_paths=(),
        )

        (verdict,) = outcome.verdicts
        assert verdict.holds is True
        assert verdict.retained_targets == ("c1", "c2")

    def test_an_increase_prediction_fails_on_a_flat_delta(self):
        outcome = verify_recommendation(
            _recommendation(predictions=[_increase(("c1",))]),
            current_recalls={"c1": 0.0, "c2": 0.5, "c3": 1.0},
            existing_pages={"p:1", "p:2", "p:3"},
            current_stamp=_stamp(),
            changed_paths=(),
        )

        (verdict,) = outcome.verdicts
        assert verdict.holds is False

    def test_a_no_regression_prediction_fails_when_a_target_regresses(self):
        outcome = verify_recommendation(
            _recommendation(predictions=[_no_regression(("c3",))]),
            current_recalls={"c1": 0.0, "c2": 0.5, "c3": 0.5},
            existing_pages={"p:1", "p:2", "p:3"},
            current_stamp=_stamp(),
            changed_paths=(),
        )

        (verdict,) = outcome.verdicts
        assert verdict.holds is False

    def test_a_prediction_with_no_retained_targets_is_unmeasurable(self):
        outcome = verify_recommendation(
            _recommendation(predictions=[_increase(("c3",))]),
            current_recalls={"c1": 0.0, "c2": 0.5},
            existing_pages={"p:1", "p:2"},
            current_stamp=_stamp(),
            changed_paths=(),
        )

        (verdict,) = outcome.verdicts
        assert verdict.holds is None
        assert verdict.retained_targets == ()


class TestDriftAttribution:
    def test_unchanged_knowledge_base_reports_no_drift(self):
        outcome = verify_recommendation(
            _recommendation(predictions=[_increase()]),
            current_recalls={"c1": 0.0, "c2": 0.5, "c3": 1.0},
            existing_pages={"p:1", "p:2", "p:3"},
            current_stamp=_stamp(),
            changed_paths=(),
        )

        assert outcome.drift.knowledge_base_changed is False
        assert outcome.drift.warning is None

    def test_content_change_fully_covered_by_the_briefs_is_attributed(self):
        outcome = verify_recommendation(
            _recommendation(predictions=[_increase()], briefs=[PageWorkOrder(page_id="p:1", note_path="Memory/Papers/a.md", action="split")]),
            current_recalls={"c1": 1.0, "c2": 0.5, "c3": 1.0},
            existing_pages={"p:1", "p:2", "p:3"},
            current_stamp=_stamp(manifest_digest="manifest-2"),
            changed_paths=("Memory/Papers/a.md",),
        )

        assert outcome.drift.content_changed is True
        assert outcome.drift.unattributed_paths == ()
        assert outcome.drift.warning is None

    def test_content_change_outside_the_briefs_raises_the_unattributed_warning(self):
        outcome = verify_recommendation(
            _recommendation(predictions=[_increase()], briefs=[PageWorkOrder(page_id="p:1", note_path="Memory/Papers/a.md", action="split")]),
            current_recalls={"c1": 1.0, "c2": 0.5, "c3": 1.0},
            existing_pages={"p:1", "p:2", "p:3"},
            current_stamp=_stamp(manifest_digest="manifest-2"),
            changed_paths=("Memory/Papers/a.md", "Memory/Rogue/edit.md"),
        )

        assert outcome.drift.unattributed_paths == ("Memory/Rogue/edit.md",)
        assert outcome.drift.warning is not None
        assert "unattributed" in outcome.drift.warning

    def test_content_change_with_no_newer_manifest_rows_warns_about_attribution(self):
        outcome = verify_recommendation(
            _recommendation(predictions=[_increase()]),
            current_recalls={"c1": 1.0, "c2": 0.5, "c3": 1.0},
            existing_pages={"p:1", "p:2", "p:3"},
            current_stamp=_stamp(manifest_digest="manifest-2"),
            changed_paths=(),
        )

        assert outcome.drift.warning is not None
        assert "attribution" in outcome.drift.warning

    def test_a_spec_only_change_is_flagged_but_not_a_content_warning(self):
        outcome = verify_recommendation(
            _recommendation(predictions=[_increase()]),
            current_recalls={"c1": 0.0, "c2": 0.5, "c3": 1.0},
            existing_pages={"p:1", "p:2", "p:3"},
            current_stamp=_stamp(spec_hash="spec-2"),
            changed_paths=(),
        )

        assert outcome.drift.spec_changed is True
        assert outcome.drift.content_changed is False
        assert outcome.drift.warning is None


class TestRenderVerifyMarkdown:
    def test_renders_the_sections_verdicts_and_drift(self):
        recommendation = _recommendation(predictions=[_increase(("c1",)), _no_regression(("c3",))])
        outcome = verify_recommendation(
            recommendation,
            current_recalls={"c1": 1.0, "c2": 0.5, "c3": 0.5},
            existing_pages={"p:1", "p:2", "p:3"},
            current_stamp=_stamp(manifest_digest="manifest-2"),
            changed_paths=("Memory/Rogue/edit.md",),
        )

        rendered = render_verify_markdown(recommendation, outcome, verified_at=datetime(2026, 7, 4, tzinfo=UTC))

        assert "## Knowledge base drift" in rendered
        assert "## Case partition" in rendered
        assert "## Prediction verdicts" in rendered
        assert "## Deltas" in rendered
        assert "HOLDS" in rendered
        assert "FAILED" in rendered
        assert "Memory/Rogue/edit.md" in rendered
        assert rendered == render_verify_markdown(recommendation, outcome, verified_at=datetime(2026, 7, 4, tzinfo=UTC))

    def test_a_policy_note_is_rendered_when_supplied(self):
        recommendation = _recommendation(predictions=[_increase(("c1",))])
        outcome = verify_recommendation(
            recommendation,
            current_recalls={"c1": 1.0, "c2": 0.5, "c3": 1.0},
            existing_pages={"p:1", "p:2", "p:3"},
            current_stamp=_stamp(),
            changed_paths=(),
        )

        rendered = render_verify_markdown(
            recommendation, outcome, verified_at=datetime(2026, 7, 4, tzinfo=UTC), policy_note="re-ran under the shipped baseline"
        )

        assert "re-ran under the shipped baseline" in rendered


# -- the handler, end-to-end over fakes ---------------------------------------


@dataclass(frozen=True)
class _Score:
    """A stand-in for one `EvaluationResult`, the reader reads only `.value`."""

    value: float


@dataclass(frozen=True)
class _Case:
    """A stand-in for one `ReportCase`, the slice the handler folds over plus the case name the port requires."""

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
    """A stand-in for an `EvaluationReport`, the handler reads only `.cases`."""

    cases: list[_Case]


class _FakeEvaluatorFactory:
    """A `PolicyEvaluatorFactory` whose evaluator returns the scripted report."""

    def __init__(self, report: _Report) -> None:
        self.report = report
        self.policies: list[RetrievalPolicy] = []

    def __call__(self, model: str | None) -> _FakeEvaluatorFactory:
        del model
        return self

    async def evaluate(self, policy: RetrievalPolicy, *, dataset: EvaluationDataset) -> _Report:
        del dataset
        self.policies.append(policy)
        return self.report


class _FakeRunRepository:
    """A `SupportsRunPersistence` recording each `add_run` with its populated case rows."""

    def __init__(self) -> None:
        self.add_run_calls: list[tuple[EvaluationRunSummary, tuple[EvaluationCaseResultRow, ...]]] = []

    async def add_run(self, run: EvaluationRunSummary, case_results: Sequence[EvaluationCaseResultRow]) -> None:
        self.add_run_calls.append((run, tuple(case_results)))


class _Factory[T]:
    """A one-item async-context-manager factory over a canned gateway or repository."""

    def __init__(self, item: T) -> None:
        self.item = item

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[T]:
        yield self.item


def _passing_case(question: str, gold: str) -> _Case:
    return _Case(
        name=gold,
        inputs=QaInput(question=question),
        metadata=QaCaseMeta(anchor_page_id=gold, difficulty="easy", archetype=Archetype.PINPOINT),
        expected_output=RetrievalAnswer(answer="ref", cited_source_ids=[gold]),
        output=RetrievalAnswer(answer="got", cited_source_ids=[gold]),
        scores={"recall@5": _Score(1.0), "n_tool_calls": _Score(2.0)},
        metrics={"input_tokens": 100.0},
        total_duration=0.2,
    )


def _write_dataset(tmp_path: Path) -> Path:
    cases = [
        AuthoredCase(
            question=QaInput(question=f"q-{gold}"),
            expected=ExpectedQa(answer="a", expected_source_ids=(gold,)),
            meta=QaCaseMeta(anchor_page_id=gold, difficulty="easy", archetype=Archetype.PINPOINT),
        )
        for gold in ("paper.a", "paper.b")
    ]
    path = tmp_path / "golden.yaml"
    save_dataset(build_dataset(cases), path)
    return path


def _handler_recommendation(dataset_path: Path) -> Recommendation:
    """A recommendation whose two failing baseline cases match the dataset's names, with one increase prediction."""
    return Recommendation(
        collection="research",
        created_at=datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
        baseline=BaselineSnapshot(
            dataset=str(dataset_path),
            run_id="run-0",
            policy_source="default",
            evidence_grade="raw-baseline",
            knowledge_base=KnowledgeBaseStamp(collection="research", spec_hash="spec-1", manifest_digest="manifest-1", n_pages=2),
            overall={"recall@5": 0.0},
            cases=(
                CaseSnapshot(case_name="paper.a-0", recall=0.0, gold_ids=("paper.a",), cited_ids=()),
                CaseSnapshot(case_name="paper.b-1", recall=0.0, gold_ids=("paper.b",), cited_ids=()),
            ),
        ),
        proposals=(
            _proposal(
                predictions=[Prediction(description="a recovers", target_cases=("paper.a-0",), metric="recall@5", direction="increase")],
                briefs=[PageWorkOrder(page_id="paper.a", note_path="Memory/paper/a.md", action="split findings")],
            ),
        ),
        findings=(),
    )


class TestVerifyRecommendationHandler:
    async def test_end_to_end_writes_the_report_and_persists_the_run(self, tmp_path: Path):
        dataset_path = _write_dataset(tmp_path)
        recommendation_path = tmp_path / "research.yaml"
        save_recommendation(_handler_recommendation(dataset_path), recommendation_path)
        gateway = FakeKnowledgeBaseGateway(
            page_types_result={"paper.a": "paper", "paper.b": "paper"},
            stamp_result=KnowledgeBaseStamp(collection="research", spec_hash="spec-1", manifest_digest="manifest-2", n_pages=2),
            changed_paths_result=("Memory/paper/a.md",),
        )
        repo = _FakeRunRepository()
        knowledge_base = tmp_path / "knowledge_base"
        knowledge_base.mkdir()
        handler = VerifyRecommendationHandler(
            _FakeEvaluatorFactory(_Report(cases=[_passing_case("q-paper.a", "paper.a"), _passing_case("q-paper.b", "paper.b")])),
            _Factory(gateway),
            _Factory(repo),
            knowledge_base,
            clock=iter([0.0, 1.5]).__next__,
            now=lambda: datetime(2026, 7, 4, tzinfo=UTC),
            new_run_id=lambda: "verify-run-1",
        )

        from engram.evals.commands import VerifyRecommendation

        report = await handler.handle(VerifyRecommendation(recommendation=str(recommendation_path)))

        assert report.n_retained == 2
        assert report.n_dropped == 0
        assert report.predictions_held == 1
        assert report.predictions_failed == 0
        assert report.drift_warning is None
        assert report.run_id == "verify-run-1"
        assert report.duration_seconds == 1.5
        out = recommendation_path.with_name("research.verify.md")
        assert report.out == str(out)
        rendered = out.read_text(encoding="utf-8")
        assert "HOLDS" in rendered
        (run, rows) = repo.add_run_calls[0]
        assert run.variant["recommendation_verify"] is True
        assert len(rows) == 2
        assert gateway.changed_paths_calls[0].since == datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
        assert [event.__class__.__name__ for event in handler.events] == ["VerificationStarted", "VerificationCompleted"]

    async def test_a_vanished_gold_page_drops_its_case_and_unattributed_drift_warns(self, tmp_path: Path):
        dataset_path = _write_dataset(tmp_path)
        recommendation_path = tmp_path / "research.yaml"
        save_recommendation(_handler_recommendation(dataset_path), recommendation_path)
        gateway = FakeKnowledgeBaseGateway(
            page_types_result={"paper.a": "paper"},
            stamp_result=KnowledgeBaseStamp(collection="research", spec_hash="spec-1", manifest_digest="manifest-2", n_pages=1),
            changed_paths_result=("Memory/rogue/edit.md",),
        )
        knowledge_base = tmp_path / "knowledge_base"
        knowledge_base.mkdir()
        handler = VerifyRecommendationHandler(
            _FakeEvaluatorFactory(_Report(cases=[_passing_case("q-paper.a", "paper.a"), _passing_case("q-paper.b", "paper.b")])),
            _Factory(gateway),
            _Factory(_FakeRunRepository()),
            knowledge_base,
            now=lambda: datetime(2026, 7, 4, tzinfo=UTC),
        )

        from engram.evals.commands import VerifyRecommendation

        report = await handler.handle(VerifyRecommendation(recommendation=str(recommendation_path)))

        assert report.n_retained == 1
        assert report.n_dropped == 1
        assert report.drift_warning is not None
        assert "unattributed" in report.drift_warning


class TestVerifyRecommendationCli:
    def test_verify_recommendation_help_lists_the_flags(self):
        result = runner.invoke(app, ["evals", "verify-recommendation", "--help"])

        assert result.exit_code == 0
        for flag in ("--recommendation", "--model", "--dataset"):
            assert flag in result.stdout
