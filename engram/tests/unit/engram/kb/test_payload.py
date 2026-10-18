"""Unit tests for the authoring payload (`src/engram/memory/payload.py`).

The payload is the extractor JSON contract: instead of hand-writing markdown,
an ingestion extractor emits an :class:`AuthoringPayload`
that `memory note build` renders + validates into canonical `.md`. These tests
pin the *construction-time* guarantees the renderer relies on:

* the `frontmatter` dict is validated through `parse_frontmatter` (the single
  generic path) so a bad `type` / missing required key / wrong id-prefix is
  rejected at build, not discovered at render;
* claim anchors auto-assign `c1..cN` in payload order when omitted, and an
  explicit `^cN` (or bare `cN`) anchor is respected verbatim;
* sections default to H2 (`level == 2`) and accept an explicit H3 level;
* :func:`payload_json_schema` exports a per-page-type JSON schema whose properties
  include that type's distinguishing frontmatter fields (the `memory note schema`
  contract).

No monkeypatching, no mocks: the payload is a pure pydantic model.
"""

from __future__ import annotations

import pydantic
import pytest

from core.markdown import heading_slug
from pydantic import JsonValue

from engram.kb.contracts import (
    AuthoringPayload,
    BaseFrontmatter,
    CanonicalSection,
    ClaimGrammarSpec,
    KnowledgeBaseSchema,
    PageTypeSpec,
    UnitLink,
    parse,
    payload_json_schema,
    render,
)
from engram.kb.domain.payload import AuthoringClaim, AuthoringSection, assign_anchors
from engram.testing.specs import minimal_spec, research_spec


def _paper_frontmatter(**overrides: JsonValue) -> dict[str, JsonValue]:
    """A minimal valid `paper` frontmatter dict (the id-prefix validator must pass)."""
    base: dict[str, JsonValue] = {
        "id": "paper.sample-2025",
        "type": "paper",
        "title": "Sample",
        "status": "draft",
        "paper_type": "method",
        "year": 2025,
        "zotero_key": "K",
        "source_hash": "h",
    }
    base.update(overrides)
    return base


class TestFrontmatterValidation:
    """The frontmatter dict is validated through the single generic path on construction."""

    def test_valid_paper_frontmatter_accepted(self) -> None:
        payload = AuthoringPayload(frontmatter=_paper_frontmatter(), sections=(), claims=(), knowledge_base_schema=research_spec())

        assert payload.page_type == "paper"
        assert payload.frontmatter["id"] == "paper.sample-2025"

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            AuthoringPayload(frontmatter=_paper_frontmatter(type="not-a-type"), sections=(), claims=(), knowledge_base_schema=research_spec())

    def test_missing_required_paper_key_rejected(self) -> None:
        # `year` is required in the research spec for paper — its absence must fail at build.
        bad = _paper_frontmatter()
        del bad["year"]

        with pytest.raises(pydantic.ValidationError):
            AuthoringPayload(frontmatter=bad, sections=(), claims=(), knowledge_base_schema=research_spec())

    def test_wrong_id_prefix_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            AuthoringPayload(frontmatter=_paper_frontmatter(id="ent.sample"), sections=(), claims=(), knowledge_base_schema=research_spec())

    def test_validated_frontmatter_exposes_typed_model(self) -> None:
        payload = AuthoringPayload(frontmatter=_paper_frontmatter(year=2024), sections=(), claims=(), knowledge_base_schema=research_spec())

        frontmatter = payload.validated_frontmatter()
        assert isinstance(frontmatter, BaseFrontmatter)
        assert frontmatter.model_extra is not None
        assert frontmatter.model_extra["year"] == 2024


