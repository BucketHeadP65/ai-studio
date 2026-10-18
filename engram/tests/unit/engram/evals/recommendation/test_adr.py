"""Unit specs for the recommendation draft-ADR renderer (`engram.evals.recommendation.domain.adr`).

These specs pin the renderer's contract: `render_draft_adr` produces a MADR-shaped draft ADR with
Context, Decision, and Consequences sections plus a findings tail, cites the measured evidence case
names verbatim, renders the proposals in the recommendation's own rank order, degrades gracefully when the
recommendation carries no proposals or findings, and is a pure deterministic function of its input.
"""

from __future__ import annotations

from datetime import UTC, datetime

from engram.evals.recommendation.contracts import (
    BaselineSnapshot,
    CandidateEdge,
    CaseSnapshot,
    KnowledgeBaseStamp,
    MinedLink,
    render_draft_adr,
)
from engram.evals.recommendation.domain.recommendation import (
    AddRelationOp,
    CounterfactualResult,
    PageWorkOrder,
    Prediction,
    ProposalEvidence,
    Recommendation,
    RestructureSectionsOp,
    SchemaEditProposal,
    SimulatedSplit,
    SimulationResult,
    StructuralFinding,
)
from engram.kb.contracts import FrontmatterFieldSpec, RelationDecl


def _knowledge_base() -> KnowledgeBaseStamp:
    return KnowledgeBaseStamp(collection="research", spec_hash="abc123", manifest_digest="def456", n_pages=42)


def _baseline() -> BaselineSnapshot:
    return BaselineSnapshot(
        dataset="research-ds",
        run_id="run-1",
        policy_source="default",
        evidence_grade="raw-baseline",
        knowledge_base=_knowledge_base(),
        overall={"recall@5": 0.5, "precision@5": 0.4},
        cases=(CaseSnapshot(case_name="baseline::case", recall=0.0, gold_ids=("p:1",), cited_ids=("p:2",)),),
    )


def _add_relation_proposal(pid: str = "add-relation-extends-on-paper", case_names: tuple[str, ...] = ("trace::alpha",)) -> SchemaEditProposal:
    return SchemaEditProposal(
        id=pid,
        op=AddRelationOp(
            relation=RelationDecl(slug="extends", domain="paper", range="paper"),
            on_page_type="paper",
            edge_field=FrontmatterFieldSpec(name="extends", type="link-list"),
        ),
        evidence=ProposalEvidence(
            case_names=case_names,
            recall_deficit=0.5,
            counterfactual=CounterfactualResult(
                n_pairs=2,
                reachable_before=0,
                reachable_after=2,
                max_hops=3,
                candidate_edges=(CandidateEdge(src_id="p:1", dst_id="p:2", rel="extends"),),
            ),
            mined_links=(
                MinedLink(src_page_id="p:1", src_type="paper", raw_target="Foo", dst_page_id="p:2", dst_type="paper", rel="extends"),
                MinedLink(src_page_id="p:3", src_type="paper", raw_target="Bar", dst_page_id=None, dst_type=None, rel="extends"),
            ),
            oversized_sections=(),
        ),
        predictions=(Prediction(description="alpha recovers", target_cases=case_names, metric="recall@5", direction="increase"),),
        migration=(PageWorkOrder(page_id="p:1", note_path="Memory/Papers/foo.md", action="add an extends link to p:2"),),
        rationale="the unlifted p:1 -> p:2 link explains the miss",
    )


def _restructure_proposal(pid: str = "restructure-sections-on-paper", case_names: tuple[str, ...] = ("survey::beta",)) -> SchemaEditProposal:
    return SchemaEditProposal(
        id=pid,
        op=RestructureSectionsOp(page_type="paper", canonical_sections=("intro", "methods", "results")),
        evidence=ProposalEvidence(
            case_names=case_names, recall_deficit=0.3, counterfactual=None, mined_links=(), oversized_sections=("p:9#findings (812)",)
        ),
        predictions=(Prediction(description="beta recovers", target_cases=case_names, metric="recall@5", direction="no_regression"),),
        migration=(
            PageWorkOrder(page_id="p:9", note_path="Memory/Papers/bar.md", action="split H2 'findings' at H3 'Results'"),
            PageWorkOrder(page_id="p:10", note_path="Memory/Papers/baz.md", action="split H2 'findings' at H3 'Discussion'"),
        ),
        rationale="the findings section is oversized and dilutes retrieval",
    )


def _recommendation(proposals: tuple[SchemaEditProposal, ...] = (), findings: tuple[StructuralFinding, ...] = ()) -> Recommendation:
    return Recommendation(
        collection="research",
        created_at=datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
        baseline=_baseline(),
        proposals=proposals,
        findings=findings,
    )


class TestMadrSections:
    def test_renders_the_core_madr_sections(self):
        out = render_draft_adr(_recommendation(proposals=(_add_relation_proposal(),)))

        assert "## Context" in out
        assert "## Decision" in out
        assert "## Consequences" in out
        assert "## Findings tail" in out

    def test_status_is_a_proposed_draft(self):
        out = render_draft_adr(_recommendation())

        assert "proposed" in out.lower()

    def test_title_names_the_collection(self):
        out = render_draft_adr(_recommendation())

        first_line = out.splitlines()[0]
        assert first_line.startswith("#")
        assert "research" in first_line

    def test_uses_only_the_recommendation_created_at_timestamp(self):
        out = render_draft_adr(_recommendation())

        assert "2026-07-03T12:00:00+00:00" in out


