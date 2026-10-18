"""Unit tests for the payload -> canonical-markdown renderer (`src/engram/memory/renderer.py`).

These pin the binding *renderer invariants*:

* **round-trip:** `parse(render(payload))` is a semantically-equal
  :class:`KnowledgeBaseNote` — every frontmatter field, the section structure, and every
  claim field (including the `quote::` outer-quote round-trip) survive;
* **idempotent re-render:** `render(parse(render(payload)))` equals the first
  render byte-for-byte — the renderer has one stable output for one note;
* **stable claim-field order + anchor formatting:** claim child fields emit in a
  fixed order (`loc, quote, kind, subject, edges`, the declared link and text
  fields in the claim grammar's order, `status, disputed_by`) and anchors
  as `- {text} ^cN`;
* **canonical H2 ordering:** sections the author supplies in non-canonical order
  are emitted in their page type's canonical order, with unknown H2s after in
  original relative order;
* **Obsidian-dialect correctness:** unit links re-emit as `[[target|alias]]` /
  `[[target#^block]]` etc.

The renderer returns a *string* (file IO is the CLI's job). No monkeypatching,
no mocks: render and parse are pure functions, exercised against the existing
domain factories plus one hand-written realistic paper payload.
"""

from __future__ import annotations

import pathlib

from typing import TYPE_CHECKING

from engram.kb.contracts import (
    AuthoringPayload,
    BaseFrontmatter,
    CanonicalSection,
    ClaimBlock,
    ClaimGrammarSpec,
    KnowledgeBaseNote,
    KnowledgeBaseSchema,
    PageTypeSpec,
    UnitLink,
    parse,
    render,
)
from engram.kb.domain.payload import AuthoringClaim, AuthoringSection
from engram.kb.domain.renderer import _CLAIM_FIELD_ORDER, _CLAIM_HEADER_FIELDS
from engram.testing.knowledge_base_builder import GOLDEN_LIGHTRAG_PAPER
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from pydantic import JsonValue

_GOLDEN = GOLDEN_LIGHTRAG_PAPER

_PAPER_FM_TEXT = """---
id: paper.sample-2025
type: paper
title: Sample
status: draft
paper_type: method
year: 2025
zotero_key: K
source_hash: h
---
"""


def _paper_text(body: str) -> str:
    """Raw markdown source (frontmatter fence + body) for a parser-facing round-trip test."""
    return _PAPER_FM_TEXT + "\n" + body


def _paper_frontmatter(**overrides: JsonValue) -> dict[str, JsonValue]:
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


def _realistic_paper_payload() -> AuthoringPayload:
    """A hand-written paper payload modelled on lightrag-2025's shape.

    Three claims exercise the hard round-trip cases: a benchmark-result with a
    re-wrappable quote + multi-link `on_dataset` + a comparative edge +
    qualifiers; a table-derived claim whose quote has interior quotes (must NOT
    be re-wrapped); and a bare observation with a subject and no edges. Sections
    are deliberately supplied out of canonical order.
    """
    return AuthoringPayload(
        frontmatter=_paper_frontmatter(
            id="paper.lightrag-2025",
            title="LightRAG: Simple and Fast Retrieval-Augmented Generation",
            summary="Graph-indexed RAG with dual-level retrieval.",
            status="synthesized",
            aliases=["LightRAG paper"],
            tasks=["long-doc-qa"],
            proposes=["[[lightrag]]", "[[dual-level-retrieval]]"],
            evaluates=["[[graphrag]]"],
            uses_datasets=["[[ultradomain]]"],
            builds_on=["[[graphrag-2024]]"],
        ),
        sections=(
            AuthoringSection(heading="Approach", body="Graph-based text indexing into a knowledge graph."),
            AuthoringSection(heading="TL;DR", body="LightRAG indexes a knowledge base as a graph and retrieves at two levels."),
        ),
        claims=(
            AuthoringClaim(
                text="LightRAG achieves higher win rates than GraphRAG on the three large subsets.",
                loc="§4.2, Table 1",
                quote="LightRAG consistently outperforms GraphRAG, particularly in larger datasets",
                kind="benchmark-result",
                edges=(("outperforms", "[[graphrag]]"),),
                links={"on_dataset": ("[[ultradomain]]", "[[mix]]"), "metric": ("[[llm-judged-win-rate]]",)},
                fields={"condition": "hybrid mode; GPT-4o-mini judge", "magnitude": "Overall 54.8/52.0/52.8% vs 45.2/48.0/47.2%"},
            ),
            AuthoringClaim(
                text="On the Mix subset LightRAG and GraphRAG are effectively tied.",
                loc="§4.2, Table 1 (Mix column)",
                quote='"50.4%" vs "49.6%"; "50.8%" vs "49.2%"',
                kind="negative-result",
                edges=(("comparable_to", "[[graphrag]]"),),
                links={"on_dataset": ("[[ultradomain]]",), "metric": ("[[llm-judged-win-rate]]",)},
                fields={"derived": "table"},
            ),
            AuthoringClaim(
                text="The evaluation is entirely GPT-4o-mini-judged pairwise win rates.",
                loc="§4.1",
                quote="We employ a robust LLM, specifically GPT-4o-mini, to rank each baseline",
                kind="observation",
                subject="[[lightrag]]",
            ),
        ),
        knowledge_base_schema=research_spec(),
    )


