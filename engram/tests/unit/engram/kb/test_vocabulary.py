"""Unit tests for the vocabulary indexer (`src/engram/memory/vocabulary.py`).

`parse_vocabulary` turns the REAL `_meta/vocabulary/*.md` markdown into a
:class:`~engram.kb.domain.note.VocabularyIndex` — the controlled vocabulary the index
*validate* stage checks notes against. The fixtures under
`tests/fixtures/golden/mini-vocabularies/` are verbatim REAL subsets of the live
knowledge base files, so these tests pin the parser to the actual markdown shapes, not an
imagined one:

* `entity-types.md` — one `| entity_type | Definition | Examples |` table of
  backtick-wrapped slugs, followed by a `## Boundary rules` prose section the
  parser must ignore;
* `relation-types.md` — several `| Relation | Domain → Range | Meaning |`
  tables (the `→`/`↔` arrow carries domain and range; `↔` ⇒ symmetric),
  plus a `| Relation | Meaning |` *Lifecycle* table with NO domain/range, plus
  a `## Claim qualifier fields` table (`subject::` …) that is NOT relations
  and must be excluded;
* ``facets.md`` — three different value shapes: prose ``Seed: `a`, `b`, …``
  paragraphs (`tasks`/`domains`/`stages`), closed `| Value | Definition |`
  tables (`scale_regimes`), and `·`-separated inline lists (`paper_type`,
  `eval_setting`); the `page_type` facet feeds `claim_types`, not `facets`.

Malformed rows never abort the parse: they are skipped and surfaced as
`warnings` on the result. The `entity-types.md` fixture embeds one broken row to
prove it.

Pure function of its `(name, text)` inputs — no filesystem, no monkeypatching.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from engram.kb.contracts import (
    FacetSpec,
    FrontmatterFieldSpec,
    KnowledgeBaseSchema,
    PageTypeSpec,
    RelationSpec,
    VocabularyIndex,
    facet_choices_drift,
    parse_vocabulary,
    schema_vocabulary_drift,
)
from engram.testing.knowledge_base_builder import GOLDEN_MINI_VOCABULARIES
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from engram.kb.domain.vocabulary import VocabularyParseResult

_MINI = GOLDEN_MINI_VOCABULARIES


def _documents() -> list[tuple[str, str]]:
    """The three real-format mini-vocabulary fixtures as `(name, text)` pairs."""
    return [(p.name, p.read_text(encoding="utf-8")) for p in sorted(_MINI.glob("*.md"))]


@pytest.fixture
def result() -> VocabularyParseResult:
    return parse_vocabulary(_documents())


class TestResultShape:
    def test_returns_a_result_carrying_an_index_and_warnings(self, result: VocabularyParseResult):
        assert isinstance(result.index, VocabularyIndex)
        assert isinstance(result.warnings, tuple)


class TestEntityTypes:
    def test_parses_backtick_slugs_with_definitions(self, result: VocabularyParseResult):
        et = result.index.entity_types
        assert "technique" in et
        assert "benchmark" in et
        assert et["technique"] == "A method/algorithm/strategy that can be applied, composed, or compared"

    def test_ignores_boundary_rules_prose_section(self, result: VocabularyParseResult):
        # Words from the "## Boundary rules" section must not leak in as entity types.
        assert "technique vs tool" not in result.index.entity_types
        assert all(" " not in value for value in result.index.entity_types)

    def test_skips_malformed_row_with_a_warning(self, result: VocabularyParseResult):
        # The fixture has a broken row ("this row has no closing pipe value");
        # the parser keeps the good rows and records a warning mentioning the file.
        assert "model" in result.index.entity_types  # the row AFTER the broken one still loads
        assert any("entity-types.md" in w.source for w in result.warnings)


class TestRelations:
    def test_conceptual_relation_has_domain_and_range(self, result: VocabularyParseResult):
        addresses = result.index.relations["addresses"]
        assert addresses.domain == "technique"
        assert addresses.range == "problem"

    def test_symmetric_arrow_sets_symmetric_flag(self, result: VocabularyParseResult):
        # `composes_with` uses the ↔ arrow ⇒ symmetric; `is_a` uses → ⇒ not symmetric.
        assert result.index.relations["composes_with"].symmetric is True
        assert result.index.relations["is_a"].symmetric is False

    def test_paper_level_relation_present(self, result: VocabularyParseResult):
        builds_on = result.index.relations["builds_on"]
        assert builds_on.domain == "paper"
        assert builds_on.range == "paper"

    def test_evidential_relation_with_alternation_range(self, result: VocabularyParseResult):
        # `supports` range is written "entity \| claim" in the markdown; the parser must
        # un-escape the \| so the full multi-valued range round-trips as "entity | claim".
        supports = result.index.relations["supports"]
        assert supports.domain == "claim"
        assert supports.range == "entity | claim"

    def test_escaped_pipe_in_range_parses_full_value(self):
        # Regression test for the naive split("|") bug: a cell containing \| must not
        # be truncated.  Inline fixture with an escaped pipe in the Domain → Range cell.
        # Must be named "relation-types.md" so parse_vocabulary routes it correctly.
        text = """\
