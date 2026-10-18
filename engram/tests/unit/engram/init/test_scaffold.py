"""Unit tests for `engram/init/domain/scaffold.py` — pure scaffold(spec) → dict[path, text].

Tests:
  1. Round-trip research spec: scaffold → parse_vocabulary → VocabularyIndex matches declared vocab.
  2. Round-trip cooking spec: same guarantee for a generic non-research spec (symmetric + lifecycle relations).
  3. Template files: correct set per page_type (all non-meta), each has canonical sections as H2s; paper has claim stub.
  4. knowledge-base-schema.json round-trips via KnowledgeBaseSchema.from_json.
  5. Purpose/schema/AGENTS/CLAUDE files are present and contain expected strings.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from engram.kb.contracts import KnowledgeBaseSchema
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from engram.kb.contracts import VocabularyIndex

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cooking_spec() -> KnowledgeBaseSchema:
    """Build a minimal cooking spec for generality tests.

    Covers: symmetric relation, lifecycle (edgeless) relation, two page types
    (recipe + ingredient), two facets addressed by key alone, no claims.
    """
    return KnowledgeBaseSchema.model_validate(
        {
            "slug": "cooking",
            "purpose": "A cooking knowledge base.",
            "page_types": [
                {
                    "page_type": "recipe",
                    "id_prefix": "rec.",
                    "frontmatter_fields": [
                        {"name": "cuisine", "type": "scalar", "required": False},
                        {"name": "pairs_with", "type": "link-list"},
                    ],
                    "canonical_sections": ["overview", "ingredients", "method"],
                    "edge_fields": ["pairs_with"],
                    "claims_enabled": False,
                },
                {
                    "page_type": "ingredient",
                    "id_prefix": "ing.",
                    "frontmatter_fields": [{"name": "substitute_for", "type": "link-list"}],
                    "canonical_sections": ["overview"],
                    "edge_fields": ["substitute_for"],
                    "claims_enabled": False,
                },
                {
                    "page_type": "meta",
                    "id_prefix": "meta.",
                    "frontmatter_fields": [],
                    "canonical_sections": [],
                    "edge_fields": [],
                    "claims_enabled": False,
                },
            ],
            "relations": [
                {
                    "slug": "pairs_with",
                    "domain": "ingredient",
                    "range": "ingredient",
                    "symmetric": True,
                    "description": "Ingredients that complement each other.",
                },
                {
                    "slug": "substitute_for",
                    "domain": "ingredient",
                    "range": "ingredient",
                    "symmetric": False,
                    "description": "Can be used instead of.",
                },
                {
                    "slug": "archived_by",
                    "domain": "",
                    "range": "",
                    "symmetric": False,
                    "description": "Recipe archived by a newer version.",
                },
            ],
            "entity_types": [
                {"value": "spice", "definition": "A flavouring agent."},
                {"value": "protein", "definition": "A main protein source."},
            ],
            "claim_types": [],
            "facets": [
                {"key": "cuisine", "scope": ["recipe"], "seed": ["italian", "french", "japanese"]},
                {"key": "season", "scope": ["recipe"], "seed": ["spring", "summer", "autumn", "winter"]},
            ],
            "claims": None,
        }
    )


def _round_trip_index(spec: KnowledgeBaseSchema) -> VocabularyIndex:
    """Scaffold spec, feed vocabulary files through parse_vocabulary, return VocabularyIndex."""
    from engram.init.contracts import scaffold
    from engram.kb.contracts import parse_vocabulary

    files = scaffold(spec)
    docs = [(name.split("/")[-1], text) for name, text in files.items() if name.startswith("_meta/vocabulary/")]
    result = parse_vocabulary(docs)
    return result.index


# ---------------------------------------------------------------------------
# 1. Round-trip: research spec
# ---------------------------------------------------------------------------


class TestRoundTripResearch:
    def test_entity_types_exact(self) -> None:
        spec = research_spec()
        idx = _round_trip_index(spec)
        spec_values = {e.value for e in spec.entity_types}
        assert set(idx.entity_types.keys()) == spec_values

    def test_entity_type_definitions_preserved(self) -> None:
        spec = research_spec()
        idx = _round_trip_index(spec)
        for entry in spec.entity_types:
            assert idx.entity_types.get(entry.value) == entry.definition

    def test_claim_kinds_exact(self) -> None:
        spec = research_spec()
        idx = _round_trip_index(spec)
        spec_ck = {e.value for e in spec.claim_types}
        assert set(idx.claim_types.keys()) == spec_ck

    def test_relation_slugs_superset(self) -> None:
        """All spec slugs appear in the parsed index (parser may not parse every row)."""
        spec = research_spec()
        idx = _round_trip_index(spec)
        spec_slugs = {r.slug for r in spec.relations}
        assert spec_slugs.issubset(set(idx.relations.keys()))

    def test_domain_range_preserved_for_3col_relations(self) -> None:
        r"""Every relation with non-empty domain/range parses back with the same domain/range.

        This holds even when the domain or range contains a literal `|` (e.g. the research
        `supports`/`refutes` relations whose range is `entity | claim`): the scaffold escapes
        `|` as `\|` when it renders the relation table, and `parse_vocabulary` unescapes it back
        to `|`, so the round-trip is exact with no exclusions.
        """
        spec = research_spec()
        idx = _round_trip_index(spec)
        for rel in spec.relations:
            if rel.domain and rel.range:
                parsed = idx.relations.get(rel.slug)
                assert parsed is not None, f"Relation {rel.slug!r} missing from parsed index"
                assert parsed.domain == rel.domain, f"{rel.slug}: domain mismatch"
                assert parsed.range == rel.range, f"{rel.slug}: range mismatch"

    def test_symmetric_relations_parsed_correctly(self) -> None:
        spec = research_spec()
        idx = _round_trip_index(spec)
        for rel in spec.relations:
            if rel.symmetric:
                parsed = idx.relations.get(rel.slug)
                assert parsed is not None
                assert parsed.symmetric is True, f"{rel.slug!r} should be symmetric"

    def test_lifecycle_relations_have_empty_domain_range(self) -> None:
        """Edgeless (lifecycle) relations must parse back with empty domain and range."""
        spec = research_spec()
        idx = _round_trip_index(spec)
        for rel in spec.relations:
            if not rel.domain and not rel.range:
                parsed = idx.relations.get(rel.slug)
                assert parsed is not None, f"Lifecycle relation {rel.slug!r} missing"
                assert parsed.domain == "", f"{rel.slug}: domain should be empty"
                assert parsed.range == "", f"{rel.slug}: range should be empty"

    def test_facets_keys_present(self) -> None:
        spec = research_spec()
        idx = _round_trip_index(spec)
        # All spec facets (non-page_type) appear in parsed facets OR claim_types (for 'page_type')
        for fspec in spec.facets:
            if fspec.key == "page_type":
                # routed to claim_types
                continue
            assert fspec.key in idx.facets, f"Facet {fspec.key!r} missing"

    def test_facet_seed_values_present(self) -> None:
        spec = research_spec()
        idx = _round_trip_index(spec)
        for fspec in spec.facets:
            if fspec.key == "page_type":
                continue
            for seed in fspec.seed:
                assert seed in idx.facets[fspec.key], f"Seed {seed!r} missing from facet {fspec.key!r}"


# ---------------------------------------------------------------------------
# 2. Round-trip: cooking spec (symmetric + lifecycle + no claims)
# ---------------------------------------------------------------------------


class TestRoundTripCooking:
    def test_entity_types_exact(self) -> None:
        spec = _cooking_spec()
        idx = _round_trip_index(spec)
        assert set(idx.entity_types.keys()) == {"spice", "protein"}

    def test_symmetric_relation_parsed(self) -> None:
        spec = _cooking_spec()
        idx = _round_trip_index(spec)
        parsed = idx.relations.get("pairs_with")
        assert parsed is not None
        assert parsed.symmetric is True
        assert parsed.domain == "ingredient"
        assert parsed.range == "ingredient"

    def test_non_symmetric_relation_parsed(self) -> None:
        spec = _cooking_spec()
        idx = _round_trip_index(spec)
        parsed = idx.relations.get("substitute_for")
        assert parsed is not None
        assert parsed.symmetric is False
        assert parsed.domain == "ingredient"

    def test_lifecycle_edgeless_relation_parsed(self) -> None:
        """archived_by has empty domain+range → must appear in lifecycle table → domain/range empty."""
        spec = _cooking_spec()
        idx = _round_trip_index(spec)
        parsed = idx.relations.get("archived_by")
        assert parsed is not None
        assert parsed.domain == ""
        assert parsed.range == ""

    def test_facets_both_present(self) -> None:
        spec = _cooking_spec()
        idx = _round_trip_index(spec)
        assert "cuisine" in idx.facets
        assert "season" in idx.facets

    def test_claim_kinds_empty_when_none_declared(self) -> None:
        spec = _cooking_spec()
        idx = _round_trip_index(spec)
        # spec.claim_types is () and spec.claims is None → no 'page_type' section → empty
        assert len(idx.claim_types) == 0


# ---------------------------------------------------------------------------
# 3. Template files
# ---------------------------------------------------------------------------


class TestTemplates:
    def test_template_exists_for_each_non_meta_kind(self) -> None:
        from engram.init.contracts import scaffold

        spec = research_spec()
        files = scaffold(spec)
        for pt in spec.page_types:
            if pt.page_type == "meta":
                continue
            key = f"_meta/templates/tpl-{pt.page_type}.md"
            assert key in files, f"Missing template: {key}"

    def test_no_template_for_meta_kind(self) -> None:
        from engram.init.contracts import scaffold

        spec = research_spec()
        files = scaffold(spec)
        assert "_meta/templates/tpl-meta.md" not in files

    def test_paper_template_has_canonical_sections_as_h2(self) -> None:
        from engram.init.contracts import scaffold
        from engram.init.domain.scaffold import _slug_to_heading

        spec = research_spec()
        pt = spec.page_type("paper")
        assert pt is not None
        files = scaffold(spec)
        text = files["_meta/templates/tpl-paper.md"]
        for section_slug in spec.sections_for("paper"):
            # A slug the spec declares a heading for (e.g. tl_dr -> "TL;DR") wins over the
            # scaffolder's own generic title-case fallback, same as `_render_template` itself.
            heading = pt.canonical_headings.get(section_slug, _slug_to_heading(section_slug))
            assert f"## {heading}" in text, f"paper template missing H2 for section {section_slug!r} (expected '## {heading}')"

    def test_a_slug_with_no_declared_heading_falls_back_to_generic_title_case(self) -> None:
        """`_slug_to_heading` no longer special-cases `tl_dr`; the spec is the one source for it now."""
        from engram.init.domain.scaffold import _slug_to_heading

        assert _slug_to_heading("tl_dr") == "Tl Dr"
        assert _slug_to_heading("open_questions") == "Open Questions"

    def test_a_spec_declared_heading_for_any_slug_renders_in_the_template(self) -> None:
        """Not just `tl_dr`: a scaffolded template honours whatever heading the spec declares."""
        from engram.init.contracts import scaffold

        spec = KnowledgeBaseSchema.model_validate(
            {
                "slug": "faq",
                "page_types": [{"page_type": "entry", "id_prefix": "e.", "canonical_sections": [{"slug": "q_and_a", "heading": "Q&A"}]}],
            }
        )

        text = scaffold(spec)["_meta/templates/tpl-entry.md"]

        assert "## Q&A" in text
        assert "## Q And A" not in text

    def test_paper_template_has_claim_stub(self) -> None:
        from engram.init.contracts import scaffold

        spec = research_spec()
        files = scaffold(spec)
        text = files["_meta/templates/tpl-paper.md"]
        assert "^c1" in text, "paper template missing claim anchor stub"

    def test_entity_template_sections_present(self) -> None:
        from engram.init.contracts import scaffold

        spec = research_spec()
        files = scaffold(spec)
        text = files["_meta/templates/tpl-entity.md"]
        # entity has: tl_dr, definition, mechanism, variants, evidence, trade_offs, open_questions
        assert "## TL;DR" in text
        assert "## Definition" in text

    def test_cooking_spec_templates_correct_count(self) -> None:
        from engram.init.contracts import scaffold

        spec = _cooking_spec()
        files = scaffold(spec)
        template_keys = [k for k in files if k.startswith("_meta/templates/tpl-")]
        # recipe + ingredient = 2 (no meta)
        assert len(template_keys) == 2

    def test_cooking_template_has_sections(self) -> None:
        from engram.init.contracts import scaffold

        spec = _cooking_spec()
        files = scaffold(spec)
        text = files["_meta/templates/tpl-recipe.md"]
        assert "## Overview" in text
        assert "## Ingredients" in text
        assert "## Method" in text


# ---------------------------------------------------------------------------
# 4. knowledge-base-schema.json round-trip
# ---------------------------------------------------------------------------


class TestKnowledgeBaseSchemaJson:
    def test_knowledge_base_schema_roundtrips(self) -> None:
        from engram.init.contracts import scaffold

        spec = research_spec()
        files = scaffold(spec)
        json_text = files["knowledge-base-schema.json"]
        restored = KnowledgeBaseSchema.from_json(json_text)
        assert restored == spec

    def test_a_headingless_kinds_sections_scaffold_as_bare_strings_not_objects(self) -> None:
        """A fresh, headingless knowledge base's `knowledge-base-schema.json` stays the compact plain-string form."""
        from engram.init.contracts import scaffold

        spec = _cooking_spec()
        json_text = scaffold(spec)["knowledge-base-schema.json"]

        recipe = next(pt for pt in json.loads(json_text)["page_types"] if pt["page_type"] == "recipe")
        assert recipe["canonical_sections"] == ["overview", "ingredients", "method"]

    def test_cooking_knowledge_base_schema_roundtrips(self) -> None:
        from engram.init.contracts import scaffold

        spec = _cooking_spec()
        files = scaffold(spec)
        json_text = files["knowledge-base-schema.json"]
        restored = KnowledgeBaseSchema.from_json(json_text)
        assert restored == spec


