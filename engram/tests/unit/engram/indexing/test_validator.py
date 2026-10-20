"""Unit tests for the pure validation validator (`src/engram/indexing/domain/validator.py`).

These pin EVERY validation rule the index *Validate* stage defines, each with one passing
and one failing case:

* **vocabulary** — unregistered entity_type / relation key / facet value / claim kind;
* **claims** — missing `loc` / `kind`; missing `quote` where required
  (`benchmark-result` / `ablation` / any evidential edge); a comparative edge
  (`outperforms` / `comparable_to`) without `on_dataset`(or `on_task`)+`metric`;
  block-id (anchor) collisions; quote-not-found-in-source (the quote check);
* **sections** — oversized `summary` (> 400 chars).

The validator only **reports** (severity error/warning); it never filters — the
flagged-but-loaded contract. Vocabulary comes from the golden mini-vocabularies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from engram.indexing.contracts import MirrorDoc, resolve, validate_note
from engram.indexing.domain.validator import mirror_provider_issues, validate, validate_vocabulary
from engram.kb.contracts import (
    CanonicalSection,
    ClaimBlock,
    ClaimGrammarSpec,
    FacetSpec,
    FrontmatterFieldSpec,
    KnowledgeBaseNote,
    KnowledgeBaseSchema,
    PageTypeSpec,
    Section,
    SourcesSpec,
    UnitLink,
    VocabularyIndex,
    parse_frontmatter,
    parse_vocabulary,
)
from engram.testing.knowledge_base_builder import GOLDEN_MINI_VOCABULARIES
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from collections.abc import Mapping

    from engram.indexing.contracts import ValidationReport

_MINI = GOLDEN_MINI_VOCABULARIES


@pytest.fixture(scope="module")
def vocabulary() -> VocabularyIndex:
    docs = [(p.name, p.read_text(encoding="utf-8")) for p in sorted(_MINI.glob("*.md"))]
    return parse_vocabulary(docs).index


_RESEARCH_SPEC = research_spec()


def _paper(
    *,
    id_: str = "paper.lightrag-2025",
    summary: str | None = "A short summary.",
    claims: tuple[ClaimBlock, ...] = (),
    sections: tuple[Section, ...] = (),
) -> KnowledgeBaseNote:
    frontmatter = parse_frontmatter(
        {
            "id": id_,
            "type": "paper",
            "title": "LightRAG",
            "summary": summary,
            "status": "draft",
            "paper_type": "method",
            "year": 2025,
            "zotero_key": "K",
            "source_hash": "h",
        },
        _RESEARCH_SPEC,
    )
    return KnowledgeBaseNote(frontmatter=frontmatter, sections=sections, claims=claims, source_path="Memory/Papers/lightrag-2025.md")


# A knowledge base of its own whose mirror key field is a knowledge base path, not the research bundle's field name:
# the fixture every spec-driven-key-field test reads.
_DOCUMENT_TYPE = PageTypeSpec(
    page_type="document",
    id_prefix="doc.",
    frontmatter_fields=(
        FrontmatterFieldSpec(name="source_path", type="scalar"),
        FrontmatterFieldSpec(name="content_hash", type="scalar"),
    ),
    claims_enabled=True,
)

_PATH_KEYED_SPEC = KnowledgeBaseSchema(
    slug="handbooks",
    page_types=(_DOCUMENT_TYPE,),
    claims=ClaimGrammarSpec(section="findings"),
    sources=SourcesSpec(
        enabled=True,
        connectors=("library",),
        key_field="source_path",
        digest_link_field="derived_from",
        hash_field="content_hash",
        digest_types={"article": "document"},
    ),
)

_KEYLESS_SPEC = KnowledgeBaseSchema(slug="handbooks", page_types=(_DOCUMENT_TYPE,), claims=ClaimGrammarSpec(section="findings"))


def _document(*, claims: tuple[ClaimBlock, ...] = (), **fields: object) -> KnowledgeBaseNote:
    """A `document` note of `_PATH_KEYED_SPEC`'s own page_type, carrying whatever frontmatter `fields` names."""
    frontmatter = parse_frontmatter(
        {"id": "doc.handbook", "type": "document", "title": "Handbook", "status": "draft", **fields}, _PATH_KEYED_SPEC
    )
    return KnowledgeBaseNote(frontmatter=frontmatter, sections=(), claims=claims, source_path="Wiki/handbook.md")


def _entity(*, id_: str = "ent.lightrag", entity_type: str = "technique") -> KnowledgeBaseNote:
    frontmatter = parse_frontmatter(
        {"id": id_, "type": "entity", "title": "LightRAG", "status": "draft", "entity_type": entity_type}, _RESEARCH_SPEC
    )
    return KnowledgeBaseNote(frontmatter=frontmatter, sections=(), claims=(), source_path="Memory/Entities/lightrag.md")


def _claim(**overrides: object) -> ClaimBlock:
    base: dict[str, object] = {"anchor": "^c1", "text": "A claim.", "loc": "§1", "kind": "observation"}
    base.update(overrides)
    return ClaimBlock.model_validate(base)


def _report(
    notes: tuple[KnowledgeBaseNote, ...],
    vocabulary: VocabularyIndex,
    *,
    mirror_texts: Mapping[str, str] | None = None,
    spec: KnowledgeBaseSchema = _RESEARCH_SPEC,
) -> ValidationReport:
    resolved = resolve(notes)
    return validate(resolved.notes, vocabulary, mirror_texts=mirror_texts, resolve_result=resolved, spec=spec)


def _with_qualifiers(slug: str, qualifiers: tuple[tuple[str, ...], ...]) -> KnowledgeBaseSchema:
    """The research spec with relation `slug` declaring `qualifiers` in place of its own."""
    relations = tuple(
        relation.model_copy(update={"qualifiers": qualifiers}) if relation.slug == slug else relation for relation in _RESEARCH_SPEC.relations
    )
    return _RESEARCH_SPEC.model_copy(update={"relations": relations})