class TestAnchorAssignment:
    """Claim anchors auto-assign `c1..cN` when omitted; explicit anchors are respected."""

    def test_absent_anchors_assigned_sequentially(self) -> None:
        claims = (AuthoringClaim(text="first", loc="§1"), AuthoringClaim(text="second", loc="§2"), AuthoringClaim(text="third", loc="§3"))

        assigned = assign_anchors(claims)

        assert [c.anchor for c in assigned] == ["^c1", "^c2", "^c3"]

    def test_explicit_anchor_respected(self) -> None:
        claims = (AuthoringClaim(text="a", loc="§1", anchor="^c7"),)

        assigned = assign_anchors(claims)

        assert assigned[0].anchor == "^c7"

    def test_bare_anchor_normalised_to_caret_form(self) -> None:
        # An extractor may write 'c4' without the caret; normalise to '^c4'.
        claims = (AuthoringClaim(text="a", loc="§1", anchor="c4"),)

        assigned = assign_anchors(claims)

        assert assigned[0].anchor == "^c4"

    def test_mixed_explicit_and_auto_skips_used_numbers(self) -> None:
        # An explicit ^c1 plus two auto claims: the auto ones must not collide with c1.
        claims = (
            AuthoringClaim(text="auto-a", loc="§1"),
            AuthoringClaim(text="pinned", loc="§2", anchor="^c1"),
            AuthoringClaim(text="auto-b", loc="§3"),
        )

        anchors = [c.anchor for c in assign_anchors(claims)]

        assert "^c1" in anchors
        assert len(set(anchors)) == len(anchors)  # unique


class TestSectionLevel:
    """Authoring sections default to H2; an explicit level is honoured."""

    def test_section_defaults_to_h2(self) -> None:
        section = AuthoringSection(heading="Approach", body="text")

        assert section.level == 2

    def test_section_accepts_h3(self) -> None:
        section = AuthoringSection(heading="Graph indexing", body="text", level=3)

        assert section.level == 3


class TestSectionCanonAlias:
    """`canon` (a canonical section slug) is an alternative to `heading`; both present must agree.

    `canon` is not a declared `AuthoringSection` field — it's consumed by a `mode="before"`
    validator on the raw payload dict, the shape an extractor actually emits — so these go
    through `model_validate` rather than keyword construction.
    """

    def test_canon_only_derives_heading_and_round_trips(self) -> None:
        section = AuthoringSection.model_validate({"canon": "tl_dr", "body": "text"})

        assert heading_slug(section.heading) == "tl_dr"

    def test_heading_and_consistent_canon_keeps_the_given_heading(self) -> None:
        section = AuthoringSection.model_validate({"heading": "TL;DR", "canon": "tl_dr", "body": "text"})

        assert section.heading == "TL;DR"

    def test_heading_and_conflicting_canon_raises_naming_both(self) -> None:
        with pytest.raises(pydantic.ValidationError) as exc_info:
            AuthoringSection.model_validate({"heading": "TL;DR", "canon": "open_questions", "body": "text"})

        message = str(exc_info.value)
        assert "TL;DR" in message
        assert "open_questions" in message


class TestClaimEdgesAndQualifiers:
    """An authoring claim mirrors `ClaimBlock` fields with authoring ergonomics."""

    def test_edges_and_multilink_dataset_accept_raw_strings(self) -> None:
        claim = AuthoringClaim(
            text="LightRAG beats GraphRAG",
            loc="§4.2",
            edges=(("outperforms", "[[graphrag]]"),),
            links={"on_dataset": ("[[ultradomain]]",), "metric": ("[[llm-judged-win-rate]]",)},
        )

        assert claim.edges == (("outperforms", UnitLink(target="graphrag")),)
        assert claim.links["on_dataset"] == (UnitLink(target="ultradomain"),)
        assert claim.links["metric"] == (UnitLink(target="llm-judged-win-rate"),)

    def test_lifecycle_fields_flow_through_to_block(self) -> None:
        # status/disputed_by (schema rule 8) accept raw strings and survive to_block().
        claim = AuthoringClaim(text="A.", loc="§1", anchor="^c1", status="disputed", disputed_by=("[[paper.x#^c2]]",))

        block = claim.to_block()

        assert block.status == "disputed"
        assert block.disputed_by == (UnitLink(target="paper.x", block="c2"),)


