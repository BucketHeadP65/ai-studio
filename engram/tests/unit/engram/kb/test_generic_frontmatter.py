"""Tests for the generic frontmatter validation path in domain.py, resolver.py, and payload.py.

Covers:
- Generic-page_type happy path (cooking spec: recipe/ingredient, link-list, required field, facet field)
- Generic-page_type rejections (missing required / bad prefix / bad choices / unknown type)
- Research path unchanged (subclass discrimination + payload_json_schema parity)
- Resolver extras parity (generic link fields in __pydantic_extra__ are extracted)
"""

from __future__ import annotations

import pytest

from engram.kb.contracts import (
    BaseFrontmatter,
    CanonicalSection,
    FacetSpec,
    FrontmatterFieldSpec,
    KnowledgeBaseSchema,
    PageTypeSpec,
    UnitLink,
    parse_frontmatter,
)
from engram.kb.domain.note import _parse_frontmatter_generic
from engram.kb.exceptions import FrontmatterError

# ---------------------------------------------------------------------------
# Test fixtures: a tiny cooking spec
# ---------------------------------------------------------------------------


def _cooking_spec() -> KnowledgeBaseSchema:
    """A minimal spec with two page_types: recipe (required 'name', link-list 'pairs_with') and ingredient.

    recipe also has a facet (cuisines) to exercise _check_facets.
    """
    recipe = PageTypeSpec(
        page_type="recipe",
        id_prefix="rcp.",
        frontmatter_fields=(
            FrontmatterFieldSpec(name="name", type="scalar", required=True),
            FrontmatterFieldSpec(name="pairs_with", type="link-list"),
            FrontmatterFieldSpec(name="difficulty", type="scalar", choices=("easy", "medium", "hard")),
        ),
        edge_fields=("pairs_with",),
        canonical_sections=(CanonicalSection(slug="ingredients"), CanonicalSection(slug="method")),
    )
    ingredient = PageTypeSpec(
        page_type="ingredient", id_prefix="ing.", frontmatter_fields=(FrontmatterFieldSpec(name="category", type="scalar"),)
    )
    cuisines_facet = FacetSpec(key="cuisines", scope=("recipe",), seed=("italian", "french"))
    return KnowledgeBaseSchema(slug="cooking", page_types=(recipe, ingredient), facets=(cuisines_facet,))


def _recipe_data(*, pairs_with: list[str] | None = None, difficulty: str | None = None) -> dict[str, object]:
    """Minimal valid recipe frontmatter dict."""
    data: dict[str, object] = {"id": "rcp.carbonara", "type": "recipe", "title": "Carbonara", "status": "draft", "name": "Carbonara"}
    if pairs_with is not None:
        data["pairs_with"] = pairs_with
    if difficulty is not None:
        data["difficulty"] = difficulty
    return data


# ---------------------------------------------------------------------------
# Generic page_type happy path
# ---------------------------------------------------------------------------


class TestGenericKindHappyPath:
    """A generic page_type note parses, coerces links, and satisfies required/prefix/choices."""

    def test_parse_returns_base_frontmatter(self):
        spec = _cooking_spec()
        data = _recipe_data()

        fm = parse_frontmatter(data, spec)

        assert isinstance(fm, BaseFrontmatter)
        assert fm.id == "rcp.carbonara"
        assert fm.type == "recipe"

    def test_link_list_becomes_unit_links_in_extra(self):
        spec = _cooking_spec()
        data = _recipe_data(pairs_with=["[[chianti]]", "[[pecorino]]"])

        fm = parse_frontmatter(data, spec)

        # Generic link fields land in __pydantic_extra__
        assert fm.__pydantic_extra__ is not None
        pairs = fm.__pydantic_extra__["pairs_with"]
        assert isinstance(pairs, tuple)
        assert all(isinstance(lk, UnitLink) for lk in pairs)
        assert pairs[0].target == "chianti"
        assert pairs[1].target == "pecorino"

    def test_required_field_stored_in_extra(self):
        spec = _cooking_spec()
        data = _recipe_data()

        fm = parse_frontmatter(data, spec)

        assert fm.__pydantic_extra__ is not None
        assert fm.__pydantic_extra__["name"] == "Carbonara"

    def test_choices_accepted_when_in_set(self):
        spec = _cooking_spec()
        data = _recipe_data(difficulty="easy")

        fm = parse_frontmatter(data, spec)

        assert fm.__pydantic_extra__ is not None
        assert fm.__pydantic_extra__["difficulty"] == "easy"

    def test_link_fields_resolved_as_edges_by_resolver(self):
        """Resolver extracts Unit links from __pydantic_extra__ for generic page_types."""
        from engram.indexing.domain.resolver import _frontmatter_relations

        spec = _cooking_spec()
        data = _recipe_data(pairs_with=["[[chianti]]", "[[pecorino]]"])
        fm = parse_frontmatter(data, spec)

        pairs = _frontmatter_relations(fm)

        targets = [link.target for _, link in pairs]
        assert "chianti" in targets
        assert "pecorino" in targets