def _rules(report: ValidationReport) -> set[str]:
    return {issue.rule for issue in report.errors} | {issue.rule for issue in report.warnings}


class TestClaimRequiredFields:
    """Missing `loc`/`kind` and required `quote` are claim validation errors."""

    def test_missing_kind_is_an_error(self, vocabulary: VocabularyIndex):
        report = _report((_paper(claims=(_claim(kind=""),)),), vocabulary)

        assert any(i.rule == "missing-kind" and i.anchor == "^c1" and "one of: " in i.message for i in report.errors)

    def test_present_kind_passes(self, vocabulary: VocabularyIndex):
        report = _report((_paper(claims=(_claim(kind="observation"),)),), vocabulary)

        assert "missing-kind" not in _rules(report)

    def test_benchmark_result_without_quote_is_an_error(self, vocabulary: VocabularyIndex):
        report = _report((_paper(claims=(_claim(kind="benchmark-result", quote=None),)),), vocabulary)

        assert any(i.rule == "missing-quote" for i in report.errors)

    def test_benchmark_result_with_quote_passes(self, vocabulary: VocabularyIndex):
        report = _report((_paper(claims=(_claim(kind="benchmark-result", quote="a snippet"),)),), vocabulary)

        assert "missing-quote" not in _rules(report)

    def test_evidential_edge_without_quote_is_an_error(self, vocabulary: VocabularyIndex):
        claim = _claim(kind="observation", quote=None, edges=(("supports", UnitLink(target="ent.x")),))
        report = _report((_paper(claims=(claim,)),), vocabulary)

        assert any(i.rule == "missing-quote" for i in report.errors)

    def test_observation_without_quote_passes(self, vocabulary: VocabularyIndex):
        report = _report((_paper(claims=(_claim(kind="observation", quote=None),)),), vocabulary)

        assert "missing-quote" not in _rules(report)


class TestComparativeQualifiers:
    """A comparative edge requires `on_dataset` (or `on_task`) + `metric`."""

    def test_comparative_edge_missing_metric_is_an_error(self, vocabulary: VocabularyIndex):
        claim = _claim(
            kind="benchmark-result",
            quote="q",
            edges=(("outperforms", UnitLink(target="ent.graphrag")),),
            links={"on_dataset": (UnitLink(target="ent.ultradomain"),)},
        )
        report = _report((_paper(claims=(claim,)),), vocabulary)

        assert any(i.rule == "comparative-missing-qualifiers" for i in report.errors)

    def test_comparative_edge_missing_dataset_and_task_is_an_error(self, vocabulary: VocabularyIndex):
        claim = _claim(
            kind="benchmark-result",
            quote="q",
            edges=(("outperforms", UnitLink(target="ent.graphrag")),),
            links={"metric": (UnitLink(target="ent.win-rate"),)},
        )
        report = _report((_paper(claims=(claim,)),), vocabulary)

        assert any(i.rule == "comparative-missing-qualifiers" for i in report.errors)

    def test_comparative_edge_with_dataset_and_metric_passes(self, vocabulary: VocabularyIndex):
        claim = _claim(
            kind="benchmark-result",
            quote="q",
            edges=(("outperforms", UnitLink(target="ent.graphrag")),),
            links={"on_dataset": (UnitLink(target="ent.ultradomain"),), "metric": (UnitLink(target="ent.win-rate"),)},
        )
        report = _report((_paper(claims=(claim,)),), vocabulary)

        assert "comparative-missing-qualifiers" not in _rules(report)

    def test_comparative_edge_with_task_and_metric_passes(self, vocabulary: VocabularyIndex):
        claim = _claim(
            kind="benchmark-result",
            quote="q",
            edges=(("comparable_to", UnitLink(target="ent.graphrag")),),
            links={"on_task": (UnitLink(target="ent.long-doc-qa"),), "metric": (UnitLink(target="ent.win-rate"),)},
        )
        report = _report((_paper(claims=(claim,)),), vocabulary)

        assert "comparative-missing-qualifiers" not in _rules(report)

    def test_outperforms_with_task_and_metric_passes(self, vocabulary: VocabularyIndex):
        claim = _claim(
            kind="benchmark-result",
            quote="q",
            edges=(("outperforms", UnitLink(target="ent.graphrag")),),
            links={"on_task": (UnitLink(target="ent.long-doc-qa"),), "metric": (UnitLink(target="ent.win-rate"),)},
        )
        report = _report((_paper(claims=(claim,)),), vocabulary)

        assert "comparative-missing-qualifiers" not in _rules(report)

    def test_outperforms_with_only_metric_names_dataset_and_task(self, vocabulary: VocabularyIndex):
        claim = _claim(
            kind="benchmark-result",
            quote="q",
            edges=(("outperforms", UnitLink(target="ent.graphrag")),),
            links={"metric": (UnitLink(target="ent.win-rate"),)},
        )
        report = _report((_paper(claims=(claim,)),), vocabulary)

        [issue] = [i for i in report.errors if i.rule == "comparative-missing-qualifiers"]
        assert "on_dataset" in issue.message
        assert "on_task" in issue.message
        assert issue.anchor == "^c1"

    def test_the_refusal_names_the_claim_the_relation_and_only_the_missing_group(self, vocabulary: VocabularyIndex):
        claim = _claim(
            kind="benchmark-result",
            quote="q",
            edges=(("outperforms", UnitLink(target="ent.graphrag")),),
            links={"metric": (UnitLink(target="ent.win-rate"),)},
        )
        report = _report((_paper(claims=(claim,)),), vocabulary)

        assert [i.message for i in report.errors if i.rule == "comparative-missing-qualifiers"] == [
            "claim '^c1' asserting 'outperforms' must carry on_dataset or on_task"
        ]

    def test_the_refusal_names_every_missing_group(self, vocabulary: VocabularyIndex):
        claim = _claim(kind="benchmark-result", quote="q", edges=(("comparable_to", UnitLink(target="ent.graphrag")),))
        report = _report((_paper(claims=(claim,)),), vocabulary)

        assert [i.message for i in report.errors if i.rule == "comparative-missing-qualifiers"] == [
            "claim '^c1' asserting 'comparable_to' must carry on_dataset or on_task, and metric"
        ]

    def test_the_relation_declared_groups_are_the_rule(self, vocabulary: VocabularyIndex):
        spec = _with_qualifiers("outperforms", (("metric",),))
        with_metric = _claim(
            anchor="^c1",
            kind="benchmark-result",
            quote="q",
            edges=(("outperforms", UnitLink(target="ent.graphrag")),),
            links={"metric": (UnitLink(target="ent.win-rate"),)},
        )
        with_dataset = _claim(
            anchor="^c2",
            kind="benchmark-result",
            quote="q",
            edges=(("outperforms", UnitLink(target="ent.graphrag")),),
            links={"on_dataset": (UnitLink(target="ent.ultradomain"),)},
        )
        report = _report((_paper(claims=(with_metric, with_dataset)),), vocabulary, spec=spec)

        assert [i.anchor for i in report.errors if i.rule == "comparative-missing-qualifiers"] == ["^c2"]

    def test_a_comparative_relation_declaring_no_qualifiers_checks_nothing(self, vocabulary: VocabularyIndex):
        spec = _with_qualifiers("outperforms", ())
        claim = _claim(kind="benchmark-result", quote="q", edges=(("outperforms", UnitLink(target="ent.graphrag")),))
        report = _report((_paper(claims=(claim,)),), vocabulary, spec=spec)

        assert "comparative-missing-qualifiers" not in _rules(report)

    def test_a_relation_outside_comparative_relations_checks_nothing(self, vocabulary: VocabularyIndex):
        spec = _with_qualifiers("supports", (("recommended",),))
        claim = _claim(kind="observation", quote="q", edges=(("supports", UnitLink(target="ent.graphrag")),))
        report = _report((_paper(claims=(claim,)),), vocabulary, spec=spec)

        assert "comparative-missing-qualifiers" not in _rules(report)