class TestContextEvidence:
    def test_context_reports_dataset_grade_and_knowledge_base_stamp(self):
        out = render_draft_adr(_recommendation(proposals=(_add_relation_proposal(),)))

        assert "research-ds" in out
        assert "raw-baseline" in out
        assert "abc123" in out
        assert "def456" in out
        assert "42" in out

    def test_context_reports_recall_deficit_and_counterfactual_numbers(self):
        out = render_draft_adr(_recommendation(proposals=(_add_relation_proposal(),)))

        assert "0.50" in out
        assert "0 -> 2" in out
        assert "3 hops" in out

    def test_context_clusters_mined_links_including_danglers(self):
        out = render_draft_adr(_recommendation(proposals=(_add_relation_proposal(),)))

        assert "paper -> paper" in out
        assert "paper -> (dangling)" in out

    def test_restructure_evidence_lists_oversized_sections(self):
        out = render_draft_adr(_recommendation(proposals=(_restructure_proposal(),)))

        assert "p:9#findings (812)" in out

    def test_restructure_evidence_renders_the_shadow_split_when_simulated(self):
        proposal = _restructure_proposal()
        simulation = SimulationResult(
            n_measured=2,
            n_unsplittable=1,
            mean_rank_before=9.0,
            mean_rank_after=3.5,
            splits=(SimulatedSplit(case_name="survey::beta", page_id="p:9", canon="findings", n_parts=3, rank_before=9, rank_after=3),),
        )
        enriched = proposal.model_copy(update={"evidence": proposal.evidence.model_copy(update={"simulation": simulation})})

        out = render_draft_adr(_recommendation(proposals=(enriched,)))

        assert "Shadow split: mean would-be rank 9.0 -> 3.5 over 2 case-section pair(s) (1 unsplittable)" in out


class TestCaseNamesVerbatim:
    def test_evidence_prediction_and_finding_case_names_appear_verbatim(self):
        proposal = _add_relation_proposal(case_names=("trace::alpha-beta",))
        finding = StructuralFinding(
            finding_type="layer", title="Method layer", body="bundle a method page type", related_cases=("finding::gamma-777",)
        )

        out = render_draft_adr(_recommendation(proposals=(proposal,), findings=(finding,)))

        assert "trace::alpha-beta" in out
        assert "finding::gamma-777" in out


class TestDecision:
    def test_add_relation_op_renders_slug_domain_range_and_edge_field(self):
        out = render_draft_adr(_recommendation(proposals=(_add_relation_proposal(),)))

        assert "extends" in out
        assert "paper" in out
        assert "link-list" in out

    def test_restructure_op_renders_kind_and_full_section_tuple(self):
        out = render_draft_adr(_recommendation(proposals=(_restructure_proposal(),)))

        for section in ("intro", "methods", "results"):
            assert section in out

    def test_decision_includes_the_rationale(self):
        out = render_draft_adr(_recommendation(proposals=(_add_relation_proposal(),)))

        assert "the unlifted p:1 -> p:2 link explains the miss" in out

    def test_proposals_appear_in_recommendation_rank_order(self):
        first = _add_relation_proposal(pid="proposal-alpha-first")
        second = _restructure_proposal(pid="proposal-beta-second")

        out = render_draft_adr(_recommendation(proposals=(first, second)))

        assert out.index("proposal-alpha-first") < out.index("proposal-beta-second")


class TestConsequences:
    def test_predictions_render_description_cases_metric_and_direction(self):
        out = render_draft_adr(_recommendation(proposals=(_add_relation_proposal(),)))

        assert "alpha recovers" in out
        assert "recall@5" in out
        assert "increase" in out

    def test_migration_burden_counts_and_lists_work_orders(self):
        out = render_draft_adr(_recommendation(proposals=(_restructure_proposal(),)))

        assert "2 work orders" in out
        assert "Memory/Papers/bar.md" in out
        assert "Memory/Papers/baz.md" in out
        assert "split H2 'findings' at H3 'Results'" in out

    def test_singular_work_order_wording(self):
        out = render_draft_adr(_recommendation(proposals=(_add_relation_proposal(),)))

        assert "1 work order" in out
        assert "1 work orders" not in out


class TestFindingsTail:
    def test_findings_render_kind_title_body_and_related_cases(self):
        finding = StructuralFinding(
            finding_type="machinery", title="Summary ladder grain", body="consider a coarser summary ladder", related_cases=("finding::case-777",)
        )

        out = render_draft_adr(_recommendation(findings=(finding,)))

        assert "machinery" in out
        assert "Summary ladder grain" in out
        assert "consider a coarser summary ladder" in out
        assert "finding::case-777" in out


class TestGracefulDegradation:
    def test_empty_proposals_and_findings_keep_sections_and_do_not_crash(self):
        out = render_draft_adr(_recommendation(proposals=(), findings=()))

        assert "## Context" in out
        assert "## Decision" in out
        assert "## Consequences" in out
        assert "## Findings tail" in out
        assert "no spec proposal" in out.lower()
        assert "no finding" in out.lower()


class TestDeterminism:
    def test_same_recommendation_renders_identically(self):
        recommendation = _recommendation(
            proposals=(_add_relation_proposal(), _restructure_proposal()),
            findings=(StructuralFinding(finding_type="page_type", title="Method", body="a method page type", related_cases=("trace::alpha",)),),
        )

        assert render_draft_adr(recommendation) == render_draft_adr(recommendation)

    def test_two_equal_recommendations_render_identically(self):
        first = _recommendation(proposals=(_add_relation_proposal(),))
        second = _recommendation(proposals=(_add_relation_proposal(),))

        assert render_draft_adr(first) == render_draft_adr(second)
