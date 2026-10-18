"""Unit specs for `engram.evals.recommendation.infrastructure.schema_edits` and the `evals apply-spec` CLI verb.

These specs pin the apply contract: `apply_schema_edits` folds only the selected proposals' ops onto the
knowledge base's spec and rewrites `knowledge-base-schema.json` in the working tree only, never touching notes, never
indexing, and never running git. Unknown proposal ids and an empty selection fail before any write.
Re-applying the same selection is idempotent, the file stays byte-identical and the report says so.
"""

from __future__ import annotations

import json

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from engram.evals.recommendation.contracts import (
    ApplySchemaEditsReport,
    BaselineSnapshot,
    CaseSnapshot,
    KnowledgeBaseStamp,
    apply_schema_edits,
    save_recommendation,
)
from engram.evals.recommendation.domain.recommendation import (
    AddRelationOp,
    PageWorkOrder,
    Prediction,
    ProposalEvidence,
    Recommendation,
    RestructureSectionsOp,
    SchemaEditProposal,
)
from engram.kb.contracts import (
    CanonicalSection,
    FrontmatterFieldSpec,
    KnowledgeBaseSchema,
    PageTypeSpec,
    RelationDecl,
    load_knowledge_base_schema,
)
from typer.testing import CliRunner

from studio.knowledge_base.cli import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _spec() -> KnowledgeBaseSchema:
    """A minimal valid spec: one `paper` page type with a link-typed `cites` edge and matching relation."""
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


def _evidence() -> ProposalEvidence:
    return ProposalEvidence(case_names=("c1", "c2"), recall_deficit=0.5, counterfactual=None, mined_links=(), oversized_sections=())


def _restructure_proposal() -> SchemaEditProposal:
    return SchemaEditProposal(
        id="restructure-paper-sections",
        op=RestructureSectionsOp(page_type="paper", canonical_sections=("summary", "findings-methods", "findings-results")),
        evidence=_evidence(),
        predictions=(Prediction(description="c1 recovers", target_cases=("c1",), metric="recall@5", direction="increase"),),
        migration=(PageWorkOrder(page_id="p:1", note_path="Memory/Papers/foo.md", action="split H2 'findings' into methods/results"),),
        rationale="gold sections blow the token budget",
    )


def _add_relation_proposal() -> SchemaEditProposal:
    return SchemaEditProposal(
        id="add-relation-extends-on-paper",
        op=AddRelationOp(
            relation=RelationDecl(slug="extends", domain="paper", range="paper"),
            on_page_type="paper",
            edge_field=FrontmatterFieldSpec(name="extends", type="link-list"),
        ),
        evidence=_evidence(),
        predictions=(Prediction(description="c2 recovers", target_cases=("c2",), metric="recall@5", direction="increase"),),
        migration=(PageWorkOrder(page_id="p:1", note_path="Memory/Papers/foo.md", action="add an extends link to p:2"),),
        rationale="unlifted extends links explain the trace misses",
    )


def _recommendation() -> Recommendation:
    return Recommendation(
        collection="research",
        created_at=datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
        baseline=BaselineSnapshot(
            dataset="ds",
            run_id=None,
            policy_source="default",
            evidence_grade="raw-baseline",
            knowledge_base=KnowledgeBaseStamp(collection="research", spec_hash="abc", manifest_digest="def", n_pages=1),
            overall={"recall@5": 0.5},
            cases=(CaseSnapshot(case_name="c1", recall=0.0, gold_ids=("p:1",), cited_ids=()),),
        ),
        proposals=(_restructure_proposal(), _add_relation_proposal()),
        findings=(),
    )


@pytest.fixture
def knowledge_base(tmp_path: Path) -> Path:
    """A tmp knowledge base with a knowledge-base-schema.json in the canonical on-disk format and one untouched note."""
    knowledge_base_dir = tmp_path / "knowledge_base"
    (knowledge_base_dir / "Units" / "Papers").mkdir(parents=True)
    (knowledge_base_dir / "knowledge-base-schema.json").write_text(_spec().model_dump_json(indent=2) + "\n", encoding="utf-8")
    (knowledge_base_dir / "Units" / "Papers" / "foo.md").write_text("# Foo\n\n## findings\n\nbody\n", encoding="utf-8")
    return knowledge_base_dir


