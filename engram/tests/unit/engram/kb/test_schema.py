"""Unit tests for `src/engram/memory/spec.py`.

Tests cover:
  1. Round-trip (JSON serialise → parse → equal)
  2. research_spec() (the research bundle) loads with 12 page types + correct prefix_by_page_type
  3. sections_for / canon_set_for per page_type
  4. edge_fields_for / link_fields_for per page_type
  5. required_fields_for("paper")
  6. ClaimGrammarSpec fields match the literal claim-grammar constants
  7. Validator rejects invalid specs (5 sub-cases)
  8. Research-vocab fidelity: vendored vocabulary fixture round-trips through parse_vocabulary
  9. model_json_schema() has page_types under properties
  10. resolve_schema / load_knowledge_base_schema refuse a missing spec (KnowledgeBaseSchemaMissingError)
"""

from __future__ import annotations

import re

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pydantic import ValidationError

from engram.kb.contracts import (
    CanonicalSection,
    ClaimGrammarSpec,
    FacetSpec,
    KnowledgeBaseSchema,
    KnowledgeBaseSchemaMissingError,
    PageTypeSpec,
    RelationDecl,
    SectionSummarySpec,
    SourcesSpec,
    load_knowledge_base_schema,
    resolve_schema,
)
from engram.kb.domain.schema import EmbedStrategiesSpec, RetrievalSpec
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from engram.kb.domain.vocabulary import VocabularyParseResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "research_vocabulary"


def _minimal_spec(**overrides: object) -> KnowledgeBaseSchema:
    """Build the smallest valid KnowledgeBaseSchema for negative-test cases."""
    claim_grammar = ClaimGrammarSpec(section="findings")
    base: dict[str, object] = {
        "slug": "test",
        "page_types": (PageTypeSpec(page_type="paper", id_prefix="paper.", claims_enabled=True),),
        "claims": claim_grammar,
    }
    base.update(overrides)
    return KnowledgeBaseSchema.model_validate(base)


# ---------------------------------------------------------------------------
# Test 1 — Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_json_roundtrip_equals_original(self) -> None:
        spec = _minimal_spec()
        dumped = spec.model_dump_json()
        restored = KnowledgeBaseSchema.from_json(dumped)
        assert restored == spec

    def test_from_json_handles_research_spec_json(self) -> None:
        spec = research_spec()
        restored = KnowledgeBaseSchema.from_json(spec.model_dump_json())
        assert restored == spec


class TestCanonicalSectionCompactSerialization:
    """A headingless `canonical_sections` entry re-serializes as a bare string, not `{slug, heading: null}`.

    The inverse of the bare-slug acceptance: a `knowledge-base-schema.json` written by a headingless page type
    round-trips through `model_dump`/`model_dump_json` byte-stable, and only entries that actually
    declare a heading pay for the object form. Every write path (`init.scaffold.scaffold`,
    `studio.knowledge_base.cli_init`, `evals.recommendation.domain.recommendation.apply_op`) calls one of these two dump methods on
    the whole spec, so this one serializer is what keeps all three from inflating an untouched
    page type's sections into object form.
    """

    def _spec(self, canonical_sections: tuple[object, ...]) -> KnowledgeBaseSchema:
        return KnowledgeBaseSchema.model_validate(
            {"slug": "test", "page_types": [{"page_type": "note", "canonical_sections": canonical_sections}]}
        )

    def test_a_headingless_entry_dumps_as_a_bare_string(self) -> None:
        spec = self._spec(("tl_dr",))

        assert spec.model_dump()["page_types"][0]["canonical_sections"] == ("tl_dr",)

    def test_a_headed_entry_dumps_as_an_object(self) -> None:
        spec = self._spec((CanonicalSection(slug="tl_dr", heading="TL;DR"),))

        assert spec.model_dump()["page_types"][0]["canonical_sections"] == ({"slug": "tl_dr", "heading": "TL;DR"},)

    def test_mixed_entries_each_serialize_independently(self) -> None:
        spec = self._spec(("context", CanonicalSection(slug="tl_dr", heading="TL;DR"), "findings"))

        assert spec.model_dump()["page_types"][0]["canonical_sections"] == (
            "context",
            {"slug": "tl_dr", "heading": "TL;DR"},
            "findings",
        )

    def test_json_mode_matches_python_mode(self) -> None:
        spec = self._spec(("context", CanonicalSection(slug="tl_dr", heading="TL;DR")))

        assert spec.model_dump(mode="json")["page_types"][0]["canonical_sections"] == [
            "context",
            {"slug": "tl_dr", "heading": "TL;DR"},
        ]

    def test_a_bare_string_knowledge_base_schema_round_trips_byte_stable(self) -> None:
        """A legacy or untouched-by-this-diff spec, re-dumped, is byte-identical to how it was written."""
        spec = self._spec(("tl_dr", "context"))

        assert KnowledgeBaseSchema.from_json(spec.model_dump_json()).model_dump_json() == spec.model_dump_json()

    def test_research_spec_grounds_the_same_way(self) -> None:
        """The bundled research spec declares `tl_dr`'s heading; its siblings stay bare strings."""
        spec = research_spec()

        dumped = spec.model_dump()["page_types"][0]["canonical_sections"]
        assert dumped[0] == {"slug": "tl_dr", "heading": "TL;DR"}
        assert dumped[1] == "context"


# ---------------------------------------------------------------------------
# Test 2 — research_spec() loads correctly
# ---------------------------------------------------------------------------