class TestRoundTrip:
    """`parse(render(payload))` reproduces the note's frontmatter, sections, and claims."""

    def test_frontmatter_round_trips(self):
        payload = _realistic_paper_payload()

        note = parse(render(payload, spec=research_spec()), source_path="Memory/Papers/lightrag-2025.md", spec=research_spec())

        assert isinstance(note.frontmatter, BaseFrontmatter)
        assert note.frontmatter.type == "paper"
        assert note.frontmatter.id == "paper.lightrag-2025"
        assert note.frontmatter.aliases == ("LightRAG paper",)
        assert note.frontmatter.model_extra is not None
        assert note.frontmatter.model_extra["year"] == 2025
        assert note.frontmatter.model_extra["proposes"] == (UnitLink(target="lightrag"), UnitLink(target="dual-level-retrieval"))
        assert note.frontmatter.model_extra["uses_datasets"] == (UnitLink(target="ultradomain"),)

    def test_sections_round_trip(self):
        payload = _realistic_paper_payload()

        note = parse(render(payload, spec=research_spec()), source_path="p.md", spec=research_spec())

        headings = {s.heading for s in note.sections}
        assert {"TL;DR", "Approach"} <= headings
        approach = next(s for s in note.sections if s.heading == "Approach")
        assert "knowledge graph" in approach.body

    def test_every_claim_field_round_trips(self):
        payload = _realistic_paper_payload()

        note = parse(render(payload, spec=research_spec()), source_path="p.md", spec=research_spec())

        assert [c.anchor for c in note.claims] == ["^c1", "^c2", "^c3"]
        c1 = note.claims[0]
        assert c1.text == "LightRAG achieves higher win rates than GraphRAG on the three large subsets."
        assert c1.loc == "§4.2, Table 1"
        assert c1.quote == "LightRAG consistently outperforms GraphRAG, particularly in larger datasets"
        assert c1.kind == "benchmark-result"
        assert c1.edges == (("outperforms", UnitLink(target="graphrag")),)
        assert c1.links["on_dataset"] == (UnitLink(target="ultradomain"), UnitLink(target="mix"))
        assert c1.links["metric"] == (UnitLink(target="llm-judged-win-rate"),)
        assert c1.fields["condition"] == "hybrid mode; GPT-4o-mini judge"
        assert c1.fields["magnitude"] == "Overall 54.8/52.0/52.8% vs 45.2/48.0/47.2%"

    def test_interior_quote_claim_not_rewrapped(self):
        # The table-derived quote has interior quotes -> the parser leaves it verbatim,
        # so the renderer must NOT add an outer pair (it would not round-trip).
        payload = _realistic_paper_payload()

        c2 = parse(render(payload, spec=research_spec()), source_path="p.md", spec=research_spec()).claims[1]

        assert c2.quote == '"50.4%" vs "49.6%"; "50.8%" vs "49.2%"'
        assert c2.fields["derived"] == "table"

    def test_subject_only_claim_round_trips(self):
        payload = _realistic_paper_payload()

        c3 = parse(render(payload, spec=research_spec()), source_path="p.md", spec=research_spec()).claims[2]

        assert c3.subject == UnitLink(target="lightrag")
        assert c3.edges == ()
        assert c3.kind == "observation"

    def test_quote_with_no_interior_quote_round_trips(self):
        # The plain quote on c1 must survive the strip-on-parse / wrap-on-render pair.
        payload = _realistic_paper_payload()

        c1 = parse(render(payload, spec=research_spec()), source_path="p.md", spec=research_spec()).claims[0]

        assert c1.quote == "LightRAG consistently outperforms GraphRAG, particularly in larger datasets"