class TestJsonSchemaExport:
    """`payload_json_schema(page_type)` carries that type's distinguishing fields.

    The schema is built purely from the spec (no pydantic subclass generation), so it lives at
    `schema["properties"]["frontmatter"]["properties"]` directly — no `$defs` indirection. Field
    names, JSON types, required set, and enum constraints are all spec-driven.
    """

    def test_paper_schema_has_paper_fields(self) -> None:
        schema = payload_json_schema("paper", spec=research_spec())
        properties = _frontmatter_properties(schema)

        assert "zotero_key" in properties
        assert "year" in properties
        assert "proposes" in properties

    def test_entity_schema_has_entity_fields(self) -> None:
        schema = payload_json_schema("entity", spec=research_spec())
        properties = _frontmatter_properties(schema)

        assert "entity_type" in properties
        assert "zotero_key" not in properties  # paper-only key absent from the entity schema

    def test_schema_describes_claims_and_sections(self) -> None:
        schema = payload_json_schema("paper", spec=research_spec())

        assert "claims" in schema["properties"]
        assert "sections" in schema["properties"]


def _frontmatter_properties(schema: dict[str, object]) -> dict[str, object]:
    """Resolve the `frontmatter` sub-schema's properties from a payload JSON schema.

    The spec-built schema inlines properties directly under
    `schema["properties"]["frontmatter"]["properties"]` — there is no `$defs` indirection.
    """
    top_props = schema.get("properties", {})
    assert isinstance(top_props, dict)
    frontmatter_schema = top_props.get("frontmatter", {})
    assert isinstance(frontmatter_schema, dict)
    properties = frontmatter_schema.get("properties")
    assert isinstance(properties, dict), f"expected 'properties' key in frontmatter schema, got: {frontmatter_schema!r}"
    return properties


def _frontmatter_required(schema: dict[str, object]) -> list[str]:
    """Resolve the `frontmatter` sub-schema's required list from a payload JSON schema."""
    top_props = schema.get("properties", {})
    assert isinstance(top_props, dict)
    frontmatter_schema = top_props.get("frontmatter", {})
    assert isinstance(frontmatter_schema, dict)
    required = frontmatter_schema.get("required", [])
    assert isinstance(required, list)
    return required


class TestPayloadJsonSchemaResearchFields:
    """Spec-built schema has the correct field names, JSON types, required set, and enum constraints.

    This is the Task A.4 requirement: for each research page_type, the spec-built schema
    MUST convey the correct field names, JSON types, and required set (and choices→enum).
    """

    def test_paper_properties_and_required(self) -> None:
        schema = payload_json_schema("paper", spec=research_spec())
        props = _frontmatter_properties(schema)
        required = _frontmatter_required(schema)

        # Kind-specific required fields present
        assert "paper_type" in props
        assert "year" in props
        assert "zotero_key" in props
        assert "source_hash" in props
        # Universal fields present
        assert "id" in props
        assert "title" in props
        assert "status" in props

        # year is integer-typed
        year_schema = props["year"]
        assert isinstance(year_schema, dict)
        assert year_schema.get("type") == "integer"

        # Required set includes paper's required fields
        for field in ("id", "type", "title", "status", "paper_type", "year", "zotero_key", "source_hash"):
            assert field in required, f"expected {field!r} in required, got {required!r}"

    def test_entity_properties_and_required(self) -> None:
        schema = payload_json_schema("entity", spec=research_spec())
        props = _frontmatter_properties(schema)
        required = _frontmatter_required(schema)

        assert "entity_type" in props
        assert "entity_type" in required
        # paper-only keys must be absent
        assert "year" not in props
        assert "zotero_key" not in props

    def test_question_choices_field_emits_enum(self) -> None:
        schema = payload_json_schema("question", spec=research_spec())
        props = _frontmatter_properties(schema)

        assert "question_status" in props
        qs = props["question_status"]
        assert isinstance(qs, dict)
        enum_values = qs.get("enum")
        assert isinstance(enum_values, list)
        assert set(enum_values) == {"open", "answered"}