class TestResearchSpec:
    def test_has_thirteen_page_types(self) -> None:
        spec = research_spec()
        assert len(spec.page_types) == 13

    def test_prefix_by_page_type(self) -> None:
        spec = research_spec()
        expected = {
            "paper": "paper.",
            "talk": "talk.",
            "article": "art.",
            "post": "post.",
            "flag": "flag.",
            "stance": "stance.",
            "entity": "ent.",
            "concept": "con.",
            "comparison": "cmp.",
            "playbook": "pb.",
            "question": "q.",
            "moc": "moc.",
            "meta": "meta.",
        }
        assert dict(spec.prefix_by_page_type) == expected

    def test_slug_is_research(self) -> None:
        spec = research_spec()
        assert spec.slug == "research"

    def test_sources_enabled(self) -> None:
        spec = research_spec()
        assert spec.sources.enabled is True
        assert spec.sources.quote_verify is True

    def test_retrieval_profile_full(self) -> None:
        spec = research_spec()
        assert spec.retrieval.profile == "full"

    def test_research_curates_its_map_to_the_navigation_layer(self) -> None:
        spec = research_spec()
        assert spec.retrieval.disclosure_mode == "strict"
        # The flood page_types stay searchable and fetchable, and the meta scaffolding is contract,
        # not content; only map listings drop them.
        assert spec.disclosure_excluded_page_types() == frozenset({"entity", "paper", "meta", "talk", "article", "post"})

    def test_disclosure_mode_rejects_unknown_values(self) -> None:
        with pytest.raises(ValidationError):
            RetrievalSpec(disclosure_mode="auto")  # pyright: ignore[reportArgumentType]  # the rejection under test

    def test_group_by_rejects_keys_the_sql_does_not_read(self) -> None:
        with pytest.raises(ValidationError):
            RetrievalSpec(group_by="team")  # pyright: ignore[reportArgumentType]  # the rejection under test

    def test_group_by_accepts_kind(self) -> None:
        spec = RetrievalSpec(group_by="page_type")
        assert spec.group_by == "page_type"

    def test_auto_mode_accepts_a_floor_within_the_ceiling(self) -> None:
        spec = RetrievalSpec(mode="auto", floor=("embeddings",), ceiling=("embeddings", "sql"))
        assert spec.floor == ("embeddings",)
        assert spec.ceiling == ("embeddings", "sql")

    def test_floor_outside_ceiling_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be a subset"):
            RetrievalSpec(mode="auto", floor=("graph",), ceiling=("embeddings",))

    def test_disclosure_excluded_page_types_collects_undisclosed(self) -> None:
        spec = research_spec()
        page_types = tuple(
            pt.model_copy(update={"disclosed": False}) if pt.page_type == "question" else pt for pt in spec.page_types
        )
        undisclosing = spec.model_copy(update={"page_types": page_types})
        assert undisclosing.disclosure_excluded_page_types() == frozenset(
            {"entity", "paper", "meta", "talk", "article", "post", "question"}
        )

    def test_kinds_cached_property_same_object(self) -> None:
        """Verify @cached_property on a frozen pydantic model works without raising."""
        spec = research_spec()
        first = spec.page_types
        second = spec.page_types
        assert first is second  # cached — same object identity

    def test_section_summaries_defaults_reproduce_sensible_behavior(self) -> None:
        spec = research_spec()
        assert spec.section_summaries.leading_sentences == 2
        assert spec.section_summaries.inline_gate_chars == 1000


class TestSectionSummarySpec:
    """`SectionSummarySpec` — the knowledge-base-level config for the derived section summary."""

    def test_defaults(self) -> None:
        spec = SectionSummarySpec()
        assert spec.leading_sentences == 2
        assert spec.inline_gate_chars == 1000

    def test_a_knowledge_base_may_override_both_fields(self) -> None:
        spec = _minimal_spec(section_summaries=SectionSummarySpec(leading_sentences=3, inline_gate_chars=500))
        assert spec.section_summaries.leading_sentences == 3
        assert spec.section_summaries.inline_gate_chars == 500

    def test_frozen(self) -> None:
        spec = SectionSummarySpec()
        with pytest.raises(ValidationError):
            spec.leading_sentences = 5  # pyright: ignore[reportAttributeAccessIssue]  # the rejection under test


class TestPathRoot:
    """`KnowledgeBaseSchema.path_root` — the filesystem base `path_fields` values resolve against."""

    def test_defaults_to_parent(self) -> None:
        spec = _minimal_spec()
        assert spec.path_root == "parent"

    def test_accepts_knowledge_base(self) -> None:
        spec = _minimal_spec(path_root="knowledge_base")
        assert spec.path_root == "knowledge_base"

    def test_rejects_values_the_compile_wiring_does_not_read(self) -> None:
        with pytest.raises(ValidationError):
            _minimal_spec(path_root="repo")  # not "parent" or "knowledge_base"


# ---------------------------------------------------------------------------
# Test 3 — sections_for / canon_set_for per page_type
# ---------------------------------------------------------------------------


class TestSectionsFor:
    def test_paper_sections_ordered(self) -> None:
        spec = research_spec()
        assert spec.sections_for("paper") == (
            "tl_dr",
            "context",
            "approach",
            "evaluation_setup",
            "findings",
            "limitations",
            "practical_notes",
        )

    def test_entity_sections_ordered(self) -> None:
        spec = research_spec()
        assert spec.sections_for("entity") == ("tl_dr", "definition", "mechanism", "variants", "evidence", "trade_offs", "open_questions")

    def test_concept_sections_ordered(self) -> None:
        spec = research_spec()
        assert spec.sections_for("concept") == ("tl_dr", "overview", "themes", "state_of_evidence", "open_questions")

    def test_comparison_sections_ordered(self) -> None:
        spec = research_spec()
        assert spec.sections_for("comparison") == ("tl_dr", "scope", "matrix", "analysis", "verdict")

    def test_playbook_sections_ordered(self) -> None:
        spec = research_spec()
        assert spec.sections_for("playbook") == ("tl_dr", "applies_when", "recommendation", "recipe", "evidence", "caveats", "alternatives")

    def test_question_sections_ordered(self) -> None:
        spec = research_spec()
        assert spec.sections_for("question") == ("tl_dr", "why_it_matters", "current_evidence", "candidate_answers")

    def test_moc_sections_ordered(self) -> None:
        spec = research_spec()
        assert spec.sections_for("moc") == ("tl_dr", "map")

    def test_meta_sections_empty(self) -> None:
        spec = research_spec()
        assert spec.sections_for("meta") == ()

    def test_unknown_kind_returns_empty(self) -> None:
        spec = research_spec()
        assert spec.sections_for("nonexistent") == ()

    def test_canon_set_equals_frozenset_of_sections(self) -> None:
        spec = research_spec()
        assert spec.canon_set_for("paper") == frozenset(spec.sections_for("paper"))

    def test_canon_set_unknown_kind_is_empty(self) -> None:
        spec = research_spec()
        assert spec.canon_set_for("nonexistent") == frozenset()