class TestIdempotentRerender:
    """`render` has one stable output: rendering a parsed render is byte-identical."""

    def test_rerender_is_byte_identical(self):
        payload = _realistic_paper_payload()
        first = render(payload, spec=research_spec())

        note = parse(first, source_path="p.md", spec=research_spec())
        second = render(note, spec=research_spec())

        assert first == second

    def test_minimal_paper_rerender_identical(self):
        payload = AuthoringPayload(
            frontmatter=_paper_frontmatter(),
            sections=(AuthoringSection(heading="TL;DR", body="short"),),
            claims=(AuthoringClaim(text="A claim.", loc="§1", kind="observation"),),
            knowledge_base_schema=research_spec(),
        )
        first = render(payload, spec=research_spec())

        second = render(parse(first, source_path="p.md", spec=research_spec()), spec=research_spec())

        assert first == second


class TestStableFieldOrder:
    """Claim child fields render in a fixed order; anchors as `- {text} ^cN`."""

    def test_anchor_appended_to_bullet_text(self):
        payload = AuthoringPayload(
            frontmatter=_paper_frontmatter(), sections=(), claims=(AuthoringClaim(text="LightRAG wins.", loc="§1", kind="observation"),),
            knowledge_base_schema=research_spec(),
        )

        rendered = render(payload, spec=research_spec())

        assert "- LightRAG wins. ^c1" in rendered

    def test_loc_precedes_quote_precedes_kind(self):
        payload = AuthoringPayload(
            frontmatter=_paper_frontmatter(),
            sections=(),
            claims=(AuthoringClaim(text="A.", loc="§1", quote="snippet", kind="benchmark-result", links={"metric": ("[[m]]",)}),),
            knowledge_base_schema=research_spec(),
        )

        rendered = render(payload, spec=research_spec())
        loc_at = rendered.index("loc::")
        quote_at = rendered.index("quote::")
        page_type_at = rendered.index("kind::")
        metric_at = rendered.index("metric::")

        assert loc_at < quote_at < page_type_at < metric_at

    def test_quote_rewrapped_in_double_quotes(self):
        payload = AuthoringPayload(
            frontmatter=_paper_frontmatter(),
            sections=(),
            claims=(AuthoringClaim(text="A.", loc="§1", quote="a verbatim snippet", kind="observation"),),
            knowledge_base_schema=research_spec(),
        )

        rendered = render(payload, spec=research_spec())

        assert 'quote:: "a verbatim snippet"' in rendered

    def test_fields_are_two_space_indented_direct_children(self):
        payload = AuthoringPayload(
            frontmatter=_paper_frontmatter(), sections=(), claims=(AuthoringClaim(text="A.", loc="§1", kind="observation"),),
            knowledge_base_schema=research_spec(),
        )

        rendered = render(payload, spec=research_spec())

        assert "\n    - loc:: §1" in rendered

    def test_lifecycle_fields_render_last_and_round_trip(self):
        # status/disputed_by render after the evidence fields (schema rule 8) and survive reparse.
        payload = AuthoringPayload(
            frontmatter=_paper_frontmatter(),
            sections=(),
            claims=(AuthoringClaim(text="A.", loc="§1", kind="observation", status="disputed", disputed_by=("[[paper.x#^c2]]",)),),
            knowledge_base_schema=research_spec(),
        )

        rendered = render(payload, spec=research_spec())
        derived_absent_so_use_page_type = rendered.index("kind::")

        assert derived_absent_so_use_page_type < rendered.index("status::") < rendered.index("disputed_by::")
        reparsed = parse(rendered, source_path="p.md", spec=research_spec()).claims[0]
        assert reparsed.status == "disputed"
        assert reparsed.disputed_by == (UnitLink(target="paper.x", block="c2"),)