# ---------------------------------------------------------------------------
# 5. Prose/structural files present
# ---------------------------------------------------------------------------


class TestProseFiles:
    def test_purpose_md_present(self) -> None:
        from engram.init.contracts import scaffold

        spec = research_spec()
        files = scaffold(spec)
        assert "_meta/purpose.md" in files

    def test_purpose_md_contains_purpose(self) -> None:
        from engram.init.contracts import scaffold

        spec = research_spec()
        files = scaffold(spec)
        text = files["_meta/purpose.md"]
        # spec.purpose is non-empty for research
        assert spec.purpose[:20] in text

    def test_schema_md_present(self) -> None:
        from engram.init.contracts import scaffold

        spec = research_spec()
        files = scaffold(spec)
        assert "_meta/schema.md" in files

    def test_schema_md_contains_page_types(self) -> None:
        from engram.init.contracts import scaffold

        spec = research_spec()
        files = scaffold(spec)
        text = files["_meta/schema.md"]
        for pt in spec.page_types:
            assert pt.page_type in text

    def test_schema_md_prefixless_kind_renders_an_honest_dash(self) -> None:
        """A page_type with no id prefix must not render an empty backtick pair (`` ` ` ``) in the schema table."""
        from engram.init.contracts import scaffold

        spec = KnowledgeBaseSchema.model_validate(
            {"slug": "estate", "page_types": [{"page_type": "adr", "id_prefix": "", "canonical_sections": ["context"]}]}
        )
        text = scaffold(spec)["_meta/schema.md"]
        assert "| adr | `` | context | no |" not in text
        assert "| adr | - | context | no |" in text

    def test_agents_md_present(self) -> None:
        from engram.init.contracts import scaffold

        spec = research_spec()
        files = scaffold(spec)
        assert "AGENTS.md" in files

    def test_claude_md_present(self) -> None:
        from engram.init.contracts import scaffold

        spec = research_spec()
        files = scaffold(spec)
        assert "CLAUDE.md" in files

    def test_agents_md_mentions_slug(self) -> None:
        from engram.init.contracts import scaffold

        spec = research_spec()
        files = scaffold(spec)
        assert "research" in files["AGENTS.md"]

    def test_agents_no_absolute_paths(self) -> None:
        """AGENTS.md must not hardcode any absolute filesystem path."""
        from engram.init.contracts import scaffold

        spec = research_spec()
        files = scaffold(spec)
        text = files["AGENTS.md"]
        assert "/Users/" not in text
        assert "/home/" not in text