class TestAnchorUniqueness:
    """Block-id (anchor) collisions within a note are errors."""

    def test_duplicate_anchor_is_an_error(self, vocabulary: VocabularyIndex):
        claims = (_claim(anchor="^c1"), _claim(anchor="^c1", text="Another."))
        report = _report((_paper(claims=claims),), vocabulary)

        assert any(i.rule == "duplicate-anchor" and i.anchor == "^c1" for i in report.errors)

    def test_unique_anchors_pass(self, vocabulary: VocabularyIndex):
        claims = (_claim(anchor="^c1"), _claim(anchor="^c2", text="Another."))
        report = _report((_paper(claims=claims),), vocabulary)

        assert "duplicate-anchor" not in _rules(report)


class TestVocabulary:
    """Unregistered entity_type / relation key / facet value / claim kind are errors."""

    def test_unknown_entity_type_is_an_error(self, vocabulary: VocabularyIndex):
        report = _report((_entity(entity_type="gadget"),), vocabulary)

        assert any(i.rule == "unknown-entity-type" for i in report.errors)

    def test_known_entity_type_passes(self, vocabulary: VocabularyIndex):
        report = _report((_entity(entity_type="technique"),), vocabulary)

        assert "unknown-entity-type" not in _rules(report)

    def test_unknown_claim_kind_is_an_error(self, vocabulary: VocabularyIndex):
        report = _report((_paper(claims=(_claim(kind="hot-take"),)),), vocabulary)

        assert any(i.rule == "unknown-kind" and "registered: " in i.message for i in report.errors)

    def test_known_claim_kind_passes(self, vocabulary: VocabularyIndex):
        report = _report((_paper(claims=(_claim(kind="ablation", quote="q"),)),), vocabulary)

        assert "unknown-kind" not in _rules(report)

    def test_unknown_relation_key_is_an_error(self, vocabulary: VocabularyIndex):
        claim = _claim(kind="observation", quote="q", edges=(("contradicts", UnitLink(target="ent.x")),))
        report = _report((_paper(claims=(claim,)),), vocabulary)

        assert any(i.rule == "unknown-relation" for i in report.errors)

    def test_known_relation_key_passes(self, vocabulary: VocabularyIndex):
        claim = _claim(kind="observation", quote="q", edges=(("supports", UnitLink(target="ent.x")),))
        report = _report((_paper(claims=(claim,)),), vocabulary)

        assert "unknown-relation" not in _rules(report)

    def test_unknown_facet_value_is_an_error(self, vocabulary: VocabularyIndex):
        note = KnowledgeBaseNote(
            frontmatter=parse_frontmatter(
                {
                    "id": "paper.x",
                    "type": "paper",
                    "title": "X",
                    "status": "draft",
                    "paper_type": "method",
                    "year": 2025,
                    "zotero_key": "K",
                    "source_hash": "h",
                    "tasks": ["teleportation"],
                },
                _RESEARCH_SPEC,
            ),
            sections=(),
            claims=(),
            source_path="Memory/Papers/x.md",
        )
        report = _report((note,), vocabulary)

        assert any(i.rule == "unknown-facet-value" and "teleportation" in i.message and "registered: " in i.message for i in report.errors)

    def test_known_facet_value_passes(self, vocabulary: VocabularyIndex):
        note = KnowledgeBaseNote(
            frontmatter=parse_frontmatter(
                {
                    "id": "paper.x",
                    "type": "paper",
                    "title": "X",
                    "status": "draft",
                    "paper_type": "method",
                    "year": 2025,
                    "zotero_key": "K",
                    "source_hash": "h",
                    "tasks": ["long-doc-qa"],
                },
                _RESEARCH_SPEC,
            ),
            sections=(),
            claims=(),
            source_path="Memory/Papers/x.md",
        )
        report = _report((note,), vocabulary)

        assert "unknown-facet-value" not in _rules(report)