class TestCanonicalOrdering:
    """Sections emit in the page type's canonical H2 order; unknown H2s follow."""

    def test_canonical_sections_reordered(self):
        # Supplied Findings-before-TL;DR-before-Approach; canonical order is TL;DR, Approach, Findings.
        payload = AuthoringPayload(
            frontmatter=_paper_frontmatter(),
            sections=(AuthoringSection(heading="Approach", body="how"), AuthoringSection(heading="TL;DR", body="short")),
            claims=(AuthoringClaim(text="A.", loc="§1", kind="observation"),),
            knowledge_base_schema=research_spec(),
        )

        rendered = render(payload, spec=research_spec())

        tldr_at = rendered.index("## TL;DR")
        approach_at = rendered.index("## Approach")
        findings_at = rendered.index("## Findings")
        assert tldr_at < approach_at < findings_at

    def test_unknown_h2_emitted_after_canonical_in_original_order(self):
        payload = AuthoringPayload(
            frontmatter=_paper_frontmatter(),
            sections=(
                AuthoringSection(heading="Surprising tangent", body="free-form"),
                AuthoringSection(heading="Another aside", body="more"),
                AuthoringSection(heading="TL;DR", body="short"),
            ),
            claims=(),
            knowledge_base_schema=research_spec(),
        )

        rendered = render(payload, spec=research_spec())

        tldr_at = rendered.index("## TL;DR")
        tangent_at = rendered.index("## Surprising tangent")
        aside_at = rendered.index("## Another aside")
        assert tldr_at < tangent_at < aside_at  # canonical first, then unknowns in original order

    def test_findings_emitted_when_claims_present_without_explicit_section(self):
        # Claims imply a Findings section even if the author didn't list one.
        payload = AuthoringPayload(
            frontmatter=_paper_frontmatter(),
            sections=(AuthoringSection(heading="TL;DR", body="short"),),
            claims=(AuthoringClaim(text="A.", loc="§1", kind="observation"),),
            knowledge_base_schema=research_spec(),
        )

        note = parse(render(payload, spec=research_spec()), source_path="p.md", spec=research_spec())

        assert any(s.canon == "findings" for s in note.sections)


class TestGoldenRoundTrip:
    """Rendering the parsed real `lightrag-2025.md` reproduces a semantically-equal note.

    This is the strongest net: the renderer must invert the parser on the live
    knowledge base's most complex paper (10 claims, multi-link `on_dataset`, a comparative
    edge, a table-derived interior-quote claim, lifecycle-flagged `^c8`), and the
    re-render must be byte-stable.
    """

    def _note(self) -> KnowledgeBaseNote:
        return parse(_GOLDEN.read_text(encoding="utf-8"), source_path="Memory/Papers/lightrag-2025.md", spec=research_spec())

    def test_claims_survive_render_reparse(self):
        original = self._note()

        reparsed = parse(render(original, spec=research_spec()), source_path="Memory/Papers/lightrag-2025.md", spec=research_spec())

        assert reparsed.claims == original.claims

    def test_frontmatter_survives_render_reparse(self):
        original = self._note()

        reparsed = parse(render(original, spec=research_spec()), source_path="Memory/Papers/lightrag-2025.md", spec=research_spec())

        assert reparsed.frontmatter == original.frontmatter

    def test_sections_survive_render_reparse(self):
        original = self._note()

        reparsed = parse(render(original, spec=research_spec()), source_path="Memory/Papers/lightrag-2025.md", spec=research_spec())

        # Heading set + canon map are preserved (body whitespace may renormalise).
        assert {s.heading for s in reparsed.sections} == {s.heading for s in original.sections}
        assert {s.heading: s.canon for s in reparsed.sections} == {s.heading: s.canon for s in original.sections}

    def test_render_is_idempotent_on_golden(self):
        first = render(self._note(), spec=research_spec())

        second = render(parse(first, source_path="Memory/Papers/lightrag-2025.md", spec=research_spec()), spec=research_spec())

        assert first == second