---
id: meta.vocabulary-pipe-test
type: meta
title: Pipe test
---

## Relations

| Relation | Domain → Range | Meaning |
|---|---|---|
| `pipe_rel` | entity → entity \\| claim | For testing |
"""
        from engram.kb.contracts import parse_vocabulary

        result = parse_vocabulary([("relation-types.md", text)])
        assert result.index.relations["pipe_rel"].range == "entity | claim"

    def test_scaffolded_piped_range_round_trips_through_parse(self):
        # The scaffold must ESCAPE a literal "|" in a relation's range so that parse_vocabulary
        # (which unescapes "\|", vocabulary.py:157-158) reads the full multi-valued range back
        # intact; an unescaped "|" in the Domain → Range cell truncates the range to "entity".
        # Both halves are pinned: the rendered markdown carries an escaped "entity \| claim",
        # and parsing that markdown yields the full "entity | claim".
        from engram.init.contracts import scaffold
        from engram.kb.contracts import KnowledgeBaseSchema

        spec = KnowledgeBaseSchema.model_validate(
            {
                "slug": "pipe",
                "page_types": [{"page_type": "entity", "id_prefix": "ent."}],
                "relations": [{"slug": "supports", "domain": "claim", "range": "entity | claim", "description": "Evidence in favor"}],
            }
        )

        relation_md = scaffold(spec)["_meta/vocabulary/relation-types.md"]
        assert "entity \\| claim" in relation_md  # the scaffold escaped the literal pipe

        parsed = parse_vocabulary([("relation-types.md", relation_md)])
        assert parsed.index.relations["supports"].range == "entity | claim"  # not truncated to "entity"
        assert parsed.index.relations["supports"].domain == "claim"

    def test_lifecycle_relations_without_domain_range_still_register(self, result: VocabularyParseResult):
        # The Lifecycle table is `| Relation | Meaning |` — no Domain → Range column.
        assert "superseded_by" in result.index.relations
        assert "same_as" in result.index.relations

    def test_claim_qualifier_fields_are_not_relations(self, result: VocabularyParseResult):
        # `subject::`, `on_dataset::`, `metric::` live under "## Claim qualifier fields"
        # and are scoping fields, NOT relation types — they must be excluded.
        for qualifier in ("subject", "on_dataset", "metric"):
            assert qualifier not in result.index.relations


class TestFacets:
    def test_prose_seed_list_facet_values(self, result: VocabularyParseResult):
        tasks = result.index.facets["tasks"]
        assert "multi-hop-qa" in tasks
        assert "sensemaking-qa" in tasks

    def test_multiple_prose_facets_parsed(self, result: VocabularyParseResult):
        assert "literary" in result.index.facets["domains"]
        assert "indexing" in result.index.facets["stages"]

    def test_closed_table_facet_values(self, result: VocabularyParseResult):
        scale = result.index.facets["scale_regimes"]
        assert set(scale) == {"toy", "small", "large", "web-scale"}

    def test_dot_separated_single_value_facet(self, result: VocabularyParseResult):
        paper_type = result.index.facets["paper_type"]
        assert "method" in paper_type
        assert "engineering-report" in paper_type

    def test_kind_facet_feeds_claim_kinds_not_facets(self, result: VocabularyParseResult):
        assert "page_type" not in result.index.facets
        assert "benchmark-result" in result.index.claim_types
        assert "negative-result" in result.index.claim_types
        assert result.index.claim_types["ablation"] == "Component-removal evidence"


class TestPartialInput:
    def test_missing_a_registry_file_yields_empty_section_not_error(self):
        # Only the entity-types doc supplied: relations/facets/claim_types come back empty,
        # no exception — the caller decides whether that is acceptable.
        only_entities = [(n, t) for n, t in _documents() if n == "entity-types.md"]
        partial = parse_vocabulary(only_entities)
        assert partial.index.entity_types  # populated
        assert partial.index.relations == {}
        assert partial.index.facets == {}
        assert partial.index.claim_types == {}


def _registry_matching_research_spec() -> VocabularyIndex:
    """Build the VocabularyIndex that the bundled research spec scaffolds into.

    Round-trips spec -> `_meta/vocabulary/*.md` (via the scaffold) -> `parse_vocabulary`,
    so the result is exactly what the live research knowledge base's parsed vocabulary looks like.
    The drift guard must see this pair as consistent (no drift), since the scaffold is
    the authoritative source of which spec vocab seeds into the vocabulary.
    """
    from engram.init.contracts import scaffold

    files = scaffold(research_spec())
    docs = [(name.rsplit("/", 1)[-1], text) for name, text in files.items() if name.startswith("_meta/vocabulary/")]
    return parse_vocabulary(docs).index


class TestSpecRegistryDrift:
    """`schema_vocabulary_drift` surfaces vocab the spec seeds that the parsed vocabulary lacks (F27).

    Direction is deliberate: the `_meta/vocabulary/*.md` markdown is the LIVE authoritative
    vocab and may legitimately extend the spec seed, so a vocabulary that has MORE than the
    seed is fine. The dangerous direction — vocab the spec declares but the vocabulary is
    missing — can break resolution and is the only thing reported.
    """

    def test_matching_spec_and_registry_have_no_drift(self):
        spec = research_spec()
        vocabulary = _registry_matching_research_spec()
        assert schema_vocabulary_drift(vocabulary, spec) == ()

    def test_missing_entity_type_is_reported(self):
        spec = research_spec()
        vocabulary = _registry_matching_research_spec()
        # Drop one entity type the spec seeds (`benchmark`).
        without_benchmark = {k: v for k, v in dict(vocabulary.entity_types).items() if k != "benchmark"}
        drifted = vocabulary.model_copy(update={"entity_types": without_benchmark})

        drift = schema_vocabulary_drift(drifted, spec)

        assert drift
        assert any("benchmark" in d and "entity_type" in d for d in drift)
        assert any("entity_types[value=benchmark].value" in d and "_meta/vocabulary/entity-types.md" in d for d in drift)

    def test_missing_relation_is_reported(self):
        spec = research_spec()
        vocabulary = _registry_matching_research_spec()
        without_addresses = {k: v for k, v in dict(vocabulary.relations).items() if k != "addresses"}
        drifted = vocabulary.model_copy(update={"relations": without_addresses})

        drift = schema_vocabulary_drift(drifted, spec)

        assert any("addresses" in d and "relation" in d for d in drift)
        assert any("relations[slug=addresses].slug" in d and "_meta/vocabulary/relation-types.md" in d for d in drift)

    def test_missing_facet_value_is_reported(self):
        spec = research_spec()
        vocabulary = _registry_matching_research_spec()
        # Drop one seed value the spec declares for the `tasks` facet.
        tasks = {k: v for k, v in dict(vocabulary.facets["tasks"]).items() if k != "multi-hop-qa"}
        facets = {**dict(vocabulary.facets), "tasks": tasks}
        drifted = vocabulary.model_copy(update={"facets": facets})

        drift = schema_vocabulary_drift(drifted, spec)

        assert any("multi-hop-qa" in d and "tasks" in d for d in drift)
        assert any("facets[key=tasks].seed" in d and "_meta/vocabulary/facets.md" in d for d in drift)

    def test_missing_facet_key_entirely_is_reported(self):
        spec = research_spec()
        vocabulary = _registry_matching_research_spec()
        # Drop the whole `paper_type` facet section from the parsed vocabulary.
        facets = {k: v for k, v in dict(vocabulary.facets).items() if k != "paper_type"}
        drifted = vocabulary.model_copy(update={"facets": facets})

        drift = schema_vocabulary_drift(drifted, spec)

        assert any("paper_type" in d for d in drift)
        assert any("facets[key=paper_type]" in d and "_meta/vocabulary/facets.md" in d for d in drift)

    def test_missing_claim_kind_is_reported(self):
        spec = research_spec()
        vocabulary = _registry_matching_research_spec()
        without_ablation = {k: v for k, v in dict(vocabulary.claim_types).items() if k != "ablation"}
        drifted = vocabulary.model_copy(update={"claim_types": without_ablation})

        drift = schema_vocabulary_drift(drifted, spec)

        assert any("ablation" in d and "claim" in d for d in drift)
        assert any("claim_types[value=ablation].value" in d and "_meta/vocabulary/facets.md" in d for d in drift)

    def test_registry_extending_the_spec_is_not_drift(self):
        # The vocabulary is the live authoritative vocab and may legitimately carry vocab
        # the spec seed does not — that direction must NOT be reported as drift.
        spec = research_spec()
        vocabulary = _registry_matching_research_spec()
        extended = vocabulary.model_copy(
            update={
                "entity_types": {**dict(vocabulary.entity_types), "novel_thing": "a live-added type beyond the seed"},
                "relations": {**dict(vocabulary.relations), "relates_to": RelationSpec(domain="entity", range="entity")},
                "claim_types": {**dict(vocabulary.claim_types), "anecdote": "a live-added page_type"},
                "facets": {**dict(vocabulary.facets), "tasks": {**dict(vocabulary.facets["tasks"]), "novel-task": ""}},
            }
        )

        assert schema_vocabulary_drift(extended, spec) == ()


def _spec_with_facet_choices(*, seed: tuple[str, ...], choices: tuple[str, ...]) -> KnowledgeBaseSchema:
    """A minimal KnowledgeBaseSchema whose 'document' page_type's 'doc_type' field mirrors a same-keyed facet."""
    return KnowledgeBaseSchema(
        slug="docs",
        page_types=(
            PageTypeSpec(page_type="document", frontmatter_fields=(FrontmatterFieldSpec(name="doc_type", type="scalar", choices=choices),)),
        ),
        facets=(FacetSpec(key="doc_type", seed=seed),),
    )