class TestSummaryLength:
    """`summary` should be ≤ 400 chars (the core-keys cap); a longer one warns, it never fails the page."""

    def test_oversized_summary_is_a_warning(self, vocabulary: VocabularyIndex):
        report = _report((_paper(summary="x" * 401),), vocabulary)

        assert any(i.rule == "oversized-summary" for i in report.warnings)
        assert not any(i.rule == "oversized-summary" for i in report.errors)

    def test_max_length_summary_passes(self, vocabulary: VocabularyIndex):
        report = _report((_paper(summary="x" * 400),), vocabulary)

        assert "oversized-summary" not in _rules(report)


class TestQuoteCheck:
    """A claim's `quote` must appear in the cited source mirror (whitespace-normalized)."""

    def test_quote_not_in_mirror_is_an_error(self, vocabulary: VocabularyIndex):
        claim = _claim(kind="benchmark-result", quote="a phrase that is absent")
        report = _report((_paper(claims=(claim,)),), vocabulary, mirror_texts={"K": "the mirror says something else entirely"})

        assert any(i.rule == "quote-not-in-source" for i in report.errors)

    def test_quote_in_mirror_passes(self, vocabulary: VocabularyIndex):
        claim = _claim(kind="benchmark-result", quote="present phrase")
        report = _report((_paper(claims=(claim,)),), vocabulary, mirror_texts={"K": "the mirror contains the present phrase indeed"})

        assert "quote-not-in-source" not in _rules(report)

    def test_quote_check_skipped_when_no_mirror_supplied(self, vocabulary: VocabularyIndex):
        # No mirror text (e.g. mirror not yet ingested) → no quote-check error is raised.
        claim = _claim(kind="benchmark-result", quote="anything")
        report = _report((_paper(claims=(claim,)),), vocabulary)

        assert "quote-not-in-source" not in _rules(report)

    def test_multi_snippet_table_claim_all_present_passes(self, vocabulary: VocabularyIndex):
        # Table-derived claim: several quoted snippets in one value; ALL are in the mirror.
        claim = _claim(kind="benchmark-result", quote='"0.7535" vs "0.7463" vs "0.7407"')
        mirror = "Our method scored 0.7535 against 0.7463 and the weaker 0.7407 baseline."
        report = _report((_paper(claims=(claim,)),), vocabulary, mirror_texts={"K": mirror})

        assert "quote-not-in-source" not in _rules(report)

    def test_multi_snippet_claim_with_one_missing_snippet_still_errors(self, vocabulary: VocabularyIndex):
        # The third snippet ("0.7407") is absent → the claim still fails the quote check.
        claim = _claim(kind="benchmark-result", quote='"0.7535" vs "0.7463" vs "0.7407"')
        mirror = "Our method scored 0.7535 against the 0.7463 baseline."
        report = _report((_paper(claims=(claim,)),), vocabulary, mirror_texts={"K": mirror})

        assert any(i.rule == "quote-not-in-source" for i in report.errors)


class TestReportShapeAndSemantics:
    """The report carries counts + an ingest queue; it reports, never filters."""

    def test_report_counts_match_issue_lists(self, vocabulary: VocabularyIndex):
        report = _report((_paper(claims=(_claim(kind=""),)),), vocabulary)

        assert report.n_errors == len(report.errors)
        assert report.n_warnings == len(report.warnings)

    def test_flagged_but_loaded_every_note_still_present(self, vocabulary: VocabularyIndex):
        # An entity with a bad type still appears in the validated set — validate reports,
        # the load stage still upserts it (flagged-but-loaded contract).
        notes = (_entity(entity_type="gadget"), _paper(claims=(_claim(kind="observation"),)))
        report = _report(notes, vocabulary)

        assert report.n_errors >= 1
        assert {i.page_id for i in report.errors} == {"ent.lightrag"}


class TestLintNoteSingleNoteSeam:
    """`validate_note` runs the note-local rule subset over ONE note (the authoring-time seam).

    The CLI (`memory note validate|build`) has no full-knowledge_base link index, so this seam
    resolves the note against itself: every cross-note unit link is unresolved, but the
    resolver classes those as `dangling-link` *warnings* (or the ingest queue), never
    errors — so a vocabulary/grammar-clean note validations clean at single-note level even
    though all its outbound links dangle. The note-local rules (summary length,
    comparative qualifiers, quote-by-kind, anchor uniqueness, vocabulary) still apply.
    """

    def test_clean_note_has_no_errors(self, vocabulary: VocabularyIndex):
        report = validate_note(_paper(claims=(_claim(kind="observation"),)), vocabulary, spec=_RESEARCH_SPEC)

        assert report.n_errors == 0

    def test_oversized_summary_is_a_warning(self, vocabulary: VocabularyIndex):
        report = validate_note(_paper(summary="x" * 401), vocabulary, spec=_RESEARCH_SPEC)

        assert any(i.rule == "oversized-summary" for i in report.warnings)
        assert report.n_errors == 0

    def test_comparative_missing_qualifiers_is_an_error(self, vocabulary: VocabularyIndex):
        claim = _claim(kind="benchmark-result", quote="q", edges=(("outperforms", UnitLink(target="ent.graphrag")),))
        report = validate_note(_paper(claims=(claim,)), vocabulary, spec=_RESEARCH_SPEC)

        assert any(i.rule == "comparative-missing-qualifiers" for i in report.errors)

    def test_cross_note_link_is_a_warning_not_an_error(self, vocabulary: VocabularyIndex):
        # A claim subject pointing at another note dangles (no knowledge base index) → warning, exit-0 tier.
        claim = _claim(kind="observation", subject=UnitLink(target="ent.elsewhere"))
        report = validate_note(_paper(claims=(claim,)), vocabulary, spec=_RESEARCH_SPEC)

        assert report.n_errors == 0
        assert any(i.rule == "dangling-link" for i in report.warnings)

    def test_multiple_dangling_links_collapse_to_one_recommendation(self, vocabulary: VocabularyIndex):
        # Three separate dangling targets on one claim (a subject + two on_dataset links) —
        # single-note scope can't resolve any of them; reporting each individually would read
        # as three separate problems for what is really one structural fact.
        claim = _claim(
            kind="benchmark-result",
            quote="q",
            subject=UnitLink(target="ent.elsewhere"),
            links={"on_dataset": (UnitLink(target="ent.dataset-a"), UnitLink(target="ent.dataset-b"))},
        )
        report = validate_note(_paper(claims=(claim,)), vocabulary, spec=_RESEARCH_SPEC)

        dangling = [i for i in report.warnings if i.rule == "dangling-link"]
        assert len(dangling) == 1
        assert "3" in dangling[0].message
        assert "not checked here" in dangling[0].message
        assert "resolve at index" in dangling[0].message

    def test_no_dangling_links_emits_no_recommendation(self, vocabulary: VocabularyIndex):
        report = validate_note(_paper(claims=(_claim(kind="observation"),)), vocabulary, spec=_RESEARCH_SPEC)

        assert not any(i.rule == "dangling-link" for i in report.warnings)

    def test_mirror_text_enables_the_quote_check(self, vocabulary: VocabularyIndex):
        claim = _claim(kind="benchmark-result", quote="a phrase that is absent")
        report = validate_note(_paper(claims=(claim,)), vocabulary, mirror_text="the mirror says something else entirely", spec=_RESEARCH_SPEC)

        assert any(i.rule == "quote-not-in-source" for i in report.errors)

    def test_without_mirror_text_the_quote_check_is_skipped(self, vocabulary: VocabularyIndex):
        claim = _claim(kind="benchmark-result", quote="anything")
        report = validate_note(_paper(claims=(claim,)), vocabulary, spec=_RESEARCH_SPEC)

        assert "quote-not-in-source" not in _rules(report)