# ---------------------------------------------------------------------------
# 6. Template frontmatter blank line (Fix 2)
# ---------------------------------------------------------------------------


class TestTemplateFrontmatterBlankLine:
    def test_template_has_blank_line_after_frontmatter(self) -> None:
        """The closing '---' of a template's YAML frontmatter must be followed by a blank line.

        Obsidian and most frontmatter parsers require a blank line between the
        closing '---' delimiter and the first body line for the YAML block to be
        recognised.  Without it the first H2 is treated as part of the raw text,
        not as a note body heading.
        """
        from engram.init.contracts import scaffold

        spec = research_spec()
        files = scaffold(spec)
        text = files["_meta/templates/tpl-paper.md"]
        # The frontmatter block ends with "---"; it must be followed by "\n\n" (blank line)
        assert "\n---\n\n" in text, "template frontmatter missing blank line after closing '---'"

    def test_template_frontmatter_splits_cleanly(self) -> None:
        """core.markdown.split_frontmatter must return non-empty frontmatter with 'type' key.

        Validates that the blank-line fix keeps the frontmatter parseable.  Template
        files use placeholder values (e.g. '<title>') so full memory validation
        will fail, but frontmatter extraction must succeed.
        """
        from core.markdown import split_frontmatter

        from engram.init.contracts import scaffold

        spec = research_spec()
        files = scaffold(spec)
        text = files["_meta/templates/tpl-paper.md"]
        fm, _body = split_frontmatter(text)
        # fm is a dict — it must be non-empty and contain the 'type' key
        assert fm, "split_frontmatter returned empty frontmatter for tpl-paper.md"
        assert "type" in fm, "frontmatter missing 'type' field in tpl-paper.md"


