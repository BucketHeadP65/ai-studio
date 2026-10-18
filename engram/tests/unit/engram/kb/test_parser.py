"""Unit tests for the markdown -> :class:`KnowledgeBaseNote` parser (``src/engram/memory/parser.py``).

These pin the binding *parser invariants* and the claims grammar:

* frontmatter is discriminated through `parse_frontmatter` to the typed subclass;
* H2/H3 headings become :class:`Section` models with ltree-safe ``path`` slugs,
  `canon` set only when the heading matches the page type's canonical-H2 set,
  `ord` preserved, and the section body captured;
* claim blocks are the top-level Findings bullets ending in `^cN`; their fields
  are the bullet's *direct* `key:: value` children only (deeper nesting is
  inert); multi-link values comma-split at the top level; a multi-link edge
  relation appends one edge per link token, in order. Declared text and link
  fields land in the claim's `fields` and `links` by name, and an undeclared
  `key::` line raises :class:`ClaimParseError` naming the key.
* `%%` comments, fenced code, and `<!-- -->` HTML comments are inert;
* a claim missing the grammar-required `loc::`, or a link-scalar field given
  more than one link token, raises a domain-meaningful :class:`ClaimParseError`
  naming the anchor or field respectively (the domain ``ClaimBlock.loc`` is
  non-optional and a link-scalar field is single-valued, so neither can
  represent the malformed input);
* `parse` of the real, verbatim-copied `lightrag-2025.md` golden fixture
  yields its ten claims with exact anchors/fields (regression net vs the live
  knowledge base).

No monkeypatching, no mocks: the parser is a pure function of its text input.
"""

from __future__ import annotations

import pytest

from core.markdown import heading_slug

from engram.kb.contracts import BaseFrontmatter, CanonicalSection, ClaimGrammarSpec, KnowledgeBaseSchema, PageTypeSpec, UnitLink, parse
from engram.kb.exceptions import ClaimParseError
from engram.testing.knowledge_base_builder import GOLDEN_LIGHTRAG_PAPER
from engram.testing.specs import research_spec

_GOLDEN = GOLDEN_LIGHTRAG_PAPER

_RESEARCH_SPEC = research_spec()