class TestLintVocabularySeam:
    """`validate_vocabulary` runs ONLY the Vocabulary rules over ONE note (the write-time gate seam).

    `IngestPage` refuses a write on these findings before it has a knowledge base to run the full
    single-note validation against, so this seam is narrower than `validate_note`: entity type, facet
    values, claim kinds, and relation keys against an already-indexed vocabulary — nothing that
    needs knowledge-base-wide state (duplicate ids, spec/vocabulary drift, the quote check) or that the
    note grammar already enforces on construction (required fields, anchor uniqueness).
    """

    def test_clean_note_has_no_errors(self, vocabulary: VocabularyIndex):
        report = validate_vocabulary(_paper(claims=(_claim(kind="observation"),)), vocabulary, spec=_RESEARCH_SPEC)

        assert report.n_errors == 0

    def test_unknown_entity_type_is_an_error(self, vocabulary: VocabularyIndex):
        report = validate_vocabulary(_entity(entity_type="gadget"), vocabulary, spec=_RESEARCH_SPEC)

        assert any(i.rule == "unknown-entity-type" for i in report.errors)

    def test_known_entity_type_passes(self, vocabulary: VocabularyIndex):
        report = validate_vocabulary(_entity(entity_type="technique"), vocabulary, spec=_RESEARCH_SPEC)

        assert "unknown-entity-type" not in _rules(report)

    def test_unknown_claim_kind_is_an_error(self, vocabulary: VocabularyIndex):
        report = validate_vocabulary(_paper(claims=(_claim(kind="hot-take"),)), vocabulary, spec=_RESEARCH_SPEC)

        assert any(i.rule == "unknown-kind" for i in report.errors)

    def test_known_claim_kind_passes(self, vocabulary: VocabularyIndex):
        report = validate_vocabulary(_paper(claims=(_claim(kind="ablation"),)), vocabulary, spec=_RESEARCH_SPEC)

        assert "unknown-kind" not in _rules(report)

    def test_unknown_relation_key_is_an_error(self, vocabulary: VocabularyIndex):
        claim = _claim(kind="observation", edges=(("contradicts", UnitLink(target="ent.x")),))
        report = validate_vocabulary(_paper(claims=(claim,)), vocabulary, spec=_RESEARCH_SPEC)

        assert any(i.rule == "unknown-relation" for i in report.errors)

    def test_known_relation_key_passes(self, vocabulary: VocabularyIndex):
        claim = _claim(kind="observation", edges=(("supports", UnitLink(target="ent.x")),))
        report = validate_vocabulary(_paper(claims=(claim,)), vocabulary, spec=_RESEARCH_SPEC)

        assert "unknown-relation" not in _rules(report)

    def test_unknown_facet_value_is_an_error(self, vocabulary: VocabularyIndex):
        note = KnowledgeBaseNote(
            frontmatter=parse_frontmatter(
                {
                    "id": "paper.x",
                    "type": "paper",
                    "title": "X",
                    "status": "draft",
                    "paper_type": "method",
                    "year": 2025,
                    "zotero_key": "K",
                    "source_hash": "h",
                    "tasks": ["teleportation"],
                },
                _RESEARCH_SPEC,
            ),
            sections=(),
            claims=(),
            source_path="Memory/Papers/x.md",
        )
        report = validate_vocabulary(note, vocabulary, spec=_RESEARCH_SPEC)

        assert any(i.rule == "unknown-facet-value" and "teleportation" in i.message and "registered: " in i.message for i in report.errors)

    def test_known_facet_value_passes(self, vocabulary: VocabularyIndex):
        note = KnowledgeBaseNote(
            frontmatter=parse_frontmatter(
                {
                    "id": "paper.x",
                    "type": "paper",
                    "title": "X",
                    "status": "draft",
                    "paper_type": "method",
                    "year": 2025,
                    "zotero_key": "K",
                    "source_hash": "h",
                    "tasks": ["long-doc-qa"],
                },
                _RESEARCH_SPEC,
            ),
            sections=(),
            claims=(),
            source_path="Memory/Papers/x.md",
        )
        report = validate_vocabulary(note, vocabulary, spec=_RESEARCH_SPEC)

        assert "unknown-facet-value" not in _rules(report)

    def test_knowledge_base_wide_and_grammar_rules_are_never_raised_here(self, vocabulary: VocabularyIndex):
        # missing-loc / missing-kind / oversized-summary / comparative-missing-qualifiers /
        # duplicate-anchor / quote-not-in-source all need either knowledge-base-wide state or a grammar
        # violation `AuthoringPayload` already refuses on construction — none is this seam's job.
        claims = (
            _claim(anchor="^c1", loc="", kind="", edges=(("outperforms", UnitLink(target="ent.x")),)),
            _claim(anchor="^c1", text="A duplicate anchor."),
        )
        report = validate_vocabulary(_paper(summary="x" * 281, claims=claims), vocabulary, spec=_RESEARCH_SPEC)

        assert report.n_errors == 0


