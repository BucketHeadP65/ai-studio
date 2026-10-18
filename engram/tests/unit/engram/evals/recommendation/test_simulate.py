"""Unit specs for the shadow mechanical-split simulation (`recommendation/service.py`).

Two layers under test. `split_section_body` is the pure mechanical split, greedy paragraph packing
under a token budget with fence-aware paragraph detection. `simulate_restructures` is the
orchestrator: it pairs each restructure proposal's failing cases with the over-budget sections of
their own gold pages, ranks the whole body against its parts through the injected gateway
(`FakeKnowledgeBaseGateway`, scripted, network-free), and returns a new recommendation whose restructure
proposals carry a `SimulationResult`. Non-restructure proposals and unmeasurable ones pass through
untouched, and the ranking queries must carry the case's original question text (the diagnosis is
remapped to dataset case names before the simulation runs).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from core.markdown import split_section_body

from engram.evals.rca.contracts import Diagnosis, FailingCase
from engram.evals.recommendation.contracts import (
    BaselineSnapshot,
    KnowledgeBaseStamp,
    OversizedSection,
    SectionStat,
    StructuralDiagnosis,
    simulate_restructures,
)
from engram.evals.recommendation.domain.detectors import SectionSplitSignal
from engram.evals.recommendation.domain.recommendation import (
    AddRelationOp,
    ProposalEvidence,
    Recommendation,
    RestructureSectionsOp,
    SchemaEditProposal,
)
from engram.evals.shared.domain import Archetype
from engram.indexing.contracts import token_count
from engram.kb.contracts import FrontmatterFieldSpec, RelationDecl
from engram.testing.fakes.recommendation import FakeKnowledgeBaseGateway

if TYPE_CHECKING:
    from engram.evals.recommendation.domain.recommendation import SchemaEditOp

# Two paragraphs that cannot share one part under the budgets the specs use.
_TWO_PARAGRAPHS = "alpha beta gamma delta epsilon zeta\n\ntheta iota kappa lambda mu nu"


def _restructure_proposal(proposal_id: str = "restructure-paper-findings", kind: str = "paper") -> SchemaEditProposal:
    """A gated restructure proposal with empty measured evidence, the simulation's input."""
    return _proposal(proposal_id, RestructureSectionsOp(page_type=kind, canonical_sections=("summary", "findings")))


def _add_relation_proposal() -> SchemaEditProposal:
    """A gated add-relation proposal, which the simulation must pass through untouched."""
    op = AddRelationOp(
        relation=RelationDecl(slug="extends", domain="paper", range="paper"),
        on_page_type="paper",
        edge_field=FrontmatterFieldSpec(name="extends", type="link-list"),
    )
    return _proposal("add-relation-extends", op)


def _proposal(proposal_id: str, op: SchemaEditOp) -> SchemaEditProposal:
    """A minimal gated proposal around `op` (no counterfactual, no mined links, no simulation)."""
    evidence = ProposalEvidence(case_names=("case-a",), recall_deficit=0.4, counterfactual=None, mined_links=(), oversized_sections=())
    return SchemaEditProposal(id=proposal_id, op=op, evidence=evidence, predictions=(), migration=(), rationale="r")


def _recommendation(*proposals: SchemaEditProposal) -> Recommendation:
    """A minimal recommendation carrying `proposals` over an empty baseline."""
    baseline = BaselineSnapshot(
        dataset="golden.yaml",
        run_id=None,
        policy_source="default",
        evidence_grade="raw-baseline",
        knowledge_base=KnowledgeBaseStamp(collection="research", spec_hash="", manifest_digest="", n_pages=0),
        overall={"recall@5": 0.5},
        cases=(),
    )
    return Recommendation(
        collection="research", created_at=datetime(2026, 7, 3, tzinfo=UTC), baseline=baseline, proposals=proposals, findings=()
    )


def _diagnosis(
    *, kind: str = "paper", case_names: tuple[str, ...] = ("case-a",), gold_by_case: dict[str, tuple[str, ...]] | None = None
) -> StructuralDiagnosis:
    """A diagnosis with one section-split signal for `kind` and one failing case per `gold_by_case` row."""
    gold = gold_by_case if gold_by_case is not None else {"case-a": ("paper.a",)}
    worst = tuple(
        FailingCase(
            question=name, gold_ids=gold_ids, cited_ids=(), recall=0.0, n_tool_calls=1.0, archetype=Archetype.PINPOINT, difficulty="easy"
        )
        for name, gold_ids in gold.items()
    )
    base = Diagnosis(overall={"recall@5": 0.5}, buckets=(), worst_cases=worst, mean_duration_s=0.0, mean_total_tokens=None)
    signal = SectionSplitSignal(
        page_type=kind,
        case_names=case_names,
        offenders=tuple(SectionStat(page_id=page, canon="findings", tokens=900) for ids in gold.values() for page in ids),
    )
    return StructuralDiagnosis(base=base, evidence_grade="raw-baseline", section_splits=(signal,), missing_edges=(), over_expansion=None)


def _section(page_id: str, body: str = _TWO_PARAGRAPHS) -> OversizedSection:
    """An over-budget section on `page_id` whose body splits into two parts under a small budget."""
    return OversizedSection(page_id=page_id, canon="findings", tokens=900, body=body)