class TestClaimFieldOrderCoversModel:
    """The renderer accounts for every `ClaimBlock` field, so none silently drops on render.

    A claim renders in two places: the bullet header line `- {text} ^cN` (the
    `_CLAIM_HEADER_FIELDS` -- `text` and `anchor`) and the indented `key:: value`
    children driven by `_CLAIM_FIELD_ORDER` (with `edges` expanding inline). Together
    those two sets must cover `ClaimBlock.model_fields` exactly. Deriving the model
    fields dynamically (not a hardcoded list) means adding a `ClaimBlock` field that is
    neither ordered nor header-handled trips this guard rather than silently losing data,
    forcing a deliberate choice: add it to `_CLAIM_FIELD_ORDER` to render it, or to
    `_CLAIM_HEADER_FIELDS` if it belongs on the bullet line.
    """

    def test_order_and_header_fields_cover_model_exactly(self):
        model_fields = set(ClaimBlock.model_fields)
        order_fields = set(_CLAIM_FIELD_ORDER)
        accounted_for = order_fields | _CLAIM_HEADER_FIELDS

        unrendered = model_fields - accounted_for
        stale = accounted_for - model_fields
        assert not unrendered, (
            f"ClaimBlock field(s) {sorted(unrendered)} are in neither _CLAIM_FIELD_ORDER nor "
            f"_CLAIM_HEADER_FIELDS, so they never render and silently drop on round-trip (F24). "
            f"Add each to _CLAIM_FIELD_ORDER (to emit it as a `key:: value` child) or to "
            f"_CLAIM_HEADER_FIELDS (if it belongs on the bullet header line)."
        )
        assert not stale, (
            f"{sorted(stale)} appear in _CLAIM_FIELD_ORDER/_CLAIM_HEADER_FIELDS but are not "
            f"ClaimBlock fields -- a removed/renamed field left a stale render entry."
        )

    def test_order_tuple_has_no_duplicate_entries(self):
        # A duplicated key would emit the same field twice; the cover check above passes on
        # the deduped set, so guard the tuple's length separately.
        assert len(_CLAIM_FIELD_ORDER) == len(set(_CLAIM_FIELD_ORDER))

    def test_header_and_order_fields_are_disjoint(self):
        # A field handled on the bullet line must not also be an indented child (it would
        # render twice), so the two accounting sets must not overlap.
        assert not (set(_CLAIM_FIELD_ORDER) & _CLAIM_HEADER_FIELDS)


class TestPreambleSection:
    """A parsed preamble section renders as its body text ahead of every heading, with no heading line."""

    def test_preamble_precedes_first_heading_with_no_marker_and_rerender_is_stable(self):
        source = _paper_text("# Sample\n\nOpening thesis.\n\n## Findings\n\nEvidence.")
        note = parse(source, source_path="p.md", spec=research_spec())

        rendered = render(note, spec=research_spec())

        assert "Introduction" not in rendered
        assert rendered.index("Opening thesis.") < rendered.index("## ")

        rerendered = render(parse(rendered, source_path="p.md", spec=research_spec()), spec=research_spec())
        assert rendered == rerendered