# ---------------------------------------------------------------------------
# Test 4 — edge_fields_for / link_fields_for
# ---------------------------------------------------------------------------


class TestEdgeAndLinkFields:
    def test_paper_edge_fields(self) -> None:
        spec = research_spec()
        assert spec.edge_fields_for("paper") == frozenset({"proposes", "evaluates", "uses_datasets", "builds_on", "part_of"})

    def test_entity_edge_fields(self) -> None:
        spec = research_spec()
        assert spec.edge_fields_for("entity") == frozenset(
            {"is_a", "variant_of", "extends", "composes_with", "alternative_to", "requires", "addresses", "applies_to", "includes"}
        )

    def test_concept_edge_fields(self) -> None:
        spec = research_spec()
        assert spec.edge_fields_for("concept") == frozenset({"synthesizes"})

    def test_comparison_edge_fields(self) -> None:
        spec = research_spec()
        assert spec.edge_fields_for("comparison") == frozenset({"compares", "synthesizes"})

    def test_playbook_edge_fields(self) -> None:
        spec = research_spec()
        assert spec.edge_fields_for("playbook") == frozenset({"recommends", "advises_against", "synthesizes"})

    def test_question_edge_fields(self) -> None:
        spec = research_spec()
        assert spec.edge_fields_for("question") == frozenset({"answered_by"})

    def test_moc_edge_fields_empty(self) -> None:
        spec = research_spec()
        assert spec.edge_fields_for("moc") == frozenset()

    def test_meta_edge_fields_empty(self) -> None:
        spec = research_spec()
        assert spec.edge_fields_for("meta") == frozenset()

    def test_link_fields_for_question_includes_answered_by(self) -> None:
        """answered_by is a link (not link-list) — link_fields_for must include it."""
        spec = research_spec()
        assert "answered_by" in spec.link_fields_for("question")

    def test_link_fields_for_paper_includes_all_link_list_fields(self) -> None:
        spec = research_spec()
        assert spec.link_fields_for("paper") >= frozenset({"proposes", "evaluates", "uses_datasets", "builds_on"})


# ---------------------------------------------------------------------------
# Test 5 — required_fields_for("paper")
# ---------------------------------------------------------------------------


class TestRequiredFields:
    def test_paper_required_fields(self) -> None:
        spec = research_spec()
        assert spec.required_fields_for("paper") == frozenset({"paper_type", "year", "zotero_key", "source_hash"})

    def test_entity_has_entity_type_required(self) -> None:
        spec = research_spec()
        assert "entity_type" in spec.required_fields_for("entity")

    def test_meta_has_no_required_fields(self) -> None:
        spec = research_spec()
        assert spec.required_fields_for("meta") == frozenset()


# ---------------------------------------------------------------------------
# Test 6 — claims ClaimGrammarSpec fields match parser constants
# ---------------------------------------------------------------------------


class TestClaimsGrammar:
    def test_claims_present_on_research_spec(self) -> None:
        spec = research_spec()
        assert spec.claims is not None

    def test_claims_section(self) -> None:
        spec = research_spec()
        assert spec.claims is not None
        assert spec.claims.section == "findings"

    def test_edge_relations(self) -> None:
        spec = research_spec()
        assert spec.claims is not None
        assert set(spec.claims.edge_relations) == {"supports", "refutes", "replicates", "outperforms", "comparable_to"}

    def test_multilink_fields(self) -> None:
        spec = research_spec()
        assert spec.claims is not None
        assert set(spec.claims.multilink_fields) == {"on_dataset", "metric", "disputed_by"}

    def test_link_scalar_fields(self) -> None:
        spec = research_spec()
        assert spec.claims is not None
        assert set(spec.claims.link_scalar_fields) == {"subject", "on_task"}

    def test_text_scalar_fields(self) -> None:
        spec = research_spec()
        assert spec.claims is not None
        assert set(spec.claims.text_scalar_fields) == {"condition", "baseline", "magnitude", "eval_setting", "derived", "status"}

    def test_plain_fields(self) -> None:
        spec = research_spec()
        assert spec.claims is not None
        assert set(spec.claims.plain_fields) == {"loc", "quote", "kind"}

    def test_quote_required_types(self) -> None:
        spec = research_spec()
        assert spec.claims is not None
        assert set(spec.claims.quote_required_types) == {"benchmark-result", "ablation"}

    def test_comparative_relations(self) -> None:
        spec = research_spec()
        assert spec.claims is not None
        assert set(spec.claims.comparative_relations) == {"outperforms", "comparable_to"}


class TestRelationQualifiers:
    """A relation's qualifiers are groups of claim field names, and a bare name is a group of one."""

    def test_a_bare_name_is_a_group_of_one(self) -> None:
        relation = RelationDecl.model_validate({"slug": "outperforms", "domain": "a", "range": "b", "qualifiers": ["metric"]})

        assert relation.qualifiers == (("metric",),)

    def test_bare_names_load_as_one_group_each(self) -> None:
        relation = RelationDecl.model_validate({"slug": "outperforms", "domain": "a", "range": "b", "qualifiers": ["on_dataset", "metric"]})

        assert relation.qualifiers == (("on_dataset",), ("metric",))

    def test_groups_load_as_written(self) -> None:
        relation = RelationDecl.model_validate(
            {"slug": "outperforms", "domain": "a", "range": "b", "qualifiers": [["on_dataset", "on_task"], ["metric"]]}
        )

        assert relation.qualifiers == (("on_dataset", "on_task"), ("metric",))

    def test_no_qualifiers_is_no_groups(self) -> None:
        relation = RelationDecl.model_validate({"slug": "supports", "domain": "a", "range": "b"})

        assert relation.qualifiers == ()

    def test_groups_survive_a_json_round_trip(self) -> None:
        relation = RelationDecl(slug="outperforms", domain="a", range="b", qualifiers=(("on_dataset", "on_task"), ("metric",)))

        assert RelationDecl.model_validate_json(relation.model_dump_json()) == relation

    def test_research_comparative_relations_require_a_dataset_or_task_and_a_metric(self) -> None:
        relations = {relation.slug: relation for relation in research_spec().relations}

        assert relations["outperforms"].qualifiers == (("on_dataset", "on_task"), ("metric",))
        assert relations["comparable_to"].qualifiers == (("on_dataset", "on_task"), ("metric",))

    def test_research_evidential_relations_require_no_field(self) -> None:
        relations = {relation.slug: relation for relation in research_spec().relations}

        assert [relations[slug].qualifiers for slug in ("supports", "refutes", "replicates")] == [(), (), ()]