@pytest.fixture
def recommendation_path(tmp_path: Path) -> Path:
    path = tmp_path / "research.yaml"
    save_recommendation(_recommendation(), path)
    return path


class TestApplySpec:
    def test_applies_only_the_selected_proposal_and_writes_only_knowledge_base_schema(self, knowledge_base: Path, recommendation_path: Path):
        note = knowledge_base / "Units" / "Papers" / "foo.md"
        note_before = note.read_text(encoding="utf-8")

        report = apply_schema_edits(recommendation_path=recommendation_path, proposal_ids=("restructure-paper-sections",), knowledge_base_path=knowledge_base)

        spec = load_knowledge_base_schema(knowledge_base)
        paper = spec.page_type("paper")
        assert paper is not None
        assert paper.canonical_section_slugs == ("summary", "findings-methods", "findings-results")
        assert all(relation.slug != "extends" for relation in spec.relations)
        assert note.read_text(encoding="utf-8") == note_before
        assert sorted(p.name for p in knowledge_base.iterdir()) == ["Units", "knowledge-base-schema.json"]
        assert isinstance(report, ApplySchemaEditsReport)
        assert report.applied_ids == ("restructure-paper-sections",)
        assert report.already_applied_ids == ()
        assert report.changed is True
        assert report.knowledge_base_schema_path == str(knowledge_base / "knowledge-base-schema.json")

    def test_applies_both_proposals_folded_in_order(self, knowledge_base: Path, recommendation_path: Path):
        apply_schema_edits(
            recommendation_path=recommendation_path, proposal_ids=("restructure-paper-sections", "add-relation-extends-on-paper"), knowledge_base_path=knowledge_base
        )

        spec = load_knowledge_base_schema(knowledge_base)
        paper = spec.page_type("paper")
        assert paper is not None
        assert paper.canonical_section_slugs == ("summary", "findings-methods", "findings-results")
        assert any(relation.slug == "extends" for relation in spec.relations)
        assert "extends" in paper.edge_fields

    def test_an_unknown_proposal_id_fails_before_any_write(self, knowledge_base: Path, recommendation_path: Path):
        before = (knowledge_base / "knowledge-base-schema.json").read_text(encoding="utf-8")

        with pytest.raises(ValueError, match="unknown proposal id"):
            apply_schema_edits(recommendation_path=recommendation_path, proposal_ids=("nope",), knowledge_base_path=knowledge_base)

        assert (knowledge_base / "knowledge-base-schema.json").read_text(encoding="utf-8") == before

    def test_an_empty_selection_is_an_error(self, knowledge_base: Path, recommendation_path: Path):
        with pytest.raises(ValueError, match="no proposal ids"):
            apply_schema_edits(recommendation_path=recommendation_path, proposal_ids=(), knowledge_base_path=knowledge_base)

    def test_reapplying_the_same_selection_is_idempotent_and_byte_identical(self, knowledge_base: Path, recommendation_path: Path):
        ids = ("restructure-paper-sections", "add-relation-extends-on-paper")
        apply_schema_edits(recommendation_path=recommendation_path, proposal_ids=ids, knowledge_base_path=knowledge_base)
        first = (knowledge_base / "knowledge-base-schema.json").read_text(encoding="utf-8")

        report = apply_schema_edits(recommendation_path=recommendation_path, proposal_ids=ids, knowledge_base_path=knowledge_base)

        assert (knowledge_base / "knowledge-base-schema.json").read_text(encoding="utf-8") == first
        assert report.changed is False
        assert report.applied_ids == ()
        assert set(report.already_applied_ids) == set(ids)

    def test_a_conflicting_relation_slug_still_fails_via_the_spec_invariants(self, knowledge_base: Path, tmp_path: Path):
        # The same slug with a DIFFERENT declaration is a conflict, not an already-applied no-op.
        conflicting = _recommendation().model_copy(
            update={
                "proposals": (
                    _add_relation_proposal().model_copy(
                        update={
                            "op": AddRelationOp(
                                relation=RelationDecl(slug="cites", domain="paper", range="paper", symmetric=True),
                                on_page_type="paper",
                                edge_field=FrontmatterFieldSpec(name="cites2", type="link-list"),
                            )
                        }
                    ),
                )
            }
        )
        path = tmp_path / "conflict.yaml"
        save_recommendation(conflicting, path)

        with pytest.raises(ValueError, match="cites"):
            apply_schema_edits(recommendation_path=path, proposal_ids=("add-relation-extends-on-paper",), knowledge_base_path=knowledge_base)