class TestSectionsItemSchema:
    """`sections.items` carries `AuthoringSection`'s real shape, plus a canon-slug hint."""

    def test_section_item_has_heading_body_level(self) -> None:
        schema = payload_json_schema("paper", spec=research_spec())
        item = schema["properties"]["sections"]["items"]
        props = item["properties"]

        assert props["heading"]["type"] == "string"
        assert props["body"]["type"] == "string"
        assert props["level"] == {"type": "integer", "default": 2}
        assert item["required"] == ["body"]

    def test_heading_or_canon_one_required(self) -> None:
        # A section entry needs heading OR canon (body is unconditionally required, above);
        # neither field alone is hard-required, the choice is expressed as anyOf.
        schema = payload_json_schema("paper", spec=research_spec())
        item = schema["properties"]["sections"]["items"]

        assert item["anyOf"] == [{"required": ["heading"]}, {"required": ["canon"]}]

    def test_canon_property_present_and_string_typed(self) -> None:
        schema = payload_json_schema("paper", spec=research_spec())
        canon_schema = schema["properties"]["sections"]["items"]["properties"]["canon"]

        assert canon_schema["type"] == "string"
        assert "heading" in canon_schema["description"]

    def test_canonical_sections_surfaced_without_hard_enum(self) -> None:
        # paper has canonical_sections in the research spec; the slugs must be discoverable,
        # but 'heading' stays free text since non-canonical headings are legal.
        schema = payload_json_schema("paper", spec=research_spec())
        heading_schema = schema["properties"]["sections"]["items"]["properties"]["heading"]

        assert "enum" not in heading_schema
        description = heading_schema.get("description", "")
        assert "tl_dr" in description
        assert "findings" in description

    def test_kind_with_no_canonical_sections_gets_no_hint(self) -> None:
        # meta has canonical_sections=() in the research spec.
        schema = payload_json_schema("meta", spec=research_spec())
        heading_schema = schema["properties"]["sections"]["items"]["properties"]["heading"]

        assert "description" not in heading_schema


class TestClaimsItemSchema:
    """`claims.items` carries `AuthoringClaim`'s real shape, gated by the spec's claim grammar."""

    def test_claims_enabled_kind_has_real_claim_properties(self) -> None:
        # paper has claims_enabled=True in the research spec.
        schema = payload_json_schema("paper", spec=research_spec())
        item = schema["properties"]["claims"]["items"]
        props = item["properties"]

        assert item["required"] == ["text", "loc"]
        assert props["subject"]["anyOf"][0]["type"] == "string"
        assert props["on_dataset"]["type"] == "array"
        assert props["condition"]["anyOf"][0]["type"] == "string"

    def test_claim_kind_enumerates_the_knowledge_base_vocabulary(self) -> None:
        schema = payload_json_schema("paper", spec=research_spec())
        page_type_schema = schema["properties"]["claims"]["items"]["properties"]["kind"]

        assert set(page_type_schema["enum"]) == {
            "benchmark-result",
            "ablation",
            "replication",
            "negative-result",
            "theoretical",
            "qualitative",
            "author-claim",
            "observation",
        }

    def test_claim_kind_enum_carries_each_kinds_definition(self) -> None:
        # An agent choosing between eight hyphenated slugs needs their definitions, not just
        # the closed vocabulary itself.
        schema = payload_json_schema("paper", spec=research_spec())
        page_type_schema = schema["properties"]["claims"]["items"]["properties"]["kind"]
        description = page_type_schema.get("description", "")

        assert "benchmark-result: Quantitative result on a named dataset/metric" in description
        assert "ablation: Component-removal evidence" in description
        # One description line per enum member, so every page_type's definition surfaces, not just some.
        assert description.count("\n") == len(page_type_schema["enum"]) - 1

    def test_edges_constrained_to_declared_relations(self) -> None:
        schema = payload_json_schema("paper", spec=research_spec())
        edges_schema = schema["properties"]["claims"]["items"]["properties"]["edges"]
        relation_slot = edges_schema["items"]["prefixItems"][0]

        assert set(relation_slot["enum"]) == {"supports", "refutes", "replicates", "outperforms", "comparable_to"}

    def test_quote_not_hard_required_despite_quote_required_kinds(self) -> None:
        # quote_required_types is an index-validation rule, not an AuthoringClaim constructor
        # constraint, so the schema documents it without demanding more than the model does.
        schema = payload_json_schema("paper", spec=research_spec())
        item = schema["properties"]["claims"]["items"]

        assert "quote" not in item["required"]
        description = item["properties"]["quote"].get("description", "")
        assert "benchmark-result" in description
        assert "ablation" in description

    def test_claims_disabled_kind_signals_not_applicable(self) -> None:
        # entity has claims_enabled=False in the research spec.
        schema = payload_json_schema("entity", spec=research_spec())
        claims_schema = schema["properties"]["claims"]

        assert claims_schema["maxItems"] == 0
        assert claims_schema["items"] == {"type": "object"}
        assert "description" in claims_schema