# ---------------------------------------------------------------------------
# facet controlled-vocabulary check, addressed by key alone
# ---------------------------------------------------------------------------

# A cooking-style KnowledgeBaseSchema: one page type "recipe" with id_prefix "recipe.",
# a facet keyed "technique" whose values the author writes directly under that same key
# (a long-tail extra, since "technique" is not a page type field). The vocabulary vocab lives
# under the facet KEY: vocabulary.facets.get("technique").


def _cooking_spec() -> KnowledgeBaseSchema:
    """A minimal cooking KnowledgeBaseSchema: recipe page_type, a "technique" facet."""
    return KnowledgeBaseSchema(
        slug="cooking",
        purpose="A cooking knowledge base",
        page_types=(PageTypeSpec(page_type="recipe", id_prefix="recipe.", canonical_sections=(CanonicalSection(slug="overview"),)),),
        facets=(FacetSpec(key="technique", seed=("roasting", "frying", "grilling")),),
    )


def _cooking_registry() -> VocabularyIndex:
    """A minimal VocabularyIndex for the cooking knowledge_base: technique facet vocab keyed by 'technique'."""
    return VocabularyIndex(
        entity_types={},
        relations={},
        facets={"technique": {"roasting": "Dry-heat method", "frying": "Oil-bath method", "grilling": "Direct-heat method"}},
        claim_types={},
    )


def _recipe_note(*, technique: tuple[str, ...]) -> KnowledgeBaseNote:
    """A minimal recipe note with the given technique facet values."""
    cooking = _cooking_spec()
    frontmatter = parse_frontmatter(
        {"id": "recipe.pasta-2025", "type": "recipe", "title": "Pasta", "status": "draft", "technique": list(technique)}, cooking
    )
    return KnowledgeBaseNote(frontmatter=frontmatter, sections=(), claims=(), source_path="Memory/Recipes/pasta.md")


def _cooking_report(note: KnowledgeBaseNote) -> ValidationReport:
    """Run validate over a single cooking note with the cooking spec + vocabulary."""
    resolved = resolve((note,))
    return validate(resolved.notes, _cooking_registry(), resolve_result=resolved, spec=_cooking_spec())


class TestFacetVocabByKey:
    """Facets are validated against the vocabulary vocab under the facet KEY alone.

    `_check_facets` iterates `spec.facets` and looks up `vocabulary.facets.get(facet.key)`, reading
    values via `frontmatter.facet_values(facet.key)`.
    """

    def test_registered_technique_value_passes(self):
        # "frying" is in vocabulary.facets["technique"] → no facet error.
        note = _recipe_note(technique=("frying",))
        report = _cooking_report(note)

        assert "unknown-facet-value" not in {i.rule for i in report.errors}

    def test_unregistered_technique_value_is_flagged(self):
        # "deepfrying" is NOT in vocabulary.facets["technique"], so it must produce exactly one
        # unknown-facet-value error mentioning "deepfrying".
        note = _recipe_note(technique=("deepfrying",))
        report = _cooking_report(note)

        assert any(i.rule == "unknown-facet-value" and "deepfrying" in i.message for i in report.errors)

    def test_research_tasks_facet_value_accepted(self, vocabulary: VocabularyIndex):
        note = KnowledgeBaseNote(
            frontmatter=parse_frontmatter(
                {
                    "id": "paper.x",
                    "type": "paper",
                    "title": "X",
                    "status": "draft",
                    "paper_type": "method",
                    "year": 2025,
                    "zotero_key": "K",
                    "source_hash": "h",
                    "tasks": ["long-doc-qa"],
                },
                _RESEARCH_SPEC,
            ),
            sections=(),
            claims=(),
            source_path="Memory/Papers/x.md",
        )
        report = _report((note,), vocabulary)

        assert "unknown-facet-value" not in {i.rule for i in report.errors}

    def test_research_unknown_facet_value_still_flagged(self, vocabulary: VocabularyIndex):
        # An unregistered tasks value must still be flagged.
        note = KnowledgeBaseNote(
            frontmatter=parse_frontmatter(
                {
                    "id": "paper.x",
                    "type": "paper",
                    "title": "X",
                    "status": "draft",
                    "paper_type": "method",
                    "year": 2025,
                    "zotero_key": "K",
                    "source_hash": "h",
                    "tasks": ["teleportation"],
                },
                _RESEARCH_SPEC,
            ),
            sections=(),
            claims=(),
            source_path="Memory/Papers/x.md",
        )
        report = _report((note,), vocabulary)

        assert any(i.rule == "unknown-facet-value" and "teleportation" in i.message and "registered: " in i.message for i in report.errors)


# ---------------------------------------------------------------------------
# facets with no slot machinery are now validated
# ---------------------------------------------------------------------------