class TestClaimRelationNames:
    """A comparative relation's qualifiers name declared claim link fields, and no edge relation takes a built-in claim name."""

    def _spec(self, *, qualifiers: tuple[tuple[str, ...], ...], comparative: tuple[str, ...] = ("governs",)) -> KnowledgeBaseSchema:
        return _minimal_spec(
            relations=(RelationDecl(slug="governs", domain="paper", range="paper", qualifiers=qualifiers),),
            claims=ClaimGrammarSpec(
                section="findings",
                edge_relations=("governs",),
                multilink_fields=("applies_to",),
                link_scalar_fields=("governed_by",),
                plain_fields=("court",),
                comparative_relations=comparative,
            ),
        )

    def test_a_comparative_relation_requiring_declared_link_fields_loads(self) -> None:
        spec = self._spec(qualifiers=(("applies_to", "governed_by"),))

        assert spec.relations[0].qualifiers == (("applies_to", "governed_by"),)

    def test_a_comparative_relation_requiring_a_field_that_is_no_link_field_is_refused(self) -> None:
        with pytest.raises(ValidationError) as refusal:
            self._spec(qualifiers=(("applies_to",), ("court",)))

        assert (
            "relation 'governs' qualifier 'court' is not a declared claim link field. "
            "Declare it in multilink_fields or link_scalar_fields of the claims block, or remove it from the relation's qualifiers. "
            "Declared link fields: applies_to, governed_by"
        ) in str(refusal.value)

    def test_a_comparative_relation_requiring_an_undeclared_name_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="qualifier 'jurisdiction' is not a declared claim link field"):
            self._spec(qualifiers=(("jurisdiction",),))

    def test_a_relation_that_is_not_comparative_keeps_any_qualifier_name(self) -> None:
        spec = self._spec(qualifiers=(("recommended",),), comparative=())

        assert spec.relations[0].qualifiers == (("recommended",),)

    @pytest.mark.parametrize("name", ["loc", "quote", "kind", "subject", "status", "disputed_by", "text", "anchor"])
    def test_an_edge_relation_with_a_built_in_claim_name_is_refused(self, name: str) -> None:
        with pytest.raises(ValidationError) as refusal:
            _minimal_spec(claims=ClaimGrammarSpec(section="findings", edge_relations=("governs", name)))

        assert (
            f"edge relation {name!r} is a built-in claim name. Rename the relation. "
            "Built-in claim names: anchor, disputed_by, kind, loc, quote, status, subject, text"
        ) in str(refusal.value)


class TestDeclaredClaimFields:
    """The declared text and link fields are the grammar's field lists in order, without the built-in claim fields."""

    def test_research_declared_fields_leave_out_the_built_in_fields(self) -> None:
        claims = research_spec().claims
        assert claims is not None

        assert claims.link_fields == ("on_dataset", "metric", "on_task")
        assert claims.text_fields == ("condition", "baseline", "magnitude", "eval_setting", "derived")
        assert claims.link_scalar_names == frozenset({"on_task"})

    def test_a_grammar_of_only_built_in_fields_declares_none(self) -> None:
        grammar = ClaimGrammarSpec(
            section="findings",
            edge_relations=("supports", "refutes"),
            multilink_fields=("disputed_by",),
            link_scalar_fields=("subject",),
            text_scalar_fields=("condition", "status"),
            plain_fields=("loc", "quote", "kind"),
        )

        assert grammar.link_fields == ()
        assert grammar.text_fields == ("condition",)
        assert grammar.link_scalar_names == frozenset()

    def test_a_name_is_declared_once_with_the_first_meaning_the_parser_gives_it(self) -> None:
        grammar = ClaimGrammarSpec(
            section="findings",
            edge_relations=("governs",),
            multilink_fields=("applies_to", "governs"),
            link_scalar_fields=("applies_to", "governed_by"),
            text_scalar_fields=("governed_by", "jurisdiction"),
            plain_fields=("jurisdiction",),
        )

        assert grammar.link_fields == ("applies_to", "governed_by")
        assert grammar.text_fields == ("jurisdiction",)
        assert grammar.link_scalar_names == frozenset({"governed_by"})

    def test_the_refusal_names_the_key_the_built_in_fields_the_declared_fields_and_the_edge_relations(self) -> None:
        grammar = ClaimGrammarSpec(
            section="findings", edge_relations=("governs",), multilink_fields=("applies_to",), plain_fields=("court",)
        )

        refusal = grammar.undeclared_field_refusal("mood")

        assert "'mood'" in refusal
        assert "Built-in fields: disputed_by, kind, loc, quote, status, subject" in refusal
        assert "Declared fields: applies_to, court" in refusal
        assert "Edge relations: governs" in refusal

    def test_a_grammar_of_only_built_in_fields_still_names_them_in_the_refusal(self) -> None:
        grammar = ClaimGrammarSpec(section="findings", plain_fields=("loc", "quote", "kind"))

        refusal = grammar.undeclared_field_refusal("qoute")

        assert refusal == (
            "field 'qoute' is not declared by the knowledge base's claim grammar. "
            "Declare it in the claims block of knowledge-base-schema.json or remove it. "
            "Built-in fields: disputed_by, kind, loc, quote, status, subject. Declared fields: none. Edge relations: none"
        )


# ---------------------------------------------------------------------------
# Test — SourcesSpec / facets address by key alone
# ---------------------------------------------------------------------------


class TestSourcesSpec:
    def test_disabled_with_no_connectors_is_valid(self) -> None:
        spec = SourcesSpec()

        assert spec.connectors == ()
        assert spec.connector == ""

    def test_enabled_with_no_connectors_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="connectors"):
            SourcesSpec(enabled=True)

    def test_enabled_with_a_declared_connector_is_valid(self) -> None:
        spec = SourcesSpec(
            enabled=True,
            connectors=("zotero",),
            key_field="zotero_key",
            digest_link_field="digest_of",
            hash_field="source_hash",
            digest_types={"paper": "paper"},
        )

        assert spec.connector == "zotero"