class TestFacetChoicesDrift:
    """`facet_choices_drift` surfaces a facet seed and its mirroring field's choices disagreeing (F3).

    A facet key that also names a frontmatter field with non-empty `choices` declares the same
    closed vocabulary twice: the facet's `seed` and the field's `choices`. They must agree in both
    directions — a seed value the field's choices rejects can never be authored even though the
    facet declares it wanted, and a choices value the seed omits is authorable but invisible to
    `schema_vocabulary_drift`.
    """

    def test_matching_seed_and_choices_have_no_drift(self):
        spec = _spec_with_facet_choices(seed=("method", "survey"), choices=("method", "survey"))
        assert facet_choices_drift(spec) == ()

    def test_seed_value_missing_from_choices_is_reported(self):
        spec = _spec_with_facet_choices(seed=("method", "survey"), choices=("method",))
        drift = facet_choices_drift(spec)
        assert any("survey" in d and "doc_type" in d for d in drift)

    def test_choices_value_missing_from_seed_is_reported(self):
        spec = _spec_with_facet_choices(seed=("method",), choices=("method", "survey"))
        drift = facet_choices_drift(spec)
        assert any("survey" in d and "doc_type" in d for d in drift)

    def test_field_with_empty_choices_is_ignored(self):
        spec = _spec_with_facet_choices(seed=("method", "survey"), choices=())
        assert facet_choices_drift(spec) == ()

    def test_facet_with_no_matching_field_is_ignored(self):
        spec = KnowledgeBaseSchema(
            slug="docs",
            page_types=(PageTypeSpec(page_type="document", frontmatter_fields=()),),
            facets=(FacetSpec(key="doc_type", seed=("method", "survey")),),
        )
        assert facet_choices_drift(spec) == ()

    def test_field_with_no_matching_facet_is_ignored(self):
        spec = KnowledgeBaseSchema(
            slug="docs",
            page_types=(
                PageTypeSpec(
                    page_type="document", frontmatter_fields=(FrontmatterFieldSpec(name="doc_type", type="scalar", choices=("method",)),)
                ),
            ),
        )
        assert facet_choices_drift(spec) == ()

    def test_message_names_both_knowledge_base_schema_paths(self):
        spec = _spec_with_facet_choices(seed=("method", "survey"), choices=("method",))
        drift = facet_choices_drift(spec)
        seed_path = "facets[key=doc_type].seed"
        choices_path = "page_types[page_type=document].frontmatter_fields[name=doc_type].choices"
        assert any(seed_path in d and choices_path in d for d in drift)