class TestSlotlessFacetsNowValidated:
    """`paper_type` carries no slot; `_check_facets` reads it via `frontmatter.facet_values(facet.key)`."""

    def _paper_with_paper_type(self, paper_type: str) -> KnowledgeBaseNote:
        return KnowledgeBaseNote(
            frontmatter=parse_frontmatter(
                {
                    "id": "paper.x",
                    "type": "paper",
                    "title": "X",
                    "status": "draft",
                    "paper_type": paper_type,
                    "year": 2025,
                    "zotero_key": "K",
                    "source_hash": "h",
                },
                _RESEARCH_SPEC,
            ),
            sections=(),
            claims=(),
            source_path="Memory/Papers/x.md",
        )

    def test_registered_paper_type_passes(self, vocabulary: VocabularyIndex) -> None:
        report = _report((self._paper_with_paper_type("method"),), vocabulary)

        assert "unknown-facet-value" not in {i.rule for i in report.errors}

    def test_unregistered_paper_type_is_flagged(self, vocabulary: VocabularyIndex) -> None:
        report = _report((self._paper_with_paper_type("not-a-real-type"),), vocabulary)

        assert any(i.rule == "unknown-facet-value" and "not-a-real-type" in i.message for i in report.errors)


# ---------------------------------------------------------------------------
# facet seed vs mirroring field choices drift (F3)
# ---------------------------------------------------------------------------


def _spec_with_mismatched_doc_type_choices() -> KnowledgeBaseSchema:
    """A KnowledgeBaseSchema whose 'document' page_type's 'doc_type' field choices omit a facet seed value."""
    return KnowledgeBaseSchema(
        slug="docs",
        page_types=(
            PageTypeSpec(page_type="document", frontmatter_fields=(FrontmatterFieldSpec(name="doc_type", type="scalar", choices=("method",)),)),
        ),
        facets=(FacetSpec(key="doc_type", seed=("method", "survey")),),
    )


class TestFacetChoicesDriftSurfacedByValidate:
    """The validate stage folds facet seed/choices mismatches into `ValidationReport.warnings`.

    Recommendation by design, same as spec-vocabulary-drift: a mismatch must surface so it cannot pass
    unnoticed, but never fails indexing.
    """

    def test_mismatch_appears_as_a_warning_not_an_error(self, vocabulary: VocabularyIndex):
        report = validate((), vocabulary, spec=_spec_with_mismatched_doc_type_choices())

        drift_warnings = [issue for issue in report.warnings if issue.rule == "facet-choices-drift"]
        assert drift_warnings
        assert all(issue.severity == "warning" for issue in drift_warnings)
        assert not any(issue.rule == "facet-choices-drift" for issue in report.errors)
        assert any("survey" in issue.message for issue in drift_warnings)

    def test_consistent_seed_and_choices_yield_no_drift_warning(self, vocabulary: VocabularyIndex):
        spec = KnowledgeBaseSchema(
            slug="docs",
            page_types=(
                PageTypeSpec(
                    page_type="document",
                    frontmatter_fields=(FrontmatterFieldSpec(name="doc_type", type="scalar", choices=("method", "survey")),),
                ),
            ),
            facets=(FacetSpec(key="doc_type", seed=("method", "survey")),),
        )

        report = validate((), vocabulary, spec=spec)

        assert not any(issue.rule == "facet-choices-drift" for issue in report.warnings)


class TestDanglingSourceRef:
    """A page citing a source key with no vault mirror is a `dangling-source-ref` WARNING."""

    def test_missing_mirror_is_a_warning(self, vocabulary: VocabularyIndex):
        resolved = resolve((_paper(),))

        report = validate(resolved.notes, vocabulary, resolve_result=resolved, knowledge_base_source_keys=frozenset(), spec=_RESEARCH_SPEC)

        issue = next(i for i in report.warnings if i.rule == "dangling-source-ref")
        assert issue.severity == "warning"
        assert issue.page_id == "paper.lightrag-2025"
        assert "'K'" in issue.message

    def test_present_mirror_passes(self, vocabulary: VocabularyIndex):
        resolved = resolve((_paper(),))

        report = validate(resolved.notes, vocabulary, resolve_result=resolved, knowledge_base_source_keys=frozenset({"K"}), spec=_RESEARCH_SPEC)

        assert "dangling-source-ref" not in _rules(report)

    def test_omitted_universe_skips_the_check(self, vocabulary: VocabularyIndex):
        report = _report((_paper(),), vocabulary)

        assert "dangling-source-ref" not in _rules(report)

    def test_a_page_citing_no_source_passes(self, vocabulary: VocabularyIndex):
        resolved = resolve((_entity(),))

        report = validate(resolved.notes, vocabulary, resolve_result=resolved, knowledge_base_source_keys=frozenset(), spec=_RESEARCH_SPEC)

        assert "dangling-source-ref" not in _rules(report)