class TestRetrievalSpecPrivate:
    """`RetrievalSpec.private`, whether this knowledge base keeps out of a host's library over several knowledge bases."""

    def test_defaults_to_false(self) -> None:
        assert RetrievalSpec().private is False

    def test_true_parses(self) -> None:
        assert RetrievalSpec.model_validate({"private": True}).private is True

    def test_neither_key_parses_as_shared(self) -> None:
        assert RetrievalSpec.model_validate({"collection": "legal"}).private is False


class TestRetrievalSpecRefusesTheOldPrivacyKey:
    """A `library` key is refused, never dropped into the default `private: False`, which would read as shared."""

    @pytest.mark.parametrize("value", [False, True])
    def test_a_spec_carrying_library_is_refused_naming_the_rewrite(self, value: bool) -> None:
        with pytest.raises(ValidationError, match=r"studio\.migrations\.wave4"):
            RetrievalSpec.model_validate({"collection": "legal", "library": value})

    def test_a_schema_carrying_it_under_retrieval_is_refused_too(self) -> None:
        with pytest.raises(ValidationError, match=r"studio\.migrations\.wave4"):
            KnowledgeBaseSchema.from_json(
                '{"slug": "legal", "page_types": [{"page_type": "note", "id_prefix": "note."}], "retrieval": {"library": false}}'
            )


class TestCollectionDefaultsToSlug:
    """`RetrievalSpec.collection` left unset takes the owning `KnowledgeBaseSchema.slug`, never a literal name."""

    def test_no_retrieval_key_at_all_takes_the_slug(self) -> None:
        spec = KnowledgeBaseSchema(slug="legal", page_types=(PageTypeSpec(page_type="note", id_prefix="note."),))

        assert spec.retrieval.collection == "legal"

    def test_retrieval_with_no_collection_key_takes_the_slug(self) -> None:
        spec = KnowledgeBaseSchema(
            slug="legal",
            page_types=(PageTypeSpec(page_type="note", id_prefix="note."),),
            retrieval=RetrievalSpec(group_by="page_type"),
        )

        assert spec.retrieval.collection == "legal"

    def test_an_explicit_collection_is_kept_over_the_slug(self) -> None:
        spec = KnowledgeBaseSchema(
            slug="docs",
            page_types=(PageTypeSpec(page_type="note", id_prefix="note."),),
            retrieval=RetrievalSpec(collection="repo-docs"),
        )

        assert spec.retrieval.collection == "repo-docs"

    def test_from_json_with_no_collection_key_takes_the_slug(self) -> None:
        spec = KnowledgeBaseSchema.from_json(
            '{"slug": "legal", "page_types": [{"page_type": "note", "id_prefix": "note."}], "retrieval": {"profile": "full"}}'
        )

        assert spec.retrieval.collection == "legal"

    def test_bundled_research_spec_still_resolves_to_research(self) -> None:
        """The bundled spec's own slug is `research`, so this is a regression check, not a coincidence."""
        assert research_spec().retrieval.collection == "research"


def _dated_spec(date_field: str | None) -> KnowledgeBaseSchema:
    """A schema whose one page type declares a scalar, an int and a link-list field, naming `date_field` when given."""
    retrieval: dict[str, object] = {} if date_field is None else {"date_field": date_field}
    return KnowledgeBaseSchema.model_validate(
        {
            "slug": "logbook",
            "page_types": [
                {
                    "page_type": "record",
                    "id_prefix": "record.",
                    "frontmatter_fields": [
                        {"name": "observed_on", "type": "scalar"},
                        {"name": "volume", "type": "int"},
                        {"name": "seen_with", "type": "link-list"},
                    ],
                }
            ],
            "retrieval": retrieval,
        }
    )


class TestRetrievalSpecDateField:
    """`retrieval.date_field`, the frontmatter field a page's page date is read from."""

    def test_defaults_to_created(self) -> None:
        assert RetrievalSpec().date_field == "created"

    def test_a_schema_naming_none_reads_created(self) -> None:
        assert _dated_spec(None).retrieval.date_field == "created"

    def test_a_declared_scalar_field_is_kept(self) -> None:
        assert _dated_spec("observed_on").retrieval.date_field == "observed_on"

    def test_created_may_be_named_outright(self) -> None:
        assert _dated_spec("created").retrieval.date_field == "created"

    @pytest.mark.parametrize("name", ["nowhere", "volume", "seen_with"])
    def test_a_name_that_is_no_declared_scalar_field_is_refused_naming_the_allowed_ones(self, name: str) -> None:
        with pytest.raises(ValidationError, match=rf"retrieval\.date_field '{name}'.*: created, observed_on"):
            _dated_spec(name)


class TestFacetsAddressByKeyAlone:
    def test_research_spec_loads_with_slot_less_facets(self) -> None:
        """The bundled research spec parses with all 8 facets carrying no `slot` attribute."""
        spec = research_spec()

        assert len(spec.facets) == 8
        assert all(not hasattr(facet, "slot") for facet in spec.facets)


# ---------------------------------------------------------------------------
# Test — EmbedStrategiesSpec / active_embed_strategies()
# ---------------------------------------------------------------------------


class TestEmbedStrategiesSpec:
    def test_defaults_to_no_explicit_active_list_and_empty_config(self) -> None:
        spec = EmbedStrategiesSpec()

        assert spec.active is None
        assert spec.config == {}

    def test_a_knowledge_base_spec_with_no_embed_strategies_declared_gets_the_default(self) -> None:
        spec = _minimal_spec()

        assert spec.embed_strategies == EmbedStrategiesSpec()


