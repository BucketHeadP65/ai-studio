"""Unit specs for the recommendation gate (`engram.evals.recommendation.domain.gate`).

These specs pin the pure post-LLM gate's contract: it drops draft proposals whose op cannot be
applied to the spec, composes each survivor's evidence mechanically from the `StructuralDiagnosis`
(never from the LLM's own claim), enforces the evidence thresholds, ranks by `recall_deficit`
descending and stable on ties, dedupes identical ops, and passes the findings tail through untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from engram.evals.rca.contracts import Diagnosis, FailingCase
from engram.evals.rca.domain.diagnosis import BucketMetrics
from engram.evals.recommendation.contracts import (
    BaselineSnapshot,
    DraftRecommendation,
    KnowledgeBaseStamp,
    MinedLink,
    SectionStat,
    StructuralDiagnosis,
    gate_recommendation,
)
from engram.evals.recommendation.domain.detectors import MissingEdgeSignal, SectionSplitSignal
from engram.evals.recommendation.domain.recommendation import (
    AddRelationOp,
    CounterfactualResult,
    DraftProposal,
    PageWorkOrder,
    RestructureSectionsOp,
    StructuralFinding,
)
from engram.evals.shared.domain import Archetype
from engram.kb.contracts import CanonicalSection, FrontmatterFieldSpec, KnowledgeBaseSchema, PageTypeSpec, RelationDecl

if TYPE_CHECKING:
    from engram.evals.shared.domain import QaDifficulty

# -- fixtures ----------------------------------------------------------------


def _spec(kinds: tuple[str, ...] = ("paper",)) -> KnowledgeBaseSchema:
    """A valid spec whose `paper` type carries a link-list `cites` edge and matching relation."""
    page_types = tuple(
        PageTypeSpec(
            page_type=kind,
            id_prefix=f"{kind[0]}{index}",
            canonical_sections=(CanonicalSection(slug="summary"), CanonicalSection(slug="findings")),
            frontmatter_fields=(FrontmatterFieldSpec(name="cites", type="link-list"),) if kind == "paper" else (),
            edge_fields=("cites",) if kind == "paper" else (),
        )
        for index, kind in enumerate(kinds)
    )
    relations = (RelationDecl(slug="cites", domain="paper", range="paper"),) if "paper" in kinds else ()
    return KnowledgeBaseSchema(slug="test", page_types=page_types, relations=relations)


def _failing_case(question: str, *, archetype: Archetype | None = None, difficulty: QaDifficulty = "medium") -> FailingCase:
    """A failing case carrying only the identifier and bucket labels the gate reads."""
    return FailingCase(
        question=question, gold_ids=(), cited_ids=(), recall=0.0, n_tool_calls=0.0, archetype=archetype, difficulty=difficulty
    )


def _bucket(archetype: Archetype | None, difficulty: QaDifficulty, recall: float) -> BucketMetrics:
    """A single-metric bucket carrying only the `recall@5` mean the deficit is computed from."""
    return BucketMetrics(archetype=archetype, difficulty=difficulty, n_cases=1, metrics={"recall@5": recall})


def _diagnosis(
    *,
    section_splits: tuple[SectionSplitSignal, ...] = (),
    missing_edges: tuple[MissingEdgeSignal, ...] = (),
    worst_cases: tuple[FailingCase, ...] = (),
    buckets: tuple[BucketMetrics, ...] = (),
    overall: dict[str, float] | None = None,
) -> StructuralDiagnosis:
    """A hand-built structural diagnosis with the signal groups and recall context the gate reads."""
    base = Diagnosis(
        overall=overall if overall is not None else {"recall@5": 0.6},
        buckets=buckets,
        worst_cases=worst_cases,
        mean_duration_s=0.0,
        mean_total_tokens=None,
    )
    return StructuralDiagnosis(
        base=base, evidence_grade="raw-baseline", section_splits=section_splits, missing_edges=missing_edges, over_expansion=None
    )


def _section_signal(kind: str, case_names: tuple[str, ...], offenders: tuple[SectionStat, ...] = ()) -> SectionSplitSignal:
    """A section-split signal for `kind` with authoritative case names and over-budget offenders."""
    return SectionSplitSignal(page_type=kind, case_names=case_names, offenders=offenders)


def _missing_edge_signal(
    case_names: tuple[str, ...], *, mined: tuple[MinedLink, ...] = (), counterfactual: CounterfactualResult | None = None
) -> MissingEdgeSignal:
    """A missing-edge signal carrying authoritative case names, mined candidates, and a counterfactual."""
    return MissingEdgeSignal(case_names=case_names, unreachable_pairs=(), mined_candidates=mined, counterfactual=counterfactual)


def _restructure_draft(
    proposal_id: str, kind: str, *, case_claim: tuple[str, ...] = ("claim",), migration: tuple[PageWorkOrder, ...] | None = None
) -> DraftProposal:
    """A draft restructure proposal, migration defaulting to one work order unless overridden."""
    return DraftProposal(
        id=proposal_id,
        op=RestructureSectionsOp(page_type=kind, canonical_sections=("intro", "methods", "results")),
        case_names=case_claim,
        rationale="r",
        predictions=(),
        migration=migration if migration is not None else (PageWorkOrder(page_id="x", note_path="p.md", action="split"),),
    )


def _add_relation_draft(
    proposal_id: str, *, slug: str = "extends", domain: str = "paper", target: str = "paper", case_claim: tuple[str, ...] = ("claim",)
) -> DraftProposal:
    """A draft add-relation proposal for a new relation placed on the `domain` page type."""
    return DraftProposal(
        id=proposal_id,
        op=AddRelationOp(
            relation=RelationDecl(slug=slug, domain=domain, range=target),
            on_page_type=domain,
            edge_field=FrontmatterFieldSpec(name=slug, type="link-list"),
        ),
        case_names=case_claim,
        rationale="r",
        predictions=(),
        migration=(),
    )


def _draft(
    proposals: tuple[DraftProposal, ...] = (), findings: tuple[StructuralFinding, ...] = (), rationale: str = "overall"
) -> DraftRecommendation:
    """A draft recommendation wrapping the given proposals and findings tail."""
    return DraftRecommendation(proposals=proposals, findings=findings, rationale=rationale)


def _baseline() -> BaselineSnapshot:
    """A minimal baseline snapshot the gate stamps onto the recommendation verbatim."""
    return BaselineSnapshot(
        dataset="ds",
        run_id=None,
        policy_source="default",
        evidence_grade="raw-baseline",
        knowledge_base=KnowledgeBaseStamp(collection="research", spec_hash="h", manifest_digest="d", n_pages=1),
        overall={"recall@5": 0.6},
        cases=(),
    )


def _gate(draft: DraftRecommendation, diagnosis: StructuralDiagnosis, spec: KnowledgeBaseSchema, *, min_cases: int = 1, max_proposals: int = 5):
    """Invoke the gate with the fixed baseline, collection, and clock the handler would supply."""
    return gate_recommendation(
        draft,
        diagnosis,
        spec,
        baseline=_baseline(),
        collection="research",
        created_at=datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
        min_cases_per_proposal=min_cases,
        max_proposals=max_proposals,
    )


# -- op re-validation --------------------------------------------------------


class TestOpRevalidation:
    def test_rejects_an_op_that_fails_apply_op_for_an_unknown_kind(self):
        spec = _spec()
        draft = _draft(proposals=(_restructure_draft("p1", "ghost"),))
        diagnosis = _diagnosis(
            section_splits=(_section_signal("ghost", ("q1", "q2")),), worst_cases=(_failing_case("q1"), _failing_case("q2"))
        )

        result = _gate(draft, diagnosis, spec)

        assert result.proposals == ()

    def test_rejects_an_op_the_spec_invariants_reject_for_a_duplicate_relation_slug(self):
        spec = _spec()  # already declares a "cites" relation
        draft = _draft(proposals=(_add_relation_draft("p1", slug="cites"),))
        diagnosis = _diagnosis(
            missing_edges=(_missing_edge_signal(("q1", "q2")),),
            worst_cases=(_failing_case("q1", archetype=Archetype.TRACE), _failing_case("q2", archetype=Archetype.TRACE)),
        )

        result = _gate(draft, diagnosis, spec)

        assert result.proposals == ()


# -- evidence composition ----------------------------------------------------


class TestEvidenceComposition:
    def test_evidence_case_names_come_from_the_signal_not_the_draft_claim(self):
        spec = _spec()
        draft = _draft(proposals=(_add_relation_draft("p1", slug="extends", case_claim=("bogus",)),))
        diagnosis = _diagnosis(
            missing_edges=(
                _missing_edge_signal(
                    ("real-q1", "real-q2"),
                    mined=(
                        MinedLink(src_page_id="p:1", src_type="paper", raw_target="X", dst_page_id="p:2", dst_type="paper", rel="extends"),
                        MinedLink(src_page_id="p:9", src_type="note", raw_target="Y", dst_page_id="p:8", dst_type="note", rel="mentions"),
                    ),
                    counterfactual=CounterfactualResult(n_pairs=1, reachable_before=0, reachable_after=1, max_hops=3, candidate_edges=()),
                ),
            ),
            worst_cases=(_failing_case("real-q1", archetype=Archetype.TRACE), _failing_case("real-q2", archetype=Archetype.TRACE)),
        )

        result = _gate(draft, diagnosis, spec)

        assert len(result.proposals) == 1
        evidence = result.proposals[0].evidence
        assert evidence.case_names == ("real-q1", "real-q2")
        assert "bogus" not in evidence.case_names
        assert tuple(link.rel for link in evidence.mined_links) == ("extends",)  # note->note dropped by domain/range filter
        assert evidence.counterfactual is not None

    def test_restructure_evidence_formats_oversized_sections_and_carries_no_graph_evidence(self):
        spec = _spec()
        diagnosis = _diagnosis(
            section_splits=(
                _section_signal(
                    "paper",
                    ("q1", "q2"),
                    offenders=(
                        SectionStat(page_id="p:1", canon="findings", tokens=900),
                        SectionStat(page_id="p:2", canon="summary", tokens=700),
                    ),
                ),
            ),
            worst_cases=(_failing_case("q1"), _failing_case("q2")),
        )

        result = _gate(_draft(proposals=(_restructure_draft("p1", "paper"),)), diagnosis, spec)

        evidence = result.proposals[0].evidence
        assert evidence.oversized_sections == ("p:1#findings (900)", "p:2#summary (700)")
        assert evidence.counterfactual is None
        assert evidence.mined_links == ()

    def test_a_proposal_with_no_matching_signal_gets_no_evidence_and_is_dropped(self):
        spec = _spec()
        diagnosis = _diagnosis(
            section_splits=(_section_signal("note", ("q1", "q2")),), worst_cases=(_failing_case("q1"), _failing_case("q2"))
        )

        result = _gate(_draft(proposals=(_restructure_draft("p1", "paper"),)), diagnosis, spec)

        assert result.proposals == ()


# -- thresholds --------------------------------------------------------------


class TestThresholds:
    def test_drops_a_proposal_below_min_cases_per_proposal(self):
        spec = _spec()
        diagnosis = _diagnosis(section_splits=(_section_signal("paper", ("q1",)),), worst_cases=(_failing_case("q1"),))

        result = _gate(_draft(proposals=(_restructure_draft("p1", "paper"),)), diagnosis, spec, min_cases=2)

        assert result.proposals == ()

    def test_keeps_a_proposal_meeting_min_cases_per_proposal(self):
        spec = _spec()
        diagnosis = _diagnosis(
            section_splits=(_section_signal("paper", ("q1", "q2")),), worst_cases=(_failing_case("q1"), _failing_case("q2"))
        )

        result = _gate(_draft(proposals=(_restructure_draft("p1", "paper"),)), diagnosis, spec, min_cases=2)

        assert len(result.proposals) == 1
        assert result.proposals[0].evidence.case_names == ("q1", "q2")

    def test_truncates_to_max_proposals_keeping_the_strongest(self):
        spec = _spec(("paper", "note", "topic"))
        drafts = (_restructure_draft("p-topic", "topic"), _restructure_draft("p-paper", "paper"), _restructure_draft("p-note", "note"))
        diagnosis = _diagnosis(
            section_splits=(
                _section_signal("topic", ("qt1", "qt2")),
                _section_signal("paper", ("qp1", "qp2")),
                _section_signal("note", ("qn1", "qn2")),
            ),
            worst_cases=(
                _failing_case("qt1", archetype=Archetype.SURVEY, difficulty="hard"),
                _failing_case("qt2", archetype=Archetype.SURVEY, difficulty="hard"),
                _failing_case("qp1", archetype=Archetype.PINPOINT, difficulty="easy"),
                _failing_case("qp2", archetype=Archetype.PINPOINT, difficulty="easy"),
                _failing_case("qn1", archetype=Archetype.PINPOINT, difficulty="easy"),
                _failing_case("qn2", archetype=Archetype.PINPOINT, difficulty="easy"),
            ),
            buckets=(_bucket(Archetype.SURVEY, "hard", 0.0), _bucket(Archetype.PINPOINT, "easy", 0.5)),
        )

        result = _gate(_draft(proposals=drafts), diagnosis, spec, min_cases=2, max_proposals=2)

        assert tuple(p.id for p in result.proposals) == ("p-topic", "p-paper")


# -- ranking and dedupe ------------------------------------------------------


class TestRankingAndDedupe:
    def test_ranks_by_recall_deficit_descending_stable_on_ties(self):
        spec = _spec(("paper", "note", "topic"))
        drafts = (_restructure_draft("p-paper", "paper"), _restructure_draft("p-note", "note"), _restructure_draft("p-topic", "topic"))
        diagnosis = _diagnosis(
            section_splits=(
                _section_signal("paper", ("qp1", "qp2")),
                _section_signal("note", ("qn1", "qn2")),
                _section_signal("topic", ("qt1", "qt2")),
            ),
            worst_cases=(
                _failing_case("qp1", archetype=Archetype.PINPOINT, difficulty="easy"),
                _failing_case("qp2", archetype=Archetype.PINPOINT, difficulty="easy"),
                _failing_case("qn1", archetype=Archetype.PINPOINT, difficulty="easy"),
                _failing_case("qn2", archetype=Archetype.PINPOINT, difficulty="easy"),
                _failing_case("qt1", archetype=Archetype.SURVEY, difficulty="hard"),
                _failing_case("qt2", archetype=Archetype.SURVEY, difficulty="hard"),
            ),
            buckets=(_bucket(Archetype.PINPOINT, "easy", 0.5), _bucket(Archetype.SURVEY, "hard", 0.0)),
        )

        result = _gate(_draft(proposals=drafts), diagnosis, spec, min_cases=2)

        assert tuple(p.id for p in result.proposals) == ("p-topic", "p-paper", "p-note")
        assert result.proposals[0].evidence.recall_deficit == pytest.approx(0.6)
        assert result.proposals[1].evidence.recall_deficit == pytest.approx(0.1)
        assert result.proposals[2].evidence.recall_deficit == pytest.approx(0.1)

    def test_dedupes_identical_op_payloads_keeping_the_first(self):
        spec = _spec()
        drafts = (_restructure_draft("p-a", "paper"), _restructure_draft("p-b", "paper"))
        diagnosis = _diagnosis(
            section_splits=(_section_signal("paper", ("q1", "q2")),), worst_cases=(_failing_case("q1"), _failing_case("q2"))
        )

        result = _gate(_draft(proposals=drafts), diagnosis, spec)

        assert len(result.proposals) == 1
        assert result.proposals[0].id == "p-a"


# -- migration invariant and findings tail -----------------------------------


class TestMigrationAndFindings:
    def test_drops_a_restructure_proposal_with_an_empty_migration_brief(self):
        spec = _spec()
        diagnosis = _diagnosis(
            section_splits=(_section_signal("paper", ("q1", "q2")),), worst_cases=(_failing_case("q1"), _failing_case("q2"))
        )

        result = _gate(_draft(proposals=(_restructure_draft("p1", "paper", migration=()),)), diagnosis, spec)

        assert result.proposals == ()

    def test_findings_tail_passes_through_untouched(self):
        spec = _spec()
        finding = StructuralFinding(finding_type="machinery", title="embedding grain", body="tune it", related_cases=("q1",))

        result = _gate(_draft(findings=(finding,)), _diagnosis(), spec)

        assert result.findings == (finding,)
        assert result.collection == "research"