# ---------------------------------------------------------------------------
# Generic page_type rejections
# ---------------------------------------------------------------------------


class TestGenericKindRejections:
    """Missing required / bad prefix / bad choices / unknown type all raise FrontmatterError."""

    def test_unknown_type_raises_frontmatter_error(self):
        spec = _cooking_spec()
        with pytest.raises(FrontmatterError, match="unknown or missing page type"):
            parse_frontmatter({"id": "spaceship.x", "type": "spaceship", "title": "T", "status": "draft"}, spec)

    def test_missing_required_field_raises_frontmatter_error(self):
        spec = _cooking_spec()
        data: dict[str, object] = {
            "id": "rcp.carbonara",
            "type": "recipe",
            "title": "Carbonara",
            "status": "draft",
            # 'name' is required but absent
        }
        with pytest.raises(FrontmatterError, match="required field"):
            parse_frontmatter(data, spec)

    def test_empty_required_field_raises_frontmatter_error(self):
        """Present but blank is missing: an empty string reaches the database as a dangling key otherwise."""
        spec = _cooking_spec()
        data: dict[str, object] = {
            "id": "rcp.carbonara",
            "type": "recipe",
            "title": "Carbonara",
            "status": "draft",
            "name": "",
        }
        with pytest.raises(FrontmatterError, match="is empty"):
            parse_frontmatter(data, spec)

    def test_bad_id_prefix_raises_frontmatter_error(self):
        spec = _cooking_spec()
        data: dict[str, object] = {
            "id": "ing.carbonara",  # wrong prefix for recipe
            "type": "recipe",
            "title": "Carbonara",
            "status": "draft",
            "name": "Carbonara",
        }
        with pytest.raises(FrontmatterError, match="must start with"):
            parse_frontmatter(data, spec)

    def test_value_not_in_choices_raises_frontmatter_error(self):
        spec = _cooking_spec()
        data = _recipe_data(difficulty="impossible")
        with pytest.raises(FrontmatterError, match="choices") as exc_info:
            parse_frontmatter(data, spec)
        # Names the exact knowledge-base-schema.json path to edit, not just that a choices gate failed.
        assert "knowledge-base-schema.json page_types[page_type=recipe].frontmatter_fields[name=difficulty].choices" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Research path unchanged
# ---------------------------------------------------------------------------


class TestResearchPathUnchanged:
    """parse_frontmatter with a research page_type uses the single generic path (post-TA.4)."""

    def test_paper_returns_basefrontmatter_with_extras(self) -> None:
        """parse_frontmatter returns BaseFrontmatter; page_type-specific fields in model_extra."""
        from engram.kb.contracts import BaseFrontmatter
        from engram.testing.specs import research_spec

        spec = research_spec()
        data: dict[str, object] = {
            "id": "paper.lightrag-2025",
            "type": "paper",
            "title": "LightRAG",
            "status": "draft",
            "paper_type": "method",
            "year": 2025,
            "zotero_key": "K",
            "source_hash": "h",
        }

        fm = parse_frontmatter(data, spec)

        assert isinstance(fm, BaseFrontmatter)
        assert fm.type == "paper"
        assert fm.model_extra is not None
        assert fm.model_extra["year"] == 2025

    def test_entity_returns_basefrontmatter_with_extras(self) -> None:
        """parse_frontmatter returns BaseFrontmatter for entity; entity_type in model_extra."""
        from engram.kb.contracts import BaseFrontmatter
        from engram.testing.specs import research_spec

        spec = research_spec()
        data: dict[str, object] = {
            "id": "ent.lightrag",
            "type": "entity",
            "title": "LightRAG",
            "status": "draft",
            "entity_type": "technique",
        }

        fm = parse_frontmatter(data, spec)

        assert isinstance(fm, BaseFrontmatter)
        assert fm.type == "entity"
        assert fm.model_extra is not None
        assert fm.model_extra["entity_type"] == "technique"

    def test_payload_json_schema_paper_has_the_declared_fields(self) -> None:
        """payload_json_schema('paper', spec) names the paper page_type's own declared fields."""
        from engram.kb.contracts import payload_json_schema
        from engram.testing.specs import research_spec

        spec = research_spec()

        schema = payload_json_schema("paper", spec)

        assert schema["properties"]["frontmatter"]["properties"]["paper_type"]


# ---------------------------------------------------------------------------
# Resolver extras parity
# ---------------------------------------------------------------------------