class TestActiveEmbedStrategies:
    """`KnowledgeBaseSchema.active_embed_strategies()` — the knowledge-base-declared embed-strategy activation."""

    def test_research_spec_activates_page_card_section_claim_and_source_chunk(self) -> None:
        # The bundled research spec declares both a claim grammar and `sources.enabled=True`.
        spec = research_spec()

        assert spec.active_embed_strategies() == ("page-card", "section", "claim", "source-chunk")

    def test_page_card_and_section_are_unconditional(self) -> None:
        spec = _minimal_spec(claims=None, page_types=(PageTypeSpec(page_type="paper", id_prefix="paper."),))

        assert spec.active_embed_strategies() == ("page-card", "section")

    def test_claim_is_active_only_when_a_claim_grammar_is_declared(self) -> None:
        with_claims = _minimal_spec()
        without_claims = _minimal_spec(claims=None, page_types=(PageTypeSpec(page_type="paper", id_prefix="paper."),))

        assert "claim" in with_claims.active_embed_strategies()
        assert "claim" not in without_claims.active_embed_strategies()

    def test_source_chunk_is_active_only_when_sources_enabled(self) -> None:
        enabled = _minimal_spec(
            sources=SourcesSpec(
                enabled=True,
                connectors=("zotero",),
                key_field="zotero_key",
                digest_link_field="digest_of",
                hash_field="source_hash",
                digest_types={"paper": "paper"},
            )
        )
        disabled = _minimal_spec(sources=SourcesSpec(enabled=False))

        assert "source-chunk" in enabled.active_embed_strategies()
        assert "source-chunk" not in disabled.active_embed_strategies()

    def test_an_explicit_active_override_replaces_the_default_outright(self) -> None:
        spec = _minimal_spec(embed_strategies=EmbedStrategiesSpec(active=("page-card",)))

        assert spec.active_embed_strategies() == ("page-card",)

    def test_an_explicit_active_override_can_add_a_name_the_default_would_never_include(self) -> None:
        spec = _minimal_spec(
            claims=None,
            page_types=(PageTypeSpec(page_type="paper", id_prefix="paper."),),
            embed_strategies=EmbedStrategiesSpec(active=("page-card", "section", "custom")),
        )

        assert spec.active_embed_strategies() == ("page-card", "section", "custom")


# ---------------------------------------------------------------------------
# Test 7 — Validator rejects invalid specs
# ---------------------------------------------------------------------------