_PAPER_FM = """---
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


def _paper(body: str) -> str:
    """A minimal valid paper note: the fixed frontmatter block + the given body."""
    return _PAPER_FM + "\n" + body


class TestSectionPathsAreCollisionProof:
    """Section ltree paths must be non-empty and unique within a note, else the load collides on `(page, path)`."""

    def test_non_latin_headings_get_distinct_nonempty_paths(self):
        # Regression: two all-Greek headings each slugged to "" and collided at load.
        note = parse(_paper("## Τιμές\n\nprices\n\n## Παραλίες\n\nbeaches\n"), source_path="p.md", spec=_RESEARCH_SPEC)
        paths = [s.path for s in note.sections]
        assert all(paths)  # none empty
        assert len(set(paths)) == len(paths)  # all distinct
        assert {"times", "paralies"} <= set(paths)

    def test_duplicate_headings_are_disambiguated(self):
        note = parse(_paper("## Notes\n\nfirst\n\n## Notes\n\nsecond\n"), source_path="p.md", spec=_RESEARCH_SPEC)
        by_path = {s.path: s.body for s in note.sections}
        assert sorted(by_path) == ["notes", "notes_2"]
        assert by_path["notes"] == "first"
        assert by_path["notes_2"] == "second"


class TestFrontmatterDiscrimination:
    """The parser routes frontmatter through `parse_frontmatter` (single generic path)."""

    def test_paper_frontmatter_typed(self) -> None:
        note = parse(_paper("## TL;DR\n\nbody\n"), source_path="Memory/Papers/sample-2025.md", spec=_RESEARCH_SPEC)

        assert isinstance(note.frontmatter, BaseFrontmatter)
        assert note.frontmatter.id == "paper.sample-2025"
        assert note.frontmatter.type == "paper"
        assert note.frontmatter.model_extra is not None
        assert note.frontmatter.model_extra["year"] == 2025
        assert note.source_path == "Memory/Papers/sample-2025.md"

    def test_entity_frontmatter_typed(self) -> None:
        text = (
            "---\n"
            "id: ent.dual-level-retrieval\n"
            "type: entity\n"
            "title: Dual-level retrieval\n"
            "status: stub\n"
            "entity_type: technique\n"
            "---\n\n## TL;DR\n\nTwo altitudes.\n"
        )

        note = parse(text, source_path="Memory/Entities/dual-level-retrieval.md", spec=_RESEARCH_SPEC)

        assert isinstance(note.frontmatter, BaseFrontmatter)
        assert note.frontmatter.type == "entity"
        assert note.frontmatter.model_extra is not None
        assert note.frontmatter.model_extra["entity_type"] == "technique"


class TestSections:
    """H2/H3 -> :class:`Section` with ltree path, canon detection, ord, body."""

    def test_h2_path_is_slug(self):
        note = parse(_paper("## Approach\n\nGraph indexing.\n"), source_path="p.md", spec=_RESEARCH_SPEC)

        approach = next(s for s in note.sections if s.heading == "Approach")
        assert approach.level == 2
        assert approach.path == "approach"
        assert "Graph indexing." in approach.body

    def test_canonical_h2_sets_canon(self):
        note = parse(_paper("## Findings\n\n- A claim. ^c1\n    - loc:: §1\n"), source_path="p.md", spec=_RESEARCH_SPEC)

        findings = next(s for s in note.sections if s.heading == "Findings")
        assert findings.canon == "findings"

    def test_tldr_canon_slug_is_ltree_safe(self):
        # 'TL;DR' is canonical for papers; its slug must be ltree-safe (no ';').
        note = parse(_paper("## TL;DR\n\nshort\n"), source_path="p.md", spec=_RESEARCH_SPEC)

        tldr = next(s for s in note.sections if s.heading == "TL;DR")
        assert tldr.path == "tl_dr"
        assert tldr.canon == "tl_dr"

    def test_non_canonical_h2_has_no_canon(self):
        # 'Practical notes' is an *optional* paper H2, not in the canonical (required) set.
        note = parse(_paper("## Surprising tangent\n\nfree-form.\n"), source_path="p.md", spec=_RESEARCH_SPEC)

        section = next(s for s in note.sections if s.heading == "Surprising tangent")
        assert section.canon is None
        assert section.path == "surprising_tangent"

    def test_h3_nests_under_h2_with_dotted_path(self):
        body = "## Approach\n\nlead-in\n\n### Graph indexing\n\nchunks to graph\n"

        note = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC)

        h3 = next(s for s in note.sections if s.heading == "Graph indexing")
        assert h3.level == 3
        assert h3.path == "approach.graph_indexing"
        assert h3.canon is None
        assert "chunks to graph" in h3.body

    def test_ord_preserved_in_document_order(self):
        body = "## TL;DR\n\na\n\n## Approach\n\nb\n\n## Findings\n\n- c. ^c1\n    - loc:: §1\n"

        note = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC)

        headings_in_order = [s.heading for s in sorted(note.sections, key=lambda s: s.ord)]
        assert headings_in_order[:3] == ["TL;DR", "Approach", "Findings"]
        assert [s.ord for s in note.sections] == sorted(s.ord for s in note.sections)

    def test_comparison_canon_sections(self):
        # A comparison's canonical H2s differ from a paper's.
        text = "---\nid: cmp.x\ntype: comparison\ntitle: X vs Y\nstatus: draft\n---\n\n## Scope\n\nwhat\n\n## Verdict\n\nit depends\n"

        note = parse(text, source_path="Memory/Comparisons/x.md", spec=_RESEARCH_SPEC)

        assert isinstance(note.frontmatter, BaseFrontmatter)
        assert note.frontmatter.type == "comparison"
        assert {s.heading: s.canon for s in note.sections} == {"Scope": "scope", "Verdict": "verdict"}


class TestPreambles:
    """Meaningful prose before the first indexed heading is its own reserved section."""

    def test_intro_uses_an_unreachable_synthetic_namespace(self):
        note = parse(
            _paper("# Sample\n\nOpening thesis.\n\n## Preamble\n\nAuthored body.\n\n## Findings\n\nEvidence."),
            source_path="p.md",
            spec=_RESEARCH_SPEC,
        )

        assert [(section.kind, section.path, section.ord, section.body) for section in note.sections] == [
            ("preamble", "__preamble__", 0, "Opening thesis."),
            ("heading", "preamble", 1, "Authored body."),
            ("heading", "findings", 2, "Evidence."),
        ]

    def test_intro_without_h1_is_preserved_before_first_h2(self):
        note = parse(_paper("Opening thesis.\n\n## Findings\n\nEvidence."), source_path="p.md", spec=_RESEARCH_SPEC)

        assert [(section.kind, section.level, section.body) for section in note.sections] == [
            ("preamble", 0, "Opening thesis."),
            ("heading", 2, "Evidence."),
        ]

    def test_intro_with_only_an_h1_is_preserved(self):
        note = parse(_paper("# Sample\n\nOpening thesis."), source_path="p.md", spec=_RESEARCH_SPEC)

        assert [(section.kind, section.path, section.body) for section in note.sections] == [("preamble", "__preamble__", "Opening thesis.")]

    def test_h1_without_intro_emits_no_preamble(self):
        note = parse(_paper("# Sample"), source_path="p.md", spec=_RESEARCH_SPEC)

        assert note.sections == ()

    def test_intro_before_an_h3_is_preserved(self):
        note = parse(_paper("# Sample\n\nOpening thesis.\n\n### Detail\n\nEvidence."), source_path="p.md", spec=_RESEARCH_SPEC)

        assert [(section.kind, section.level, section.path, section.ord) for section in note.sections] == [
            ("preamble", 0, "__preamble__", 0),
            ("heading", 3, "detail", 1),
        ]

    @pytest.mark.parametrize("intro", ["", "\n\n", "%% private %%\n\n<!-- hidden -->\n\n"])
    def test_blank_or_comment_only_intro_emits_no_preamble(self, intro: str):
        note = parse(_paper(f"# Sample\n\n{intro}## Findings\n\nEvidence."), source_path="p.md", spec=_RESEARCH_SPEC)

        assert [(section.path, section.ord) for section in note.sections] == [("findings", 0)]

    def test_intro_keeps_fenced_code_and_links_but_not_comments(self):
        note = parse(
            _paper(
                "# Sample\n\n"
                "Opening [guide](https://example.test).\n\n"
                "<!-- hidden -->\n\n"
                "```python\n"
                "answer = 42\n"
                "# %% literal %%\n"
                "# <!-- literal -->\n"
                "```\n\n"
                "## Findings\n\nEvidence."
            ),
            source_path="p.md",
            spec=_RESEARCH_SPEC,
        )

        preamble = note.sections[0].body
        assert "hidden" not in preamble
        assert "[guide](https://example.test)" in preamble
        assert "```python\nanswer = 42\n# %% literal %%\n# <!-- literal -->\n```" in preamble

    def test_adding_intro_does_not_change_authored_section_id(self):
        plain = parse(_paper("## Preamble\n\nAuthored body."), source_path="p.md", spec=_RESEARCH_SPEC)
        with_intro = parse(_paper("Opening thesis.\n\n## Preamble\n\nAuthored body."), source_path="p.md", spec=_RESEARCH_SPEC)

        assert [f"{plain.frontmatter.id}#{section.path}" for section in plain.sections] == ["paper.sample-2025#preamble"]
        assert [f"{with_intro.frontmatter.id}#{section.path}" for section in with_intro.sections] == [
            "paper.sample-2025#__preamble__",
            "paper.sample-2025#preamble",
        ]

    def test_removing_intro_restores_authored_ordinals(self):
        with_intro = parse(_paper("Opening thesis.\n\n## Findings\n\nEvidence."), source_path="p.md", spec=_RESEARCH_SPEC)
        without_intro = parse(_paper("## Findings\n\nEvidence."), source_path="p.md", spec=_RESEARCH_SPEC)

        assert [(section.path, section.ord) for section in with_intro.sections] == [("__preamble__", 0), ("findings", 1)]
        assert [(section.path, section.ord) for section in without_intro.sections] == [("findings", 0)]

    def test_heading_slug_cannot_produce_the_synthetic_namespace(self):
        assert heading_slug("__preamble__") == "preamble"
        assert heading_slug("Introduction") != "__preamble__"


class TestClaimGrammar:
    """Claim blocks: anchors, direct-children-only fields, multi-link split, edges."""

    def test_anchor_and_text_captured(self):
        note = parse(_paper("## Findings\n\n- LightRAG wins. ^c1\n    - loc:: §4.2\n"), source_path="p.md", spec=_RESEARCH_SPEC)

        assert len(note.claims) == 1
        claim = note.claims[0]
        assert claim.anchor == "^c1"
        assert claim.text == "LightRAG wins."
        assert claim.loc == "§4.2"

    def test_only_findings_bullets_are_claims(self):
        # An anchored bullet under a non-Findings section is NOT a claim.
        body = "## Approach\n\n- some bullet ^c9\n    - loc:: §1\n\n## Findings\n\n- real. ^c1\n    - loc:: §2\n"

        note = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC)

        assert [c.anchor for c in note.claims] == ["^c1"]

    def test_quote_kind_subject_and_qualifiers(self):
        body = (
            "## Findings\n\n"
            "- LightRAG beats GraphRAG. ^c1\n"
            "    - loc:: §4.2, Table 1\n"
            '    - quote:: "LightRAG consistently outperforms GraphRAG"\n'
            "    - kind:: benchmark-result\n"
            "    - subject:: [[lightrag]]\n"
            "    - metric:: [[llm-judged-win-rate]]\n"
            "    - condition:: hybrid mode; GPT-4o-mini judge\n"
        )

        claim = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC).claims[0]

        assert claim.quote == "LightRAG consistently outperforms GraphRAG"
        assert claim.kind == "benchmark-result"
        assert claim.subject == UnitLink(target="lightrag")
        assert claim.links["metric"] == (UnitLink(target="llm-judged-win-rate"),)
        assert claim.fields["condition"] == "hybrid mode; GPT-4o-mini judge"

    def test_relation_keys_become_edges(self):
        body = "## Findings\n\n- A. ^c1\n    - loc:: §1\n    - outperforms:: [[graphrag]]\n    - supports:: [[graph-enhanced-rag]]\n"

        claim = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC).claims[0]

        assert claim.edges == (("outperforms", UnitLink(target="graphrag")), ("supports", UnitLink(target="graph-enhanced-rag")))

    def test_multi_link_edge_relation_splits_into_two_edges(self):
        # A single edge-relation line carrying two link tokens appends one edge per token,
        # in written order, rather than raising or merging them into one target.
        body = "## Findings\n\n- A. ^c1\n    - loc:: §1\n    - supports:: [[f1-score]], [[exact-match]]\n"

        claim = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC).claims[0]

        assert claim.edges == (("supports", UnitLink(target="f1-score")), ("supports", UnitLink(target="exact-match")))

    def test_multi_link_edge_relation_keeps_order_for_three_targets(self):
        body = "## Findings\n\n- A. ^c1\n    - loc:: §1\n    - supports:: [[a]], [[b]], [[c]]\n"

        claim = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC).claims[0]

        assert claim.edges == (("supports", UnitLink(target="a")), ("supports", UnitLink(target="b")), ("supports", UnitLink(target="c")))

    def test_multi_link_on_dataset_comma_split(self):
        body = "## Findings\n\n- A. ^c1\n    - loc:: §1\n    - on_dataset:: [[ultradomain]], [[mix]]\n"

        claim = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC).claims[0]

        assert claim.links["on_dataset"] == (UnitLink(target="ultradomain"), UnitLink(target="mix"))

    def test_single_link_on_dataset_is_one_tuple(self):
        body = "## Findings\n\n- A. ^c1\n    - loc:: §1\n    - on_dataset:: [[ultradomain]]\n"

        claim = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC).claims[0]

        assert claim.links["on_dataset"] == (UnitLink(target="ultradomain"),)

    def test_quote_outer_double_quotes_stripped(self):
        # The grammar writes quote:: "snippet"; the stored quote is the bare snippet
        # (what the verify stage matches against the raw mirror, whitespace-normalized).
        body = '## Findings\n\n- A. ^c1\n    - loc:: §1\n    - quote:: "a verbatim snippet"\n'

        claim = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC).claims[0]

        assert claim.quote == "a verbatim snippet"

    def test_quote_with_interior_quotes_left_verbatim(self):
        # A table-derived quote of cell values is not one wrapped string — keep it as written.
        body = '## Findings\n\n- A. ^c2\n    - loc:: §1, Table 1\n    - quote:: "50.4%" vs "49.6%"\n    - derived:: table\n'

        claim = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC).claims[0]

        assert claim.quote == '"50.4%" vs "49.6%"'

    def test_derived_table_flag(self):
        body = "## Findings\n\n- tie. ^c2\n    - loc:: §4.2, Table 1\n    - derived:: table\n    - kind:: negative-result\n"

        claim = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC).claims[0]

        assert claim.fields["derived"] == "table"

    def test_deeply_nested_field_is_inert(self):
        # A 'key::' under a *prose* sub-bullet (two bullet-levels deep) is NOT a direct
        # child and must be ignored (schema.md rule 2: fields are direct sub-bullets).
        body = "## Findings\n\n- A. ^c1\n    - loc:: §1\n    - free prose elaboration\n        - metric:: [[should-be-inert]]\n"

        claim = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC).claims[0]

        assert "metric" not in claim.links

    def test_free_prose_subbullet_ignored(self):
        body = "## Findings\n\n- A. ^c1\n    - loc:: §1\n    - just commentary, not a field\n"

        claim = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC).claims[0]

        assert claim.loc == "§1"

    def test_lifecycle_fields_captured(self):
        # 'status::' / 'disputed_by::' are lifecycle fields validation *adds* (schema rule 8);
        # the ClaimBlock now models them, so the parser must capture them as direct fields.
        body = "## Findings\n\n- A. ^c1\n    - loc:: §1\n    - status:: disputed\n    - disputed_by:: [[hirag-2025#^c13]]\n"

        claim = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC).claims[0]

        assert claim.anchor == "^c1"
        assert claim.loc == "§1"
        assert claim.status == "disputed"
        assert claim.disputed_by == (UnitLink(target="hirag-2025", block="c13"),)

    def test_truly_unknown_field_key_refused(self):
        # A key that is no built-in claim field, edge relation or declared field is refused, never dropped.
        body = "## Findings\n\n- A. ^c1\n    - loc:: §1\n    - mood:: cheerful\n"

        with pytest.raises(ClaimParseError) as exc:
            parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC)

        assert "'mood'" in str(exc.value)
        assert "^c1" in str(exc.value)


class TestInertSpans:
    """`%%` comments, fenced code, and `<!-- -->` are inert (no claims leak out)."""

    def test_code_fence_claim_inert(self):
        body = "## Findings\n\n```\n- fenced fake. ^c99\n    - loc:: §none\n```\n\n- real. ^c1\n    - loc:: §1\n"

        note = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC)

        assert [c.anchor for c in note.claims] == ["^c1"]

    def test_html_comment_claim_inert(self):
        body = "## Findings\n\n<!--\n- commented fake. ^c98\n    - loc:: §none\n-->\n\n- real. ^c1\n    - loc:: §1\n"

        note = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC)

        assert [c.anchor for c in note.claims] == ["^c1"]

    def test_obsidian_comment_line_inert(self):
        # A '%%'-prefixed line is a paragraph, never a list_item, so never a claim.
        body = "## Findings\n\n%% - hidden fake ^c97  loc:: §none %%\n\n- real. ^c1\n    - loc:: §1\n"

        note = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC)

        assert [c.anchor for c in note.claims] == ["^c1"]


class TestMissingLoc:
    """A claim without `loc::` cannot be represented -> domain-meaningful error."""

    def test_missing_loc_raises_naming_the_anchor(self):
        body = "## Findings\n\n- A claim with no locator. ^c7\n    - kind:: observation\n"

        with pytest.raises(ClaimParseError) as exc:
            parse(_paper(body), source_path="Memory/Papers/p.md", spec=_RESEARCH_SPEC)

        assert "c7" in str(exc.value)


class TestLinkScalarRefusesMultipleLinks:
    """A link-scalar field takes exactly one link; two or more raises, naming the field and count."""

    def test_two_links_on_a_scalar_field_raises_naming_field_and_count(self):
        # `on_task` stays single-link: a claim measures one task at a time, so two links must
        # refuse loudly instead of picking (or merging) one.
        body = "## Findings\n\n- A. ^c1\n    - loc:: §1\n    - on_task:: [[multi-hop-qa]], [[summarization]]\n"

        with pytest.raises(ClaimParseError) as exc:
            parse(_paper(body), source_path="Memory/Papers/p.md", spec=_RESEARCH_SPEC)

        assert "on_task" in str(exc.value)
        assert "2" in str(exc.value)

    def test_a_multilink_claim_field_splits_into_every_written_link(self):
        # `metric::` is multilink: a benchmark claim often measures two metrics at once, and
        # each written link becomes its own target, in authored order.
        body = "## Findings\n\n- A. ^c1\n    - loc:: §1\n    - metric:: [[f1-score]], [[exact-match]]\n"

        claim = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC).claims[0]

        assert claim.links["metric"] == (UnitLink(target="f1-score"), UnitLink(target="exact-match"))

    def test_one_link_on_a_scalar_field_still_parses(self):
        body = "## Findings\n\n- A. ^c1\n    - loc:: §1\n    - on_task:: [[multi-hop-qa]]\n"

        claim = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC).claims[0]

        assert claim.links["on_task"] == (UnitLink(target="multi-hop-qa"),)


class TestClaimFieldBoundaryIsValidated:
    """A claim field crossing into `ClaimBlock` is validated, not type-suppressed (F26).

    `parser._flush` builds the claim from the parsed field values through `ClaimBlock.model_validate`.
    A declared plain field holds its text as written, even text shaped like a link.
    """

    @staticmethod
    def _spec_with_plain_metric() -> KnowledgeBaseSchema:
        """A knowledge base spec classifying `metric` as a plain (string) claim field."""
        return KnowledgeBaseSchema(
            slug="custom",
            page_types=(
            PageTypeSpec(page_type="note", id_prefix="note", canonical_sections=(CanonicalSection(slug="findings"),), claims_enabled=True),
        ),
            claims=ClaimGrammarSpec(section="findings", plain_fields=("loc", "metric")),
        )

    def test_a_declared_plain_field_keeps_its_text(self) -> None:
        # `metric` is a plain field in this spec, so its value is text, never a link.
        text = (
            "---\nid: note.x\ntype: note\ntitle: X\nstatus: draft\n---\n\n"
            "## Findings\n\n- A claim. ^c1\n    - loc:: §1\n    - metric:: not-a-unit link\n"
        )

        claim = parse(text, source_path="p.md", spec=self._spec_with_plain_metric()).claims[0]

        assert claim.fields == {"metric": "not-a-unit link"}
        assert claim.links == {}

    def test_well_formed_claim_round_trips_unchanged(self) -> None:
        # The validated boundary must accept valid input unchanged: a [[..]] metric value (a real
        # UnitLink) parses into the expected claim.
        body = "## Findings\n\n- A claim. ^c1\n    - loc:: §1\n    - metric:: [[llm-judged-win-rate]]\n"

        claim = parse(_paper(body), source_path="p.md", spec=_RESEARCH_SPEC).claims[0]

        assert claim.anchor == "^c1"
        assert claim.loc == "§1"
        assert claim.links["metric"] == (UnitLink(target="llm-judged-win-rate"),)


class TestGoldenRegression:
    """`parse(real lightrag-2025.md)` yields its 10 claims with exact anchors/fields."""

    def test_fixture_exists(self):
        assert _GOLDEN.is_file()

    def _note(self):
        return parse(_GOLDEN.read_text(encoding="utf-8"), source_path="Memory/Papers/lightrag-2025.md", spec=_RESEARCH_SPEC)

    def test_frontmatter_is_paper(self) -> None:
        note = self._note()

        assert isinstance(note.frontmatter, BaseFrontmatter)
        assert note.frontmatter.type == "paper"
        assert note.frontmatter.id == "paper.lightrag-2025"
        assert note.frontmatter.model_extra is not None
        assert note.frontmatter.model_extra["proposes"] == (UnitLink(target="lightrag"), UnitLink(target="dual-level-retrieval"))

    def test_ten_claims_with_sequential_anchors(self):
        note = self._note()

        assert [c.anchor for c in note.claims] == [f"^c{n}" for n in range(1, 11)]

    def test_canonical_paper_sections_present(self):
        note = self._note()

        canons = {s.canon for s in note.sections if s.canon is not None}
        assert {"tl_dr", "approach", "findings"} <= canons

    def test_c1_fields(self):
        note = self._note()
        c1 = next(c for c in note.claims if c.anchor == "^c1")

        assert c1.kind == "benchmark-result"
        assert c1.edges == (("outperforms", UnitLink(target="graphrag")),)
        assert c1.links["on_dataset"] == (UnitLink(target="ultradomain"),)
        assert c1.links["metric"] == (UnitLink(target="llm-judged-win-rate"),)
        assert c1.quote == "LightRAG consistently outperforms GraphRAG, particularly in larger datasets with complex language contexts"
        assert c1.fields["magnitude"] == "Overall 54.8/52.0/52.8% vs 45.2/48.0/47.2%"

    def test_c2_is_derived_table_negative_result(self):
        note = self._note()
        c2 = next(c for c in note.claims if c.anchor == "^c2")

        assert c2.fields["derived"] == "table"
        assert c2.kind == "negative-result"
        assert c2.edges == (("comparable_to", UnitLink(target="graphrag")),)
        # Interior-quote cell values stay verbatim (not one wrapped string).
        assert c2.quote == '"50.4%" vs "49.6%"; "50.8%" vs "49.2%"'

    def test_c8_disputed_lifecycle_fields_captured_with_edges(self):
        # ^c8 carries 'status:: disputed' + 'disputed_by::' (lifecycle, schema rule 8)
        # alongside a real comparative edge and qualifiers — all now captured.
        note = self._note()
        c8 = next(c for c in note.claims if c.anchor == "^c8")

        assert c8.edges == (("outperforms", UnitLink(target="graphrag")),)
        assert c8.links["metric"] == (UnitLink(target="retrieval-cost"),)
        assert c8.links["on_dataset"] == (UnitLink(target="ultradomain"),)
        assert c8.status == "disputed"
        assert c8.disputed_by == (UnitLink(target="hirag-2025", block="c13"),)

    def test_c10_has_subject_and_no_edges(self):
        note = self._note()
        c10 = next(c for c in note.claims if c.anchor == "^c10")

        assert c10.subject == UnitLink(target="lightrag")
        assert c10.kind == "observation"
        assert c10.edges == ()


def _declared_spec(grammar: ClaimGrammarSpec) -> KnowledgeBaseSchema:
    """A one-page-type knowledge base whose claims follow `grammar`."""
    return KnowledgeBaseSchema(
        slug="custom",
        page_types=(
            PageTypeSpec(page_type="note", id_prefix="note", canonical_sections=(CanonicalSection(slug="findings"),), claims_enabled=True),
        ),
        claims=grammar,
    )


def _note_text(claim_lines: str) -> str:
    """A `note` page with one claim under its findings section, the claim's field lines as given."""
    return "---\nid: note.x\ntype: note\ntitle: X\nstatus: draft\n---\n\n## Findings\n\n- A claim. ^c1\n" + claim_lines