class TestResolverExtrasParity:
    """_frontmatter_relations also scans __pydantic_extra__ for generic-page_type Unit links."""

    def test_research_note_extras_yield_no_unit_links(self) -> None:
        """A research note's string extras (citekey/venue) — scanning them adds zero edges."""
        from engram.indexing.domain.resolver import _frontmatter_relations
        from engram.testing.specs import research_spec

        spec = research_spec()
        data: dict[str, object] = {
            "id": "paper.lightrag-2025",
            "type": "paper",
            "title": "LightRAG",
            "status": "draft",
            "paper_type": "method",
            "year": 2025,
            "zotero_key": "K",
            "source_hash": "h",
            "proposes": ["[[lightrag]]"],
        }
        fm = parse_frontmatter(data, spec)
        # Inject a string extra (simulates citekey)
        fm_with_extra = fm.model_copy(update={"citekey": "guo2025lightrag"})

        relations_base = list(_frontmatter_relations(fm))
        relations_with_extra = list(_frontmatter_relations(fm_with_extra))

        # Both should have the same edges (only 'proposes' + superseded_by if any)
        assert set(relations_base) == set(relations_with_extra)

    def test_generic_note_extra_unit_links_extracted(self):
        """Generic-page_type Unit links in __pydantic_extra__ appear in _frontmatter_relations output."""
        from engram.indexing.domain.resolver import _frontmatter_relations

        spec = _cooking_spec()
        data = _recipe_data(pairs_with=["[[chianti]]", "[[pecorino]]"])
        fm = parse_frontmatter(data, spec)

        relations = _frontmatter_relations(fm)

        pairs_with_edges = [(rel, lk) for rel, lk in relations if rel == "pairs_with"]
        assert len(pairs_with_edges) == 2
        targets = {lk.target for _, lk in pairs_with_edges}
        assert targets == {"chianti", "pecorino"}

    def test_generic_note_extra_single_link_extracted(self):
        """A single link-typed extra field is also extracted."""
        from engram.indexing.domain.resolver import _frontmatter_relations

        # Build a spec with a single link field
        spec = KnowledgeBaseSchema(
            slug="test",
            page_types=(
                PageTypeSpec(
                    page_type="chapter",
                    id_prefix="ch.",
                    frontmatter_fields=(FrontmatterFieldSpec(name="follows", type="link"),),
                    edge_fields=("follows",),
                ),
            ),
        )
        data: dict[str, object] = {"id": "ch.one", "type": "chapter", "title": "Chapter One", "status": "draft", "follows": "[[ch.zero]]"}

        fm = _parse_frontmatter_generic(data, spec, "chapter")

        relations = _frontmatter_relations(fm)

        follows_edges = [(rel, lk) for rel, lk in relations if rel == "follows"]
        assert len(follows_edges) == 1
        assert follows_edges[0][1].target == "ch.zero"


# ---------------------------------------------------------------------------
# Custom facet check
# ---------------------------------------------------------------------------


class TestFacetFieldInGenericKind:
    """A generic-page_type facet's values land in `__pydantic_extra__` under its own key."""

    def test_facet_field_accepted_in_data(self):
        """A facet key (cuisines) is present in parsed extra."""
        spec = _cooking_spec()
        data = _recipe_data()
        data["cuisines"] = ["italian", "french"]

        fm = parse_frontmatter(data, spec)

        assert fm.__pydantic_extra__ is not None
        assert "cuisines" in fm.__pydantic_extra__


def test_an_id_with_a_folded_yaml_line_is_refused_with_the_cause_named() -> None:
    """A list line indented under `id:` folds into the scalar; the id alphabet check names that, so index fails loudly."""
    data: dict[str, object] = {
        "id": 'rcp.carbonara - "[[rcp.carbonara-js]]"',
        "type": "recipe",
        "title": "Carbonara",
        "status": "draft",
        "name": "Carbonara",
    }
    with pytest.raises(FrontmatterError, match="folds into the id"):
        parse_frontmatter(data, _cooking_spec())


@pytest.mark.parametrize("bad", ["rcp.a b", "rcp.'quoted'", "rcp.[x]", " rcp.lead", "rcp.tail ", "rcp.a\nb"])
def test_ids_with_whitespace_quotes_or_brackets_are_refused(bad: str) -> None:
    data: dict[str, object] = {"id": bad, "type": "recipe", "title": "T", "status": "draft", "name": "T"}
    with pytest.raises(FrontmatterError, match="is not a page id"):
        parse_frontmatter(data, _cooking_spec())


@pytest.mark.parametrize("good", ["rcp.carbonara", "rcp.carbonara-2024_v2", "rcp.a/b", "rcp.x@y", "rcp.a+b:c"])
def test_ids_in_the_alphabet_pass(good: str) -> None:
    data: dict[str, object] = {"id": good, "type": "recipe", "title": "T", "status": "draft", "name": "T"}
    assert parse_frontmatter(data, _cooking_spec()).id == good