# ---------------------------------------------------------------------------
# 7. Template status defaults to the page_type's own vocabulary
# ---------------------------------------------------------------------------


class TestTemplateStatusDefault:
    def test_template_status_is_first_declared_status_value(self) -> None:
        """A page_type with a status_values vocabulary gets that vocabulary's first value, not 'stub'."""
        from engram.init.contracts import scaffold

        spec = KnowledgeBaseSchema.model_validate(
            {"slug": "estate", "page_types": [{"page_type": "adr", "id_prefix": "", "status_values": ["proposed", "accepted"]}]}
        )
        files = scaffold(spec)
        text = files["_meta/templates/tpl-adr.md"]
        assert "status: proposed" in text
        assert "status: stub" not in text

    def test_template_status_defaults_to_stub_when_kind_declares_no_vocabulary(self) -> None:
        """A page_type with no status_values keeps the grammar-wide 'stub' default."""
        from engram.init.contracts import scaffold

        spec = KnowledgeBaseSchema.model_validate({"slug": "estate", "page_types": [{"page_type": "note", "id_prefix": ""}]})
        text = scaffold(spec)["_meta/templates/tpl-note.md"]
        assert "status: stub" in text


# ---------------------------------------------------------------------------
# 8. Facet scope preserved on re-render (Fix 1)
# ---------------------------------------------------------------------------