class TestSplitSectionBody:
    def test_splits_two_paragraphs_that_cannot_share_the_budget(self):
        parts = split_section_body(_TWO_PARAGRAPHS, token_budget=8, count_tokens=token_count)

        assert len(parts) == 2
        assert parts[0].startswith("alpha")
        assert parts[1].startswith("theta")

    def test_packs_everything_into_one_part_when_the_budget_allows(self):
        parts = split_section_body(_TWO_PARAGRAPHS, token_budget=10_000, count_tokens=token_count)

        assert len(parts) == 1

    def test_a_blank_line_inside_a_code_fence_never_splits(self):
        body = "intro paragraph\n\n```python\nfirst = 1\n\nsecond = 2\n```"

        parts = split_section_body(body, token_budget=8, count_tokens=token_count)

        assert len(parts) == 2
        assert "first = 1\n\nsecond = 2" in parts[1]

    def test_a_single_paragraph_over_budget_becomes_its_own_part(self):
        body = "one two three four five six seven eight nine ten\n\nshort"

        parts = split_section_body(body, token_budget=3, count_tokens=token_count)

        assert len(parts) == 2

    def test_a_body_without_blank_lines_comes_back_whole(self):
        parts = split_section_body("a single block of text", token_budget=2, count_tokens=token_count)

        assert parts == ("a single block of text",)


class TestSimulateRestructures:
    async def test_enriches_a_restructure_proposal_with_the_measured_ranks(self):
        gateway = FakeKnowledgeBaseGateway(oversized_section_bodies_result=(_section("paper.a"),), snippet_ranks_result=(9, 3, 5))
        recommendation = _recommendation(_restructure_proposal())

        result = await simulate_restructures(
            recommendation, _diagnosis(), gateway, question_by_name={"case-a": "what did paper a find"}, token_budget=8, collection="research"
        )

        simulation = result.proposals[0].evidence.simulation
        assert simulation is not None
        assert simulation.n_measured == 1
        assert simulation.n_unsplittable == 0
        assert simulation.splits[0].rank_before == 9
        assert simulation.splits[0].rank_after == 3
        assert simulation.splits[0].n_parts == 2
        assert gateway.snippet_ranks_calls[0].query == "what did paper a find"
        assert gateway.snippet_ranks_calls[0].snippets[0] == _TWO_PARAGRAPHS

    async def test_pairs_a_case_only_with_its_own_gold_pages(self):
        # The signal covers two cases, but paper.b's section must only be ranked for case-b's query.
        gateway = FakeKnowledgeBaseGateway(
            oversized_section_bodies_result=(_section("paper.a"), _section("paper.b")), snippet_ranks_result=(9, 3, 5)
        )
        diagnosis = _diagnosis(case_names=("case-a", "case-b"), gold_by_case={"case-a": ("paper.a",), "case-b": ("paper.b",)})
        recommendation = _recommendation(_restructure_proposal())

        result = await simulate_restructures(
            recommendation, diagnosis, gateway, question_by_name={"case-a": "q-a", "case-b": "q-b"}, token_budget=8, collection="research"
        )

        simulation = result.proposals[0].evidence.simulation
        assert simulation is not None
        assert simulation.n_measured == 2
        assert {(split.case_name, split.page_id) for split in simulation.splits} == {("case-a", "paper.a"), ("case-b", "paper.b")}

    async def test_an_unsplittable_body_is_counted_and_never_ranked(self):
        gateway = FakeKnowledgeBaseGateway(oversized_section_bodies_result=(_section("paper.a", body="one solid block"),))
        recommendation = _recommendation(_restructure_proposal())

        result = await simulate_restructures(
            recommendation, _diagnosis(), gateway, question_by_name={"case-a": "q-a"}, token_budget=8, collection="research"
        )

        simulation = result.proposals[0].evidence.simulation
        assert simulation is not None
        assert simulation.n_measured == 0
        assert simulation.n_unsplittable == 1
        assert simulation.mean_rank_before == pytest.approx(0.0)
        assert gateway.snippet_ranks_calls == []

    async def test_a_non_restructure_proposal_passes_through_untouched(self):
        gateway = FakeKnowledgeBaseGateway()
        recommendation = _recommendation(_add_relation_proposal())

        result = await simulate_restructures(
            recommendation, _diagnosis(), gateway, question_by_name={"case-a": "q-a"}, token_budget=8, collection="research"
        )

        assert result.proposals[0] is recommendation.proposals[0]
        assert gateway.oversized_section_bodies_calls == []

    async def test_a_restructure_without_a_matching_signal_passes_through_untouched(self):
        gateway = FakeKnowledgeBaseGateway()
        recommendation = _recommendation(_restructure_proposal(kind="method"))

        result = await simulate_restructures(
            recommendation, _diagnosis(kind="paper"), gateway, question_by_name={"case-a": "q-a"}, token_budget=8, collection="research"
        )

        assert result.proposals[0].evidence.simulation is None
        assert gateway.oversized_section_bodies_calls == []

    async def test_an_unknown_case_name_is_skipped(self):
        gateway = FakeKnowledgeBaseGateway(oversized_section_bodies_result=(_section("paper.a"),))
        recommendation = _recommendation(_restructure_proposal())

        result = await simulate_restructures(recommendation, _diagnosis(), gateway, question_by_name={}, token_budget=8, collection="research")

        assert result.proposals[0].evidence.simulation is None
        assert gateway.snippet_ranks_calls == []