class TestDriftSurfacedByValidate:
    """The validate stage folds drift into `ValidationReport.warnings`, never `errors` (F27).

    Recommendation by design: a spec/vocabulary mismatch must surface in the validation report so it
    cannot pass silently, but it must NEVER fail indexing (the markdown vocabulary stays
    the live authoritative vocab and may legitimately extend the seed).
    """

    def test_drift_appears_as_a_warning_not_an_error(self):
        from engram.indexing.domain.validator import validate

        spec = research_spec()
        vocabulary = _registry_matching_research_spec()
        # A vocabulary missing a spec-seeded entity type → drift the validator must surface.
        without_benchmark = {k: v for k, v in dict(vocabulary.entity_types).items() if k != "benchmark"}
        drifted = vocabulary.model_copy(update={"entity_types": without_benchmark})

        report = validate((), drifted, spec=spec)

        drift_warnings = [issue for issue in report.warnings if issue.rule == "spec-vocabulary-drift"]
        assert drift_warnings  # surfaced
        assert all(issue.severity == "warning" for issue in drift_warnings)
        assert not any(issue.rule == "spec-vocabulary-drift" for issue in report.errors)
        assert any("benchmark" in issue.message for issue in drift_warnings)

    def test_consistent_spec_and_registry_yield_no_drift_warning(self):
        from engram.indexing.domain.validator import validate

        spec = research_spec()
        vocabulary = _registry_matching_research_spec()

        report = validate((), vocabulary, spec=spec)

        assert not any(issue.rule == "spec-vocabulary-drift" for issue in report.warnings)