_DECLARED_GRAMMAR = ClaimGrammarSpec(section="findings", text_scalar_fields=("jurisdiction",), multilink_fields=("applies_to",))


class TestDeclaredClaimFields:
    """A claim keeps every field its knowledge base's claim grammar declares, and refuses every other key."""

    def test_declared_text_and_link_fields_are_kept(self) -> None:
        text = _note_text("    - loc:: §1\n    - jurisdiction:: EU\n    - applies_to:: [[a]], [[b]]\n")

        claim = parse(text, source_path="p.md", spec=_declared_spec(_DECLARED_GRAMMAR)).claims[0]

        assert claim.fields == {"jurisdiction": "EU"}
        assert claim.links == {"applies_to": (UnitLink(target="a"), UnitLink(target="b"))}

    def test_a_grammar_of_only_a_section_parses_the_built_in_fields(self) -> None:
        text = _note_text('    - loc:: §1\n    - quote:: "a verbatim snippet"\n    - kind:: observation\n')

        claim = parse(text, source_path="p.md", spec=_declared_spec(ClaimGrammarSpec(section="findings"))).claims[0]

        assert claim.loc == "§1"
        assert claim.quote == "a verbatim snippet"
        assert claim.kind == "observation"
        assert claim.fields == {}
        assert claim.links == {}

    def test_an_undeclared_key_is_refused_naming_the_key_and_the_declared_fields(self) -> None:
        text = _note_text("    - loc:: §1\n    - mood:: cheerful\n")

        with pytest.raises(ClaimParseError) as exc:
            parse(text, source_path="p.md", spec=_declared_spec(_DECLARED_GRAMMAR))

        message = str(exc.value)
        assert "'mood'" in message
        assert "^c1" in message
        assert "applies_to" in message
        assert "jurisdiction" in message

    def test_a_declared_link_scalar_field_with_two_links_is_refused(self) -> None:
        grammar = ClaimGrammarSpec(section="findings", link_scalar_fields=("governed_by",))
        text = _note_text("    - loc:: §1\n    - governed_by:: [[a]], [[b]]\n")

        with pytest.raises(ClaimParseError) as exc:
            parse(text, source_path="p.md", spec=_declared_spec(grammar))

        assert "governed_by" in str(exc.value)
        assert "2" in str(exc.value)

    def test_a_declared_link_field_without_a_link_is_refused(self) -> None:
        text = _note_text("    - loc:: §1\n    - applies_to:: plain text\n")

        with pytest.raises(ClaimParseError) as exc:
            parse(text, source_path="p.md", spec=_declared_spec(_DECLARED_GRAMMAR))

        assert str(exc.value) == "claim ^c1 in p.md: field 'applies_to' takes [[page]] links, got 'plain text'"

    def test_disputed_by_without_a_link_is_refused(self) -> None:
        text = _note_text("    - loc:: §1\n    - disputed_by:: the other paper\n")

        with pytest.raises(ClaimParseError, match="field 'disputed_by' takes \\[\\[page\\]\\] links"):
            parse(text, source_path="p.md", spec=_declared_spec(_DECLARED_GRAMMAR))

    def test_an_empty_declared_link_field_holds_no_link(self) -> None:
        text = _note_text("    - loc:: §1\n    - applies_to::\n")

        claim = parse(text, source_path="p.md", spec=_declared_spec(_DECLARED_GRAMMAR)).claims[0]

        assert claim.links == {"applies_to": ()}

    def test_a_declared_link_scalar_field_keeps_its_one_link(self) -> None:
        grammar = ClaimGrammarSpec(section="findings", link_scalar_fields=("governed_by",))
        text = _note_text("    - loc:: §1\n    - governed_by:: [[a]]\n")

        claim = parse(text, source_path="p.md", spec=_declared_spec(grammar)).claims[0]

        assert claim.links == {"governed_by": (UnitLink(target="a"),)}

    def test_a_grammar_listing_the_built_in_fields_parses_them_as_built_in(self) -> None:
        # The built-in fields listed in the field lists, as a knowledge base's schema in another repository lists them.
        grammar = ClaimGrammarSpec(
            section="findings",
            edge_relations=("supports", "refutes"),
            multilink_fields=("disputed_by",),
            link_scalar_fields=("subject",),
            text_scalar_fields=("condition", "status"),
            plain_fields=("loc", "quote", "kind"),
        )
        text = _note_text(
            "    - loc:: §1\n"
            '    - quote:: "a verbatim snippet"\n'
            "    - kind:: observation\n"
            "    - subject:: [[s]]\n"
            "    - supports:: [[t]]\n"
            "    - condition:: in winter\n"
            "    - status:: disputed\n"
            "    - disputed_by:: [[note.y#^c2]]\n"
        )

        claim = parse(text, source_path="p.md", spec=_declared_spec(grammar)).claims[0]

        assert claim.loc == "§1"
        assert claim.quote == "a verbatim snippet"
        assert claim.kind == "observation"
        assert claim.subject == UnitLink(target="s")
        assert claim.edges == (("supports", UnitLink(target="t")),)
        assert claim.status == "disputed"
        assert claim.disputed_by == (UnitLink(target="note.y", block="c2"),)
        assert claim.fields == {"condition": "in winter"}
        assert claim.links == {}