class TestPayloadSchemaFacets:
    """The payload schema offers exactly the facets the knowledge base declares for the page type."""

    def test_a_knowledge_base_that_declares_no_facets_offers_none(self) -> None:
        properties = _frontmatter_properties(payload_json_schema("note", spec=minimal_spec()))

        assert not {"tasks", "domains", "stages", "modalities", "scale_regimes"} & set(properties)

    def test_a_declared_facet_is_offered_to_the_page_types_in_its_scope(self) -> None:
        paper = _frontmatter_properties(payload_json_schema("paper", spec=research_spec()))
        entity = _frontmatter_properties(payload_json_schema("entity", spec=research_spec()))

        assert paper["tasks"] == {"type": "array", "items": {"type": "string"}, "default": []}
        assert "paper_type" in paper
        assert "domains" not in entity


def _declared_spec() -> KnowledgeBaseSchema:
    """A one-page-type knowledge base declaring a text field, a multilink field and a link scalar field of its own."""
    return KnowledgeBaseSchema(
        slug="custom",
        page_types=(
            PageTypeSpec(page_type="note", id_prefix="note", canonical_sections=(CanonicalSection(slug="findings"),), claims_enabled=True),
        ),
        claims=ClaimGrammarSpec(
            section="findings",
            edge_relations=("governs",),
            multilink_fields=("applies_to",),
            link_scalar_fields=("governed_by",),
            text_scalar_fields=("jurisdiction",),
        ),
    )


def _declared_payload(*claims: dict[str, JsonValue]) -> AuthoringPayload:
    """A `note` payload carrying `claims` as raw claim objects, as a writer emits them."""
    return AuthoringPayload.model_validate(
        {
            "frontmatter": {"id": "note.x", "type": "note", "title": "X", "status": "draft"},
            "claims": list(claims),
            "knowledge_base_schema": _declared_spec(),
        }
    )