class TestValidatorRejects:
    def test_rejects_claims_enabled_without_claims_object(self) -> None:
        """A spec with claims_enabled=True but claims=None must be rejected."""
        with pytest.raises(ValidationError):
            KnowledgeBaseSchema.model_validate(
                {"slug": "bad", "page_types": [{"page_type": "paper", "id_prefix": "paper.", "claims_enabled": True}], "claims": None}
            )

    def test_rejects_duplicate_kinds(self) -> None:
        """Two page_types with the same page_type must fail validation."""
        with pytest.raises(ValidationError):
            KnowledgeBaseSchema.model_validate(
                {"slug": "bad", "page_types": [{"page_type": "paper", "id_prefix": "paper."}, {"page_type": "paper", "id_prefix": "p2."}]}
            )

    def test_rejects_duplicate_id_prefix(self) -> None:
        """Two page_types with the same id_prefix must fail validation."""
        with pytest.raises(ValidationError):
            KnowledgeBaseSchema.model_validate(
                {"slug": "bad", "page_types": [{"page_type": "paper", "id_prefix": "paper."}, {"page_type": "entity", "id_prefix": "paper."}]}
            )

    def test_rejects_edge_field_naming_nonexistent_field(self) -> None:
        """An edge_field entry must name an actual frontmatter_field of that type."""
        with pytest.raises(ValidationError):
            KnowledgeBaseSchema.model_validate(
                {
                    "slug": "bad",
                    "page_types": [
                        {
                            "page_type": "paper",
                            "id_prefix": "paper.",
                            "frontmatter_fields": [{"name": "foo", "type": "link-list"}],
                            "edge_fields": ["nonexistent"],
                        }
                    ],
                }
            )

    def test_rejects_edge_field_naming_non_link_field(self) -> None:
        """An edge_field must name a link or link-list typed field, not a scalar."""
        with pytest.raises(ValidationError):
            KnowledgeBaseSchema.model_validate(
                {
                    "slug": "bad",
                    "page_types": [
                        {
                            "page_type": "paper",
                            "id_prefix": "paper.",
                            "frontmatter_fields": [{"name": "year", "type": "int"}],
                            "edge_fields": ["year"],
                        }
                    ],
                }
            )

    def test_rejects_facet_with_a_slot_key(self) -> None:
        """A facet key is the sole address; `slot` is no longer a recognized field."""
        with pytest.raises(ValidationError):
            FacetSpec.model_validate({"key": "tasks", "slot": "tasks"})

    def test_rejects_duplicate_relation_slugs(self) -> None:
        """Two RelationDecl sharing the same slug must fail validation."""
        with pytest.raises(ValidationError):
            KnowledgeBaseSchema.model_validate(
                {
                    "slug": "bad",
                    "page_types": [{"page_type": "paper", "id_prefix": "paper."}],
                    "relations": [
                        {"slug": "supports", "domain": "claim", "range": "entity"},
                        {"slug": "supports", "domain": "claim", "range": "claim"},
                    ],
                }
            )

    def test_rejects_empty_page_types(self) -> None:
        """page_types must be non-empty."""
        with pytest.raises(ValidationError):
            KnowledgeBaseSchema.model_validate({"slug": "bad", "page_types": []})

    def test_accepts_multiple_empty_prefixes(self) -> None:
        spec = KnowledgeBaseSchema.model_validate(
            {
                "slug": "estate",
                "page_types": [
                    {"page_type": "adr", "id_prefix": ""},
                    {"page_type": "reference", "id_prefix": ""},
                ],
            }
        )
        assert spec.prefix_by_page_type == {"adr": "", "reference": ""}

    def test_rejects_duplicate_nonempty_prefixes_still(self) -> None:
        with pytest.raises(ValidationError, match="duplicate id_prefix"):
            KnowledgeBaseSchema.model_validate(
                {
                    "slug": "estate",
                    "page_types": [
                        {"page_type": "a", "id_prefix": "mu."},
                        {"page_type": "b", "id_prefix": "mu."},
                    ],
                }
            )

    def test_rejects_path_field_naming_nonscalar_field(self) -> None:
        with pytest.raises(ValidationError, match="path_field"):
            KnowledgeBaseSchema.model_validate(
                {
                    "slug": "estate",
                    "page_types": [
                        {
                            "page_type": "module",
                            "id_prefix": "mod.",
                            "frontmatter_fields": [{"name": "path", "type": "link-list"}],
                            "path_fields": ["path"],
                        }
                    ],
                }
            )

    def test_rejects_path_field_naming_nonexistent_field(self) -> None:
        with pytest.raises(ValidationError, match="path_field"):
            KnowledgeBaseSchema.model_validate(
                {
                    "slug": "estate",
                    "page_types": [
                        {
                            "page_type": "module",
                            "id_prefix": "mod.",
                            "path_fields": ["path"],
                        }
                    ],
                }
            )

    def test_rejects_an_explicit_active_list_omitting_page_card(self) -> None:
        """Omitting `page-card` isn't a search-quality tradeoff, it deletes every page's own row.

        `page-card` produces every page's `card_text`/`body_hash`, and its row's own identity is
        independent of it, but sections/claims FK to the page row it anchors -- omitting it from an
        explicit `active` override is not a search-quality tradeoff (unlike dropping `section`), it
        is a knowledge base that never gets a page row at all. Rejected at spec-load time.
        """
        with pytest.raises(ValidationError, match="page-card"):
            _minimal_spec(embed_strategies=EmbedStrategiesSpec(active=("section", "claim")))

    def test_rejects_an_explicit_empty_active_list(self) -> None:
        """An empty tuple omits `page-card` too, so it falls out of the same rule."""
        with pytest.raises(ValidationError, match="page-card"):
            _minimal_spec(embed_strategies=EmbedStrategiesSpec(active=()))

    def test_rejects_a_decision_open_outside_the_status_set(self) -> None:
        """`decision.open` must be one of the page_type's own declared statuses."""
        with pytest.raises(ValidationError, match=re.escape("decision.open")):
            KnowledgeBaseSchema.model_validate(
                {
                    "slug": "bad",
                    "page_types": [
                        {
                            "page_type": "flag",
                            "id_prefix": "flag.",
                            "status_values": ["pending", "accepted"],
                            "decision": {"open": "not-a-status", "outcomes": [{"status": "accepted"}]},
                        }
                    ],
                }
            )

    def test_rejects_a_decision_outcome_status_outside_the_status_set(self) -> None:
        with pytest.raises(ValidationError, match="outcome status"):
            KnowledgeBaseSchema.model_validate(
                {
                    "slug": "bad",
                    "page_types": [
                        {
                            "page_type": "flag",
                            "id_prefix": "flag.",
                            "status_values": ["pending", "accepted"],
                            "decision": {"open": "pending", "outcomes": [{"status": "not-a-status"}]},
                        }
                    ],
                }
            )

    def test_rejects_a_decision_open_that_is_also_an_outcome(self) -> None:
        with pytest.raises(ValidationError, match="also be an outcome"):
            KnowledgeBaseSchema.model_validate(
                {
                    "slug": "bad",
                    "page_types": [
                        {
                            "page_type": "flag",
                            "id_prefix": "flag.",
                            "status_values": ["pending", "accepted"],
                            "decision": {"open": "pending", "outcomes": [{"status": "pending"}]},
                        }
                    ],
                }
            )

    def test_rejects_a_decision_outcome_link_naming_a_non_edge_field(self) -> None:
        with pytest.raises(ValidationError, match="link-list edge field"):
            KnowledgeBaseSchema.model_validate(
                {
                    "slug": "bad",
                    "page_types": [
                        {
                            "page_type": "flag",
                            "id_prefix": "flag.",
                            "status_values": ["pending", "accepted"],
                            "frontmatter_fields": [{"name": "resolved_by", "type": "link-list"}],
                            "decision": {"open": "pending", "outcomes": [{"status": "accepted", "link": "resolved_by"}]},
                        }
                    ],
                }
            )

    def test_rejects_a_decision_outcome_link_naming_a_singular_link_field(self) -> None:
        """`decide_page` can only write a multi-valued field, so an outcome's link must be a link-list edge field, not a singular link."""
        with pytest.raises(ValidationError, match="link-list edge field"):
            KnowledgeBaseSchema.model_validate(
                {
                    "slug": "bad",
                    "page_types": [
                        {
                            "page_type": "flag",
                            "id_prefix": "flag.",
                            "status_values": ["pending", "accepted"],
                            "frontmatter_fields": [{"name": "resolved_by", "type": "link"}],
                            "edge_fields": ["resolved_by"],
                            "decision": {"open": "pending", "outcomes": [{"status": "accepted", "link": "resolved_by"}]},
                        }
                    ],
                }
            )

    def test_accepts_a_decision_using_the_grammar_default_status_set(self) -> None:
        """A page_type with no declared status_values falls back to the grammar default set for its decision block too."""
        spec = KnowledgeBaseSchema.model_validate(
            {
                "slug": "ok",
                "page_types": [
                    {"page_type": "flag", "id_prefix": "flag.", "decision": {"open": "pending", "outcomes": [{"status": "active"}]}}
                ],
            }
        )
        assert spec.decision_page_types == (spec.page_type("flag"),)


_MALFORMED_IDENTIFIERS = [
    pytest.param("bad\nslug", id="newline"),
    pytest.param("../escape", id="path-traversal"),
    pytest.param("a/b", id="path-separator"),
    pytest.param("# Heading Injection", id="markdown-metacharacters"),
    pytest.param("bad slug", id="whitespace"),
    pytest.param("Bad-Slug", id="uppercase"),
]


class TestSlugAndPageTypePattern:
    """`KnowledgeBaseSchema.slug` and `PageTypeSpec.page_type` are constrained to a plain lowercase-hyphen identifier.

    Both become literal filesystem path segments (`_meta/templates/tpl-{page_type}.md`, a scaffolded
    knowledge base directory name) and are interpolated verbatim into generated markdown (headings,
    `CLAUDE.md`/`AGENTS.md`), so neither field may carry a path separator, a `..` traversal
    segment, or markdown/heading syntax.
    """

    @pytest.mark.parametrize("bad_value", _MALFORMED_IDENTIFIERS)
    def test_rejects_a_malformed_slug(self, bad_value: str) -> None:
        with pytest.raises(ValidationError):
            KnowledgeBaseSchema.model_validate({"slug": bad_value, "page_types": [{"page_type": "concept", "id_prefix": "c."}]})

    @pytest.mark.parametrize("bad_value", _MALFORMED_IDENTIFIERS)
    def test_rejects_a_malformed_kind(self, bad_value: str) -> None:
        with pytest.raises(ValidationError):
            KnowledgeBaseSchema.model_validate({"slug": "demo", "page_types": [{"page_type": bad_value, "id_prefix": "c."}]})

    def test_accepts_a_valid_lowercase_hyphen_slug_and_kind(self) -> None:
        spec = KnowledgeBaseSchema.model_validate({"slug": "my-knowledge-base", "page_types": [{"page_type": "my-page-type", "id_prefix": "c."}]})
        assert spec.slug == "my-knowledge-base"
        assert spec.page_types[0].page_type == "my-page-type"