class TestApplyDoesNotInflateAnUntouchedPageTypesSections:
    """A rewrite must not inflate a sibling page type's canonical_sections.

    Its bare-string form must not turn into `{"slug": ..., "heading": null}` objects on disk —
    `CanonicalSection`'s compact serializer
    (`model_dump`/`model_dump_json` re-emit a headingless entry as a bare string) is what keeps
    `apply_schema_edits`'s "rewrites knowledge-base-schema.json... deliberately mechanical" claim minimal-diff, not
    just for the page type an op actually touches.
    """

    def _two_page_type_knowledge_base(self, tmp_path: Path) -> Path:
        spec = KnowledgeBaseSchema(
            slug="test",
            page_types=(
                PageTypeSpec(
                    page_type="paper",
                    id_prefix="p",
                    canonical_sections=(CanonicalSection(slug="summary"), CanonicalSection(slug="findings")),
                    frontmatter_fields=(FrontmatterFieldSpec(name="cites", type="link-list"),),
                    edge_fields=("cites",),
                ),
                PageTypeSpec(page_type="entity", id_prefix="e", canonical_sections=(CanonicalSection(slug="overview"), CanonicalSection(slug="notes"))),
            ),
            relations=(RelationDecl(slug="cites", domain="paper", range="paper"),),
        )
        knowledge_base_dir = tmp_path / "knowledge_base"
        knowledge_base_dir.mkdir(parents=True)
        (knowledge_base_dir / "knowledge-base-schema.json").write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return knowledge_base_dir

    def test_restructuring_one_page_type_leaves_a_sibling_page_types_sections_as_bare_strings(self, tmp_path: Path, recommendation_path: Path):
        knowledge_base = self._two_page_type_knowledge_base(tmp_path)

        apply_schema_edits(recommendation_path=recommendation_path, proposal_ids=("restructure-paper-sections",), knowledge_base_path=knowledge_base)

        raw = json.loads((knowledge_base / "knowledge-base-schema.json").read_text(encoding="utf-8"))
        entity = next(pt for pt in raw["page_types"] if pt["page_type"] == "entity")
        assert entity["canonical_sections"] == ["overview", "notes"]

    def test_an_unrelated_add_relation_never_touches_any_kinds_sections_shape(self, tmp_path: Path, recommendation_path: Path):
        knowledge_base = self._two_page_type_knowledge_base(tmp_path)

        apply_schema_edits(recommendation_path=recommendation_path, proposal_ids=("add-relation-extends-on-paper",), knowledge_base_path=knowledge_base)

        raw = json.loads((knowledge_base / "knowledge-base-schema.json").read_text(encoding="utf-8"))
        for page_type in raw["page_types"]:
            assert all(isinstance(section, str) for section in page_type["canonical_sections"])


class TestApplySpecCli:
    def test_apply_spec_help_lists_the_flags(self):
        result = runner.invoke(app, ["evals", "apply-spec", "--help"])

        assert result.exit_code == 0
        for flag in ("--recommendation", "--id", "--knowledge-base"):
            assert flag in result.stdout

    def test_happy_path_prints_the_follow_up_protocol(self, knowledge_base: Path, recommendation_path: Path):
        result = runner.invoke(
            app, ["evals", "apply-spec", "--recommendation", str(recommendation_path), "--id", "restructure-paper-sections", "--knowledge-base", str(knowledge_base)]
        )

        assert result.exit_code == 0
        assert "working tree only" in result.stdout
        assert "studio index" in result.stdout
        assert "verify-recommendation" in result.stdout
        assert "gen-dataset" in result.stdout

    def test_a_missing_recommendation_fails_cleanly(self, knowledge_base: Path, tmp_path: Path):
        result = runner.invoke(
            app, ["evals", "apply-spec", "--recommendation", str(tmp_path / "missing.yaml"), "--id", "x", "--knowledge-base", str(knowledge_base)]
        )

        assert result.exit_code == 1
        assert "evals apply-spec failed" in result.stdout

    def test_an_unknown_id_fails_cleanly_listing_the_valid_ids(self, knowledge_base: Path, recommendation_path: Path):
        result = runner.invoke(app, ["evals", "apply-spec", "--recommendation", str(recommendation_path), "--id", "nope", "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 1
        assert "evals apply-spec failed" in result.stdout