class TestTheSourceKeyFieldComesFromTheSpec:
    """Every source read in this stage goes through `spec.sources.key_field`, never a fixed field name.

    A knowledge base declaring a key field of its own gets quote verification and `dangling-source-ref` on
    that field's value, and a knowledge base declaring none cites nothing at all.
    """

    def test_the_declared_field_drives_the_dangling_source_ref_warning(self, vocabulary: VocabularyIndex):
        note = _document(source_path="Sources/handbook.md")
        resolved = resolve((note,))

        report = validate(resolved.notes, vocabulary, resolve_result=resolved, knowledge_base_source_keys=frozenset(), spec=_PATH_KEYED_SPEC)

        issue = next(i for i in report.warnings if i.rule == "dangling-source-ref")
        assert issue.page_id == "doc.handbook"
        assert "'Sources/handbook.md'" in issue.message

    def test_a_mirror_under_the_declared_field_passes(self, vocabulary: VocabularyIndex):
        note = _document(source_path="Sources/handbook.md")
        resolved = resolve((note,))

        report = validate(
            resolved.notes, vocabulary, resolve_result=resolved, knowledge_base_source_keys=frozenset({"Sources/handbook.md"}), spec=_PATH_KEYED_SPEC
        )

        assert "dangling-source-ref" not in _rules(report)

    def test_another_knowledge_base_own_key_field_is_not_read(self, vocabulary: VocabularyIndex):
        """A value under some other knowledge base's key field is a long-tail extra here, not a citation."""
        note = _document(zotero_key="K")
        resolved = resolve((note,))

        report = validate(resolved.notes, vocabulary, resolve_result=resolved, knowledge_base_source_keys=frozenset(), spec=_PATH_KEYED_SPEC)

        assert "dangling-source-ref" not in _rules(report)

    def test_a_knowledge_base_declaring_no_key_field_cites_no_source(self, vocabulary: VocabularyIndex):
        note = _document(source_path="Sources/handbook.md")
        resolved = resolve((note,))

        report = validate(resolved.notes, vocabulary, resolve_result=resolved, knowledge_base_source_keys=frozenset(), spec=_KEYLESS_SPEC)

        assert "dangling-source-ref" not in _rules(report)

    def test_quote_verification_keys_the_mirror_by_the_declared_field(self, vocabulary: VocabularyIndex):
        note = _document(source_path="Sources/handbook.md", claims=(_claim(quote="a verbatim line"),))
        resolved = resolve((note,))

        report = validate(
            resolved.notes,
            vocabulary,
            mirror_texts={"Sources/handbook.md": "before\na verbatim line\nafter"},
            resolve_result=resolved,
            spec=_PATH_KEYED_SPEC,
        )

        assert "quote-not-in-source" not in _rules(report)

    def test_a_quote_absent_from_the_mirror_the_declared_field_names_is_an_error(self, vocabulary: VocabularyIndex):
        note = _document(source_path="Sources/handbook.md", claims=(_claim(quote="never written"),))
        resolved = resolve((note,))

        report = validate(
            resolved.notes,
            vocabulary,
            mirror_texts={"Sources/handbook.md": "before\na verbatim line\nafter"},
            resolve_result=resolved,
            spec=_PATH_KEYED_SPEC,
        )

        assert any(i.rule == "quote-not-in-source" for i in report.errors)

    def test_a_page_naming_its_mirror_only_through_the_digest_link_field_has_its_quotes_checked(self, vocabulary: VocabularyIndex):
        note = _document(derived_from="Sources/handbook.md", claims=(_claim(quote="never written"),))
        resolved = resolve((note,))

        report = validate(
            resolved.notes,
            vocabulary,
            mirror_texts={"Sources/handbook.md": "before\na verbatim line\nafter"},
            resolve_result=resolved,
            spec=_PATH_KEYED_SPEC,
        )

        assert any(i.rule == "quote-not-in-source" for i in report.errors)

    def test_a_quote_the_digest_link_fields_mirror_reads_passes(self, vocabulary: VocabularyIndex):
        note = _document(derived_from="Sources/handbook.md", claims=(_claim(quote="a verbatim line"),))
        resolved = resolve((note,))

        report = validate(
            resolved.notes,
            vocabulary,
            mirror_texts={"Sources/handbook.md": "before\na verbatim line\nafter"},
            resolve_result=resolved,
            spec=_PATH_KEYED_SPEC,
        )

        assert "quote-not-in-source" not in _rules(report)

    def test_the_key_field_names_the_mirror_before_the_digest_link_field(self, vocabulary: VocabularyIndex):
        note = _document(source_path="Sources/handbook.md", derived_from="Sources/other.md", claims=(_claim(quote="a verbatim line"),))
        resolved = resolve((note,))

        report = validate(
            resolved.notes,
            vocabulary,
            mirror_texts={"Sources/handbook.md": "a verbatim line", "Sources/other.md": "something else"},
            resolve_result=resolved,
            spec=_PATH_KEYED_SPEC,
        )

        assert "quote-not-in-source" not in _rules(report)

    def test_validate_note_checks_a_quote_against_the_digest_link_fields_mirror(self, vocabulary: VocabularyIndex):
        note = _document(derived_from="Sources/handbook.md", claims=(_claim(quote="never written"),))

        report = validate_note(note, vocabulary, mirror_text="before\na verbatim line\nafter", spec=_PATH_KEYED_SPEC)

        assert any(i.rule == "quote-not-in-source" for i in report.errors)

    def test_validate_note_keys_its_single_mirror_by_the_declared_field(self, vocabulary: VocabularyIndex):
        note = _document(source_path="Sources/handbook.md", claims=(_claim(quote="a verbatim line"),))

        report = validate_note(note, vocabulary, mirror_text="before\na verbatim line\nafter", spec=_PATH_KEYED_SPEC)

        assert "quote-not-in-source" not in _rules(report)


class TestMirrorWithoutProvider:
    """A `Sources/` mirror naming no `provider` is an error attributed to its own knowledge base path."""

    def _mirror(self, path: str, frontmatter: str) -> MirrorDoc:
        return MirrorDoc.from_text(path, f"---\n{frontmatter}\n---\n\nbody\n", "research")

    def test_a_mirror_naming_no_provider_is_one_error(self) -> None:
        mirror = self._mirror("Sources/bare.md", "source_key: A")

        (issue,) = mirror_provider_issues((mirror,))

        assert issue.severity == "error"
        assert issue.rule == "mirror-without-provider"
        assert issue.page_id == "Sources/bare.md"
        assert "provider" in issue.message

    def test_a_mirror_naming_a_provider_is_no_issue(self) -> None:
        mirror = self._mirror("Sources/keyed.md", "source_key: A\nprovider: example-library")

        assert mirror_provider_issues((mirror,)) == []

    def test_one_issue_per_offending_mirror_naming_each_path(self) -> None:
        mirrors = (
            self._mirror("Sources/one.md", "source_key: A"),
            self._mirror("Sources/two.md", "source_key: B\nprovider: example-library"),
            self._mirror("Sources/three.md", "source_key: C"),
        )

        issues = mirror_provider_issues(mirrors)

        assert [issue.page_id for issue in issues] == ["Sources/one.md", "Sources/three.md"]

    def test_no_mirrors_is_no_issue(self) -> None:
        assert mirror_provider_issues(()) == []