class TestObsidianDialect:
    """Unit links re-emit in Obsidian dialect (alias / block / heading forms)."""

    def test_aliased_link_round_trips(self):
        payload = AuthoringPayload(
            frontmatter=_paper_frontmatter(),
            sections=(),
            claims=(AuthoringClaim(text="A.", loc="§1", kind="observation", subject=UnitLink(target="lightrag", alias="LightRAG")),),
            knowledge_base_schema=research_spec(),
        )

        rendered = render(payload, spec=research_spec())
        assert "[[lightrag|LightRAG]]" in rendered

        subject = parse(rendered, source_path="p.md", spec=research_spec()).claims[0].subject
        assert subject == UnitLink(target="lightrag", alias="LightRAG")

    def test_block_ref_link_round_trips(self):
        payload = AuthoringPayload(
            frontmatter=_paper_frontmatter(),
            sections=(),
            claims=(
                AuthoringClaim(
                    text="A.", loc="§1", kind="observation", edges=(("replicates", UnitLink(target="hirag-2025", block="c13")),)
                ),
            ),
            knowledge_base_schema=research_spec(),
        )

        rendered = render(payload, spec=research_spec())
        assert "[[hirag-2025#^c13]]" in rendered

        edges = parse(rendered, source_path="p.md", spec=research_spec()).claims[0].edges
        assert edges == (("replicates", UnitLink(target="hirag-2025", block="c13")),)


_RENDERED_GOLDEN_DIR = pathlib.Path(__file__).parent / "fixtures" / "rendered_golden"
_RESEARCH_NOTES_DIR = pathlib.Path(__file__).parent / "fixtures" / "research_notes"


def _declared_spec() -> KnowledgeBaseSchema:
    """A one-page-type knowledge base declaring a link field, a text field and an edge relation of its own."""
    return KnowledgeBaseSchema(
        slug="custom",
        page_types=(
            PageTypeSpec(page_type="note", id_prefix="note", canonical_sections=(CanonicalSection(slug="findings"),), claims_enabled=True),
        ),
        claims=ClaimGrammarSpec(
            section="findings", edge_relations=("governs",), multilink_fields=("applies_to",), text_scalar_fields=("jurisdiction", "court")
        ),
    )


class TestDeclaredClaimFieldsRender:
    """A claim's declared fields render in the claim grammar's order, after the built-in fields and edges."""

    def test_declared_fields_render_in_grammar_order_and_round_trip(self) -> None:
        written = (
            "---\nid: note.x\ntype: note\ntitle: X\nstatus: draft\n---\n\n## Findings\n\n"
            "- A claim. ^c1\n"
            "    - court:: high court\n"
            "    - status:: disputed\n"
            "    - jurisdiction:: EU\n"
            "    - governs:: [[c]]\n"
            "    - applies_to:: [[a]], [[b]]\n"
            "    - loc:: §1\n"
        )
        spec = _declared_spec()

        rendered = render(parse(written, source_path="p.md", spec=spec), spec=spec)

        assert rendered.endswith(
            "- A claim. ^c1\n"
            "    - loc:: §1\n"
            "    - governs:: [[c]]\n"
            "    - applies_to:: [[a]], [[b]]\n"
            "    - jurisdiction:: EU\n"
            "    - court:: high court\n"
            "    - status:: disputed\n"
        )
        assert render(parse(rendered, source_path="p.md", spec=spec), spec=spec) == rendered


class TestResearchFixtureRendersAsBefore:
    """A research note parses and renders byte for byte as it did before claim fields came from the grammar."""

    def test_ur2_paper_renders_byte_for_byte(self) -> None:
        source = (_RESEARCH_NOTES_DIR / "paper-ur2-2025.md").read_text(encoding="utf-8")
        expected = (_RENDERED_GOLDEN_DIR / "paper-ur2-2025.md").read_text(encoding="utf-8")

        rendered = render(parse(source, source_path="paper-ur2-2025.md", spec=research_spec()), spec=research_spec())

        assert rendered == expected