class TestFacetScopePreservedOnRerender:
    def test_scoped_facet_heading_unchanged_after_rewrite(self) -> None:
        """Re-running write_scaffold must leave scoped facet H2 headings byte-identical.

        The 'tasks' facet in the research spec is scoped to
        'paper, entity, comparison, playbook, question' — not 'all'.  A second
        write_scaffold call (union-merge path) must preserve that scope annotation;
        it must NOT rewrite the heading to '## `tasks` — on all'.
        """
        import tempfile

        from pathlib import Path

        from engram.init.contracts import scaffold, write_scaffold

        spec = research_spec()
        files = scaffold(spec)

        with tempfile.TemporaryDirectory() as tmp:
            knowledge_base = Path(tmp)

            # First write — creates all files
            write_scaffold(knowledge_base, files)

            # Read the tasks heading from the freshly written facets.md
            facets_path = knowledge_base / "_meta/vocabulary/facets.md"
            first_text = facets_path.read_text(encoding="utf-8")
            tasks_line_first = next((ln for ln in first_text.splitlines() if ln.startswith("## `tasks`")), None)
            assert tasks_line_first is not None, "tasks H2 not found after first write"

            # Second write — triggers union-merge for the vocabulary file
            write_scaffold(knowledge_base, files)

            second_text = facets_path.read_text(encoding="utf-8")
            tasks_line_second = next((ln for ln in second_text.splitlines() if ln.startswith("## `tasks`")), None)
            assert tasks_line_second is not None, "tasks H2 not found after second write"
            assert tasks_line_first == tasks_line_second, (
                f"tasks facet scope changed on re-render:\n  before: {tasks_line_first!r}\n  after:  {tasks_line_second!r}"
            )

    def test_scoped_facet_heading_not_on_all(self) -> None:
        """The 'tasks' facet must NOT read '— on all' in the initial scaffold output.

        Confirms the scaffold itself emits the correct scope string from the spec.
        """
        from engram.init.contracts import scaffold

        spec = research_spec()
        files = scaffold(spec)
        text = files["_meta/vocabulary/facets.md"]
        tasks_line = next((ln for ln in text.splitlines() if ln.startswith("## `tasks`")), None)
        assert tasks_line is not None, "tasks H2 missing from facets.md"
        assert "on all" not in tasks_line, f"tasks facet incorrectly scoped to 'all'; got: {tasks_line!r}"


def test_the_ingestion_policy_page_is_scaffolded_from_the_spec_defaults() -> None:
    from core.markdown import split_frontmatter

    from engram.init.contracts import scaffold

    files = scaffold(research_spec())

    front, body = split_frontmatter(files["_meta/ingestion-policy.md"])

    assert front["id"] == "meta.ingestion-policy"
    assert front["hop_depth"] == 1
    assert front["per_source_cap"] == 5
    assert body.startswith("# Ingestion policy")
    assert "builds on it" in body


def test_a_seed_value_holding_a_pipe_survives_the_vocabulary_round_trip() -> None:
    from engram.kb.contracts import FacetSpec, schema_vocabulary_drift

    seeds = ("Trattoria | Northern Italian", "french")
    spec = _cooking_spec().model_copy(update={"facets": (FacetSpec(key="cuisine", scope=("recipe",), seed=seeds),)})

    index = _round_trip_index(spec)

    assert tuple(index.facets["cuisine"]) == seeds
    assert schema_vocabulary_drift(index, spec) == ()
