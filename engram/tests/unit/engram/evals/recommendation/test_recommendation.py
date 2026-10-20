"""Unit specs for the recommendation artifact kernel (`engram.evals.recommendation.domain.recommendation`).

These specs pin the kernel's contract: the `SchemaEditOp` discriminated union round-trips through its
`op` discriminator, `apply_op` returns a new validated `KnowledgeBaseSchema` for a valid restructure or
add-relation and is rejected by the spec's own invariants for an unknown kind, a duplicate relation
slug, or a non-link edge field, and the `Recommendation` artifact survives a lossless YAML
round-trip with a timezone-aware `created_at`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import yaml

from pydantic import TypeAdapter

from engram.evals.recommendation.contracts import (
    BaselineSnapshot,
    CandidateEdge,
    CaseSnapshot,
    KnowledgeBaseStamp,
    MinedLink,
    load_recommendation,
    save_recommendation,
)
from engram.evals.recommendation.domain.recommendation import (
    AddRelationOp,
    CounterfactualResult,
    PageWorkOrder,
    Prediction,
    ProposalEvidence,
    Recommendation,
    RestructureSectionsOp,
    SchemaEditOp,
    SchemaEditProposal,
    SimulatedSplit,
    SimulationResult,
    StructuralFinding,
    apply_op,
)
from engram.kb.contracts import CanonicalSection, FrontmatterFieldSpec, KnowledgeBaseSchema, RelationDecl

if TYPE_CHECKING:
    from pathlib import Path


def _spec() -> KnowledgeBaseSchema:
    """A minimal valid spec: one `paper` page type with a link-typed `cites` edge and matching relation."""
    from engram.kb.contracts import PageTypeSpec

    return KnowledgeBaseSchema(
        slug="test",
        page_types=(
            PageTypeSpec(
                page_type="paper",
                id_prefix="p",
                canonical_sections=(CanonicalSection(slug="summary"), CanonicalSection(slug="findings")),
                frontmatter_fields=(FrontmatterFieldSpec(name="cites", type="link-list"),),
                edge_fields=("cites",),
            ),
        ),
        relations=(RelationDecl(slug="cites", domain="paper", range="paper"),),
    )


def _recommendation() -> Recommendation:
    """A fully populated recommendation covering every nested artifact model, with a tz-aware `created_at`."""
    return Recommendation(
        collection="research",
        created_at=datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
        baseline=BaselineSnapshot(
            dataset="ds",
            run_id="run-1",
            policy_source="default",
            evidence_grade="raw-baseline",
            knowledge_base=KnowledgeBaseStamp(collection="research", spec_hash="abc", manifest_digest="def", n_pages=42),
            overall={"recall@5": 0.5, "precision@5": 0.4},
            cases=(CaseSnapshot(case_name="c1", recall=0.0, gold_ids=("p:1",), cited_ids=("p:2",)),),
        ),
        proposals=(
            SchemaEditProposal(
                id="add-relation-extends-on-paper",
                op=AddRelationOp(
                    relation=RelationDecl(slug="extends", domain="paper", range="paper"),
                    on_page_type="paper",
                    edge_field=FrontmatterFieldSpec(name="extends", type="link-list"),
                ),
                evidence=ProposalEvidence(
                    case_names=("c1",),
                    recall_deficit=0.5,
                    counterfactual=CounterfactualResult(
                        n_pairs=1,
                        reachable_before=0,
                        reachable_after=1,
                        max_hops=3,
                        candidate_edges=(CandidateEdge(src_id="p:1", dst_id="p:2", rel="extends"),),
                    ),
                    mined_links=(
                        MinedLink(
                            src_page_id="p:1", src_type="paper", raw_target="Foo", dst_page_id="p:2", dst_type="paper", rel="extends"
                        ),
                    ),
                    oversized_sections=(),
                ),
                predictions=(Prediction(description="c1 recovers", target_cases=("c1",), metric="recall@5", direction="increase"),),
                migration=(PageWorkOrder(page_id="p:1", note_path="Memory/Papers/foo.md", action="add an extends link to p:2"),),
                rationale="the unlifted p:1 -> p:2 link explains c1's miss",
            ),
        ),
        findings=(
            StructuralFinding(
                finding_type="layer",
                title="Method layer",
                body="bundle a method page type with an implements relation as one coherent layer",
                related_cases=("c1",),
            ),
        ),
    )


class TestSchemaEditOpDiscriminator:
    def test_restructure_op_round_trips_via_the_op_discriminator(self):
        adapter: TypeAdapter[SchemaEditOp] = TypeAdapter(SchemaEditOp)
        op = RestructureSectionsOp(page_type="paper", canonical_sections=("intro", "methods", "results"))

        loaded = adapter.validate_python(op.model_dump())

        assert isinstance(loaded, RestructureSectionsOp)
        assert loaded == op

    def test_add_relation_op_round_trips_via_the_op_discriminator(self):
        adapter: TypeAdapter[SchemaEditOp] = TypeAdapter(SchemaEditOp)
        op = AddRelationOp(
            relation=RelationDecl(slug="extends", domain="paper", range="paper"),
            on_page_type="paper",
            edge_field=FrontmatterFieldSpec(name="extends", type="link-list"),
        )

        loaded = adapter.validate_python(op.model_dump())

        assert isinstance(loaded, AddRelationOp)
        assert loaded == op


class TestApplyOp:
    def test_restructure_replaces_the_named_page_types_sections(self):
        spec = _spec()
        op = RestructureSectionsOp(page_type="paper", canonical_sections=("intro", "methods", "results"))

        result = apply_op(spec, op)

        assert result.sections_for("paper") == ("intro", "methods", "results")

    def test_apply_op_never_mutates_the_input_spec(self):
        spec = _spec()
        op = RestructureSectionsOp(page_type="paper", canonical_sections=("intro",))

        result = apply_op(spec, op)

        assert result is not spec
        assert spec.sections_for("paper") == ("summary", "findings")

    def test_add_relation_registers_the_relation_and_places_the_edge_field(self):
        spec = _spec()
        op = AddRelationOp(
            relation=RelationDecl(slug="extends", domain="paper", range="paper"),
            on_page_type="paper",
            edge_field=FrontmatterFieldSpec(name="extends", type="link-list"),
        )

        result = apply_op(spec, op)

        assert any(r.slug == "extends" for r in result.relations)
        assert "extends" in result.edge_fields_for("paper")
        assert result.field_spec("paper", "extends") is not None
        assert all(r.slug != "extends" for r in spec.relations)

    def test_restructure_of_an_unknown_kind_raises(self):
        spec = _spec()
        op = RestructureSectionsOp(page_type="ghost", canonical_sections=("a",))

        with pytest.raises(ValueError, match="unknown page type kind"):
            apply_op(spec, op)

    def test_add_relation_on_an_unknown_kind_raises(self):
        spec = _spec()
        op = AddRelationOp(
            relation=RelationDecl(slug="extends", domain="paper", range="paper"),
            on_page_type="ghost",
            edge_field=FrontmatterFieldSpec(name="extends", type="link-list"),
        )

        with pytest.raises(ValueError, match="unknown page type kind"):
            apply_op(spec, op)

    def test_a_duplicate_relation_slug_is_rejected_by_the_spec_invariants(self):
        spec = _spec()  # already declares a "cites" relation
        op = AddRelationOp(
            relation=RelationDecl(slug="cites", domain="paper", range="paper"),
            on_page_type="paper",
            edge_field=FrontmatterFieldSpec(name="cites_again", type="link-list"),
        )

        with pytest.raises(ValueError, match="relation slug"):
            apply_op(spec, op)

    def test_a_non_link_edge_field_is_rejected_by_the_spec_invariants(self):
        spec = _spec()
        op = AddRelationOp(
            relation=RelationDecl(slug="notes", domain="paper", range="paper"),
            on_page_type="paper",
            edge_field=FrontmatterFieldSpec(name="note", type="scalar"),
        )

        with pytest.raises(ValueError, match="link"):
            apply_op(spec, op)


class TestRecommendationRoundTrip:
    def test_save_then_load_is_identity(self, tmp_path: Path):
        recommendation = _recommendation()
        path = tmp_path / "research.yaml"

        save_recommendation(recommendation, path)

        assert load_recommendation(path) == recommendation

    def test_saving_into_a_directory_that_does_not_exist_creates_it(self, tmp_path: Path):
        recommendation = _recommendation()
        path = tmp_path / "evals" / "recommendations" / "research.yaml"

        save_recommendation(recommendation, path)

        assert load_recommendation(path) == recommendation

    def test_round_trip_preserves_a_tz_aware_created_at(self, tmp_path: Path):
        recommendation = _recommendation()
        path = tmp_path / "research.yaml"

        save_recommendation(recommendation, path)
        loaded = load_recommendation(path)

        assert loaded.created_at.tzinfo is not None
        assert loaded.created_at == recommendation.created_at

    def test_round_trip_preserves_the_op_subtype_through_the_discriminator(self):
        recommendation = _recommendation()
        adapter: TypeAdapter[Recommendation] = TypeAdapter(Recommendation)

        reloaded = adapter.validate_python(recommendation.model_dump(mode="json"))

        assert isinstance(reloaded.proposals[0].op, AddRelationOp)

    def test_a_recommendation_saved_without_simulation_evidence_reloads_with_none(self, tmp_path: Path):
        path = tmp_path / "research.yaml"
        save_recommendation(_recommendation(), path)

        loaded = load_recommendation(path)

        assert loaded.proposals[0].evidence.simulation is None

    def test_round_trip_preserves_the_shadow_split_evidence(self, tmp_path: Path):
        recommendation = _recommendation()
        simulation = SimulationResult(
            n_measured=1,
            n_unsplittable=0,
            mean_rank_before=9.0,
            mean_rank_after=3.0,
            splits=(SimulatedSplit(case_name="case-a", page_id="p:1", canon="findings", n_parts=2, rank_before=9, rank_after=3),),
        )
        proposal = recommendation.proposals[0]
        enriched = recommendation.model_copy(
            update={
                "proposals": (proposal.model_copy(update={"evidence": proposal.evidence.model_copy(update={"simulation": simulation})}),)
            }
        )
        path = tmp_path / "research.yaml"

        save_recommendation(enriched, path)
        loaded = load_recommendation(path)

        assert loaded.proposals[0].evidence.simulation == simulation


class TestFileKeys:
    def test_the_ops_and_findings_read_their_file_keys_into_their_fields(self):
        restructure = RestructureSectionsOp.model_validate({"op": "restructure_sections", "kind": "paper", "canonical_sections": ["summary"]})
        add_relation = AddRelationOp.model_validate(
            {
                "op": "add_relation",
                "relation": {"slug": "extends", "domain": "paper", "range": "paper"},
                "on_kind": "paper",
                "edge_field": {"name": "extends", "type": "link-list"},
            }
        )
        finding = StructuralFinding.model_validate({"kind": "layer", "title": "Method layer", "body": "b", "related_cases": ["c1"]})

        assert (restructure.page_type, add_relation.on_page_type, finding.finding_type) == ("paper", "paper", "layer")

    def test_an_op_read_from_its_file_key_equals_one_built_by_field_name(self):
        from_file = RestructureSectionsOp.model_validate_json('{"kind": "paper", "canonical_sections": ["summary"]}')
        by_name = RestructureSectionsOp(page_type="paper", canonical_sections=("summary",))

        assert from_file == by_name
        assert hash(from_file) == hash(by_name)

    def test_a_saved_recommendation_keeps_the_kind_and_on_kind_keys(self, tmp_path: Path):
        path = tmp_path / "research.yaml"

        save_recommendation(_recommendation(), path)
        written = yaml.safe_load(path.read_text(encoding="utf-8"))

        assert written["proposals"][0]["op"]["on_kind"] == "paper"
        assert "on_page_type" not in written["proposals"][0]["op"]
        assert written["findings"][0]["kind"] == "layer"
        assert "finding_type" not in written["findings"][0]
        assert RestructureSectionsOp(page_type="paper", canonical_sections=("summary",)).model_dump() == {
            "op": "restructure_sections",
            "kind": "paper",
            "canonical_sections": ("summary",),
        }