# ---------------------------------------------------------------------------
# Test 9 — Research-vocab fidelity via vendored vocabulary fixture
# ---------------------------------------------------------------------------


class TestResearchVocabFidelity:
    """Parse the vendored vocabulary fixture and compare against the research template's vocabulary.

    This test makes the research template's schema trustworthy: if the vendored files change (new values added),
    the test catches drift and the spec must be updated.
    """

    @pytest.fixture
    def research(self) -> KnowledgeBaseSchema:
        return research_spec()

    @pytest.fixture
    def registry_result(self) -> VocabularyParseResult:
        from engram.kb.contracts import parse_vocabulary

        docs = [(p.name, p.read_text(encoding="utf-8")) for p in sorted(_FIXTURE_DIR.glob("*.md"))]
        return parse_vocabulary(docs)

    def test_entity_types_values_match(self, research: KnowledgeBaseSchema, registry_result) -> None:
        """Entity type slugs declared in the spec match the parsed vocabulary."""
        spec_values = {e.value for e in research.entity_types}
        registry_values = set(registry_result.index.entity_types.keys())
        assert spec_values == registry_values

    def test_entity_types_definitions_match(self, research: KnowledgeBaseSchema, registry_result) -> None:
        """Entity type definitions in spec match parsed vocabulary (key-by-key)."""
        for entry in research.entity_types:
            assert registry_result.index.entity_types.get(entry.value) == entry.definition, (
                f"entity_type {entry.value!r} definition mismatch"
            )

    def test_claim_kinds_match(self, research: KnowledgeBaseSchema, registry_result) -> None:
        """Claim kind values in spec match the vocabulary's claim_types."""
        spec_values = {ck.value for ck in research.claim_types}
        registry_values = set(registry_result.index.claim_types.keys())
        assert spec_values == registry_values

    def test_relation_slugs_match(self, research: KnowledgeBaseSchema, registry_result) -> None:
        """Relation slugs declared in spec are a superset of those parsed from the vocabulary.

        Note: the spec may include relations not in the vocabulary (e.g. 'answered_by' — a per-type
        field relation not in relation-types.md, and 'maps' — body-derived, registered differently).
        We assert spec slugs contain all vocabulary slugs; the reverse need not hold.
        """
        spec_slugs = {r.slug for r in research.relations}
        registry_slugs = set(registry_result.index.relations.keys())
        # All relations the vocabulary knows about must be declared in the spec
        assert registry_slugs <= spec_slugs

    def test_facet_keys_match_registry(self, research: KnowledgeBaseSchema, registry_result) -> None:
        """Facet keys in the spec exactly match those parsed from the vocabulary (parity oracle)."""
        spec_facet_keys = {f.key for f in research.facets}
        registry_facet_keys = set(registry_result.index.facets.keys())
        assert registry_facet_keys == spec_facet_keys


# ---------------------------------------------------------------------------
# Test 10 — model_json_schema()
# ---------------------------------------------------------------------------


class TestJsonSchema:
    def test_schema_has_page_types_property(self) -> None:
        schema = KnowledgeBaseSchema.model_json_schema()
        assert "page_types" in schema.get("properties", {}), "page_types must appear in JSON Schema properties"

    def test_schema_has_slug_property(self) -> None:
        schema = KnowledgeBaseSchema.model_json_schema()
        assert "slug" in schema.get("properties", {})


# ---------------------------------------------------------------------------
# Test 11 — load_knowledge_base_schema() and resolve_schema() refuse a missing spec
# ---------------------------------------------------------------------------


class TestLoadSchemaSpec:
    def test_reads_knowledge_base_schema_json_when_present(self, tmp_path: Path) -> None:
        """`<knowledge_base>/knowledge-base-schema.json` is read when it exists."""
        spec = _minimal_spec()
        (tmp_path / "knowledge-base-schema.json").write_text(spec.model_dump_json(), encoding="utf-8")

        loaded = load_knowledge_base_schema(tmp_path)

        assert loaded == spec

    def test_refuses_when_knowledge_base_schema_json_is_absent(self, tmp_path: Path) -> None:
        """An empty knowledge base (no `knowledge-base-schema.json`) is refused, naming a bundle to start from."""
        with pytest.raises(KnowledgeBaseSchemaMissingError, match="studio init --template"):
            load_knowledge_base_schema(tmp_path)


class TestResolveSpec:
    def test_returns_the_given_spec_unchanged(self) -> None:
        spec = _minimal_spec()
        assert resolve_schema(spec) is spec

    def test_refuses_none(self) -> None:
        with pytest.raises(KnowledgeBaseSchemaMissingError, match="studio init --template"):
            resolve_schema(None)


# ---------------------------------------------------------------------------
# Test 12 — Source declarations: folders (plural) and files
# ---------------------------------------------------------------------------


class TestSourceDeclarations:
    def test_note_folders_merges_folder_and_folders(self) -> None:
        spec = KnowledgeBaseSchema.model_validate(
            {
                "slug": "estate",
                "page_types": [
                    {"page_type": "adr", "id_prefix": "", "folders": ["adr", "../engram/docs/adr"]},
                    {"page_type": "brainstorming-doc", "id_prefix": "bs.", "folder": "../brainstorming"},
                ],
            }
        )
        assert spec.note_folders == ("adr", "../engram/docs/adr", "../brainstorming")

    def test_note_files_collects_in_declaration_order(self) -> None:
        spec = KnowledgeBaseSchema.model_validate(
            {
                "slug": "estate",
                "page_types": [{"page_type": "glossary", "id_prefix": "", "files": ["../CONTEXT-MAP.md", "../engram/CONTEXT.md"]}],
            }
        )
        assert spec.note_files == ("../CONTEXT-MAP.md", "../engram/CONTEXT.md")