class TestDeclaredClaimFields:
    """A claim names each declared field as its own key, and the payload keeps it in `fields` or `links`."""

    def test_declared_keys_land_in_fields_and_links(self) -> None:
        payload = _declared_payload(
            {"text": "A.", "loc": "§1", "jurisdiction": "EU", "applies_to": ["[[a]]", "[[b]]"], "governed_by": "[[c]]"}
        )

        claim = payload.claims[0]

        assert claim.fields == {"jurisdiction": "EU"}
        assert claim.links == {"applies_to": (UnitLink(target="a"), UnitLink(target="b")), "governed_by": (UnitLink(target="c"),)}

    def test_the_claim_converts_to_the_block_its_rendered_page_parses_to(self) -> None:
        payload = _declared_payload(
            {"text": "A.", "loc": "§1", "jurisdiction": "EU", "applies_to": ["[[a]]", "[[b]]"], "governed_by": "[[c]]"}
        )

        block = assign_anchors(payload.claims)[0].to_block()

        assert block == parse(render(payload, spec=_declared_spec()), source_path="p.md", spec=_declared_spec()).claims[0]

    def test_an_unset_declared_key_is_left_out(self) -> None:
        payload = _declared_payload({"text": "A.", "loc": "§1", "jurisdiction": None, "applies_to": [], "governed_by": None})

        assert payload.claims[0].fields == {}
        assert payload.claims[0].links == {}

    def test_an_undeclared_key_is_refused_naming_the_key_and_the_declared_fields(self) -> None:
        with pytest.raises(pydantic.ValidationError) as exc:
            _declared_payload({"text": "A.", "loc": "§1", "mood": "cheerful"})

        message = str(exc.value)
        assert "'mood'" in message
        assert "jurisdiction" in message
        assert "applies_to" in message

    def test_an_edge_relation_key_is_refused_naming_the_edges_shape(self) -> None:
        with pytest.raises(pydantic.ValidationError) as exc:
            _declared_payload({"text": "A.", "loc": "§1", "governs": "[[c]]"})

        assert "edges" in str(exc.value)

    def test_a_link_scalar_key_with_two_links_is_refused(self) -> None:
        with pytest.raises(pydantic.ValidationError) as exc:
            _declared_payload({"text": "A.", "loc": "§1", "governed_by": ["[[a]]", "[[b]]"]})

        assert "governed_by" in str(exc.value)

    def test_the_claim_schema_names_each_declared_field(self) -> None:
        properties = payload_json_schema("note", spec=_declared_spec())["properties"]["claims"]["items"]["properties"]

        assert properties["jurisdiction"]["anyOf"][0]["type"] == "string"
        assert properties["applies_to"]["type"] == "array"
        assert properties["governed_by"]["anyOf"][0]["type"] == "string"
        assert {"loc", "quote", "kind", "subject", "edges", "status", "disputed_by"} <= set(properties)
        assert "fields" not in properties
        assert "links" not in properties

    @pytest.mark.parametrize(("nested", "value"), [("fields", {"jurisdiction": "EU"}), ("links", {"applies_to": ["[[a]]"]})])
    def test_a_nested_fields_or_links_key_is_refused_naming_the_flat_form(self, nested: str, value: dict[str, JsonValue]) -> None:
        with pytest.raises(pydantic.ValidationError) as exc:
            _declared_payload({"text": "A.", "loc": "§1", nested: value})

        (error,) = exc.value.errors()
        assert error["loc"] == ("claims", 0, nested)
        assert "a key of its own" in error["msg"]
        assert "applies_to, governed_by, jurisdiction" in error["msg"]

    def test_a_text_field_of_the_wrong_type_is_reported_at_its_own_key(self) -> None:
        with pytest.raises(pydantic.ValidationError) as exc:
            _declared_payload({"text": "A.", "loc": "§1", "jurisdiction": 5})

        (error,) = exc.value.errors()
        assert error["loc"] == ("claims", 0, "jurisdiction")
        assert error["type"] == "string_type"

    def test_a_multilink_field_given_one_string_is_reported_at_its_own_key(self) -> None:
        with pytest.raises(pydantic.ValidationError) as exc:
            _declared_payload({"text": "A.", "loc": "§1", "applies_to": "[[a]]"})

        (error,) = exc.value.errors()
        assert error["loc"] == ("claims", 0, "applies_to")
        assert "list of [[page]] links" in error["msg"]

    def test_a_link_without_brackets_is_reported_at_its_own_key_and_position(self) -> None:
        with pytest.raises(pydantic.ValidationError) as exc:
            _declared_payload({"text": "A.", "loc": "§1", "applies_to": ["[[a]]", "plain"], "governed_by": "also plain"})

        errors = {error["loc"]: error["msg"] for error in exc.value.errors()}
        assert set(errors) == {("claims", 0, "applies_to", 1), ("claims", 0, "governed_by")}
        assert "field 'applies_to' takes [[page]] links, got 'plain'" in errors["claims", 0, "applies_to", 1]
        assert "field 'governed_by' takes [[page]] links, got 'also plain'" in errors["claims", 0, "governed_by"]

    def test_every_refused_key_of_every_claim_is_reported_at_once(self) -> None:
        with pytest.raises(pydantic.ValidationError) as exc:
            _declared_payload({"text": "A.", "loc": "§1", "mood": "x"}, {"text": "B.", "loc": "§2", "governs": "[[c]]"})

        assert [error["loc"] for error in exc.value.errors()] == [("claims", 0, "mood"), ("claims", 1, "governs")]
