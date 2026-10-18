"""Threading KnowledgeBaseSchema through the parse/render/validate kernel.

The guiding invariant: **a spec drives the kernel's whole grammar**; nothing about a page type
is hardcoded, and every call site names its own spec (there is no default to fall back on).
These tests prove the three guarantees:

1. **Spec drives sections** — a custom spec with different canonical_sections for a
   page_type governs `parse` and `render`, not the deleted constants.
2. **Claim grammar from spec** — a custom spec with different edge_relations /
   quote_required_types / comparative_relations drives `parse` field-set absorption
   and `validate_note` claim checks.
3. **Claimless knowledge base** — a spec with `claims=None` (and no `claims_enabled` page_types)
   parses notes without claim grammar; `validate_note` emits no quote/comparative findings.
"""

from __future__ import annotations

import pytest

from engram.indexing.contracts import validate_note
from engram.kb.contracts import (
    AuthoringPayload,
    CanonicalSection,
    ClaimGrammarSpec,
    FacetSpec,
    KnowledgeBaseSchema,
    PageTypeSpec,
    RelationDecl,
    parse,
    render,
)
from engram.kb.domain.payload import AuthoringSection
from engram.kb.exceptions import ClaimParseError
from engram.testing.specs import research_spec

# ---------------------------------------------------------------------------
# Tiny spec builder helpers
# ---------------------------------------------------------------------------


def _make_spec(
    *,
    page_type: str = "concept",
    id_prefix: str = "concept.",
    canonical_sections: tuple[str, ...] = ("overview", "themes"),
    claims_enabled: bool = False,
    claims: ClaimGrammarSpec | None = None,
    facets: tuple[FacetSpec, ...] = (),
    relations: tuple[RelationDecl, ...] = (),
) -> KnowledgeBaseSchema:
    """Build a tiny KnowledgeBaseSchema for testing; no real knowledge base file needed."""
    return KnowledgeBaseSchema.model_validate(
        {
            "slug": "test-cooking",
            "page_types": (
                PageTypeSpec(
                    page_type=page_type,
                    id_prefix=id_prefix,
                    canonical_sections=tuple(CanonicalSection(slug=slug) for slug in canonical_sections),
                    claims_enabled=claims_enabled,
                ),
            ),
            "claims": claims,
            "facets": facets,
            "relations": relations,
        }
    )


def _make_research_vocabulary():
    """Build a minimal VocabularyIndex that satisfies validate_note's vocabulary checks."""
    from engram.kb import contracts as kb

    registry_md = """# Relations

| relation | domain | range | description |
|---|---|---|---|
| supports | paper | paper | evidential support |
| outperforms | paper | entity | comparative result |
| pairs_with | paper | paper | custom relation |

# Entity types

| value | definition |
|---|---|
| model | a neural model |

# Claim page_types

| value | definition |
|---|---|
| benchmark-result | a measured result |
| ablation | an ablation study |
| observation | a general observation |

# Facets

## tasks

| value | definition |
|---|---|
| classification | classification task |

## domains

| value | definition |
|---|---|
| nlp | natural language processing |
"""
    return kb.parse_vocabulary([("vocabulary.md", registry_md)]).index


# ---------------------------------------------------------------------------
# Fixtures: minimal note texts
# ---------------------------------------------------------------------------

# Used with the DEFAULT (research) spec — matches research prefix for "concept" (con.)
_CONCEPT_FM = """\
---
id: con.test-concept
type: concept
title: Test Concept
status: draft
---
"""

# Used with CUSTOM specs that declare id_prefix="concept." — must match that prefix
_CUSTOM_CONCEPT_FM = """\
---
id: concept.test-concept
type: concept
title: Test Concept
status: draft
---
"""

_PAPER_FM_TEMPLATE = """\
---
id: paper.test-paper
type: paper
title: Test Paper
status: draft
paper_type: method
year: 2025
zotero_key: TEST
source_hash: abc
---
"""


# ---------------------------------------------------------------------------
# Test 1: Spec drives canon-section tagging in parse
# ---------------------------------------------------------------------------


class TestSpecDrivesSections:
    """A custom spec's canonical_sections governs which H2s get canon=slug (not the deleted constant)."""

    def test_custom_spec_concept_sections_governs_canon(self):
        """Custom spec: 'overview' and 'recipe' are canonical; 'approach' is not."""
        custom_spec = _make_spec(page_type="concept", id_prefix="concept.", canonical_sections=("overview", "recipe"))
        body = "## Overview\n\nbody\n\n## Approach\n\ncontent\n\n## Recipe\n\ncook it\n"
        note = parse(_CUSTOM_CONCEPT_FM + "\n" + body, source_path="test.md", spec=custom_spec)

        section_by_slug = {s.canon: s.path for s in note.sections if s.canon is not None}
        # 'overview' and 'recipe' are canonical in the custom spec
        assert "overview" in section_by_slug
        assert "recipe" in section_by_slug
        # 'approach' is NOT canonical in the custom spec (it's custom, not research)
        non_canon = [s for s in note.sections if s.path == "approach"]
        assert len(non_canon) == 1
        assert non_canon[0].canon is None

    def test_research_spec_marks_concepts_declared_sections_canonical(self):
        """The research spec's own `concept` page_type marks overview/themes/open_questions canonical."""
        body = "## Overview\n\nbody\n\n## Themes\n\ncontent\n\n## Open Questions\n\nq\n"
        text = _CONCEPT_FM + "\n" + body
        note = parse(text, source_path="test.md", spec=research_spec())

        assert {s.path: s.canon for s in note.sections} == {
            "overview": "overview",
            "themes": "themes",
            "open_questions": "open_questions",
        }


# ---------------------------------------------------------------------------
# Test 2: Spec drives canonical render order
# ---------------------------------------------------------------------------


class TestSpecDrivesRenderOrder:
    """A custom spec's canonical_sections governs the render section order."""

    def test_custom_spec_drives_render_order(self):
        """Custom spec puts 'recipe' before 'overview'; render order follows the spec."""
        custom_spec = _make_spec(
            page_type="concept",
            id_prefix="concept.",
            # NOTE: recipe comes first — the opposite of what the research spec would do
            canonical_sections=("recipe", "overview"),
        )
        body = "## Overview\n\nbody\n\n## Recipe\n\ncook it\n"
        text = _CUSTOM_CONCEPT_FM + "\n" + body
        note = parse(text, source_path="test.md", spec=custom_spec)
        rendered = render(note, spec=custom_spec)

        recipe_pos = rendered.index("## Recipe")
        overview_pos = rendered.index("## Overview")
        assert recipe_pos < overview_pos, "Custom spec should put Recipe before Overview"

    def test_research_spec_render_order_puts_declared_sections_first(self):
        """The research spec's concept page_type renders overview, themes, open_questions in that order."""
        body = "## Open Questions\n\nq\n\n## Overview\n\nbody\n\n## Themes\n\ncontent\n"
        text = _CONCEPT_FM + "\n" + body
        note = parse(text, source_path="test.md", spec=research_spec())

        rendered = render(note, spec=research_spec())

        overview_pos = rendered.index("## Overview")
        themes_pos = rendered.index("## Themes")
        open_questions_pos = rendered.index("## Open Questions")
        assert overview_pos < themes_pos < open_questions_pos


# ---------------------------------------------------------------------------
# Test 3: Claim grammar from spec — parser field classification
# ---------------------------------------------------------------------------


class TestClaimGrammarFromSpec:
    """A custom spec's claim grammar governs which fields are absorbed as edges/links/scalars."""

    def _paper_with_claim(self, claim_body: str) -> str:
        return _PAPER_FM_TEMPLATE + "\n## Findings\n\n" + claim_body + "\n"

    def test_custom_edge_relation_classified_as_edge(self):
        """A field declared as an edge_relation in the spec becomes a claim edge."""
        custom_spec = _make_spec(
            page_type="paper",
            id_prefix="paper.",
            canonical_sections=("findings",),
            claims_enabled=True,
            claims=ClaimGrammarSpec(section="findings", edge_relations=("pairs_with",), plain_fields=("loc", "kind", "quote")),
        )
        claim = "- Model A pairs better with B ^c1\n  - loc:: §3\n  - kind:: observation\n  - pairs_with:: [[Model B]]\n"
        note = parse(self._paper_with_claim(claim), source_path="paper.md", spec=custom_spec)

        assert len(note.claims) == 1
        edges = note.claims[0].edges
        assert len(edges) == 1
        assert edges[0][0] == "pairs_with"

    def test_unknown_relation_not_absorbed_by_the_research_spec(self):
        """A field NOT in edge_relations is never absorbed as an edge: it is refused as undeclared."""
        research_spec_note = (
            "- Some result ^c1\n"
            "  - loc:: §3\n"
            "  - kind:: observation\n"
            "  - pairs_with:: [[Model B]]\n"  # not in research spec edge_relations
        )

        with pytest.raises(ClaimParseError, match="'pairs_with'"):
            parse(self._paper_with_claim(research_spec_note), source_path="paper.md", spec=research_spec())

    def test_lru_cache_does_not_bleed_between_calls(self):
        """Two back-to-back parses with different specs each use their own grammar."""
        spec_a = _make_spec(
            page_type="paper",
            id_prefix="paper.",
            canonical_sections=("findings",),
            claims_enabled=True,
            claims=ClaimGrammarSpec(section="findings", edge_relations=("pairs_with",), plain_fields=("loc", "kind", "quote")),
        )
        spec_b = _make_spec(
            page_type="paper",
            id_prefix="paper.",
            canonical_sections=("findings",),
            claims_enabled=True,
            claims=ClaimGrammarSpec(
                section="findings",
                edge_relations=("supports",),  # different relation
                plain_fields=("loc", "kind", "quote"),
            ),
        )
        body_a = self._paper_with_claim("- Model A ^c1\n  - loc:: §3\n  - kind:: observation\n  - pairs_with:: [[Model B]]\n")
        body_b = self._paper_with_claim("- Model A ^c1\n  - loc:: §3\n  - kind:: observation\n  - supports:: [[Paper X]]\n")
        note_a = parse(body_a, source_path="paper.md", spec=spec_a)
        note_b = parse(body_b, source_path="paper.md", spec=spec_b)

        # spec_a: pairs_with is the edge
        assert any(rel == "pairs_with" for rel, _ in note_a.claims[0].edges)
        # spec_b: supports is the edge, and spec_a's relation is undeclared there
        assert any(rel == "supports" for rel, _ in note_b.claims[0].edges)
        with pytest.raises(ClaimParseError, match="'pairs_with'"):
            parse(body_a, source_path="paper.md", spec=spec_b)


# ---------------------------------------------------------------------------
# Test 4: Validator claim checks from spec
# ---------------------------------------------------------------------------


class TestValidatorClaimChecksFromSpec:
    """Custom spec governs quote_required_types and comparative_relations in validate_note."""

    def test_custom_quote_required_kind_triggers_lint(self):
        """A custom spec can declare 'observation' as quote-required; validation fires."""
        custom_spec = _make_spec(
            page_type="paper",
            id_prefix="paper.",
            canonical_sections=("findings",),
            claims_enabled=True,
            claims=ClaimGrammarSpec(
                section="findings",
                edge_relations=("supports",),
                plain_fields=("loc", "kind", "quote"),
                quote_required_types=("observation",),  # custom: observation needs quote
            ),
        )
        claim = "- Some observation ^c1\n  - loc:: §3\n  - kind:: observation\n"
        note = parse(_PAPER_FM_TEMPLATE + "\n## Findings\n\n" + claim + "\n", source_path="paper.md", spec=custom_spec)
        vocabulary = _make_research_vocabulary()
        report = validate_note(note, vocabulary, spec=custom_spec)

        missing_quote_issues = [i for i in report.errors if i.rule == "missing-quote"]
        assert len(missing_quote_issues) >= 1, "Expected a missing-quote error for 'observation' page_type in custom spec"

    def test_research_spec_observation_does_not_require_quote(self):
        """With the research spec, 'observation' does NOT require a quote (only benchmark-result / ablation do)."""
        claim = "- Some observation ^c1\n  - loc:: §3\n  - kind:: observation\n"
        note = parse(_PAPER_FM_TEMPLATE + "\n## Findings\n\n" + claim + "\n", source_path="paper.md", spec=research_spec())
        vocabulary = _make_research_vocabulary()
        report = validate_note(note, vocabulary, spec=research_spec())

        missing_quote_issues = [i for i in report.errors if i.rule == "missing-quote"]
        assert len(missing_quote_issues) == 0, "Research spec should not require quote for 'observation'"

    def test_custom_comparative_relation_triggers_lint(self):
        """A custom spec can declare 'pairs_with' as comparative; validation fires when qualifiers absent."""
        custom_spec = _make_spec(
            page_type="paper",
            id_prefix="paper.",
            canonical_sections=("findings",),
            claims_enabled=True,
            claims=ClaimGrammarSpec(
                section="findings",
                edge_relations=("pairs_with",),
                link_scalar_fields=("subject", "on_task", "metric"),
                multilink_fields=("on_dataset",),
                plain_fields=("loc", "kind", "quote"),
                comparative_relations=("pairs_with",),  # custom: pairs_with is comparative
            ),
            relations=(RelationDecl(slug="pairs_with", domain="paper", range="paper", qualifiers=(("on_dataset", "on_task"), ("metric",))),),
        )
        claim = (
            "- Model A ^c1\n  - loc:: §3\n  - kind:: observation\n  - quote:: some quote\n  - pairs_with:: [[Model B]]\n"
            # missing on_dataset/on_task + metric
        )
        note = parse(_PAPER_FM_TEMPLATE + "\n## Findings\n\n" + claim + "\n", source_path="paper.md", spec=custom_spec)
        vocabulary = _make_research_vocabulary()
        report = validate_note(note, vocabulary, spec=custom_spec)

        comp_issues = [i for i in report.errors if i.rule == "comparative-missing-qualifiers"]
        assert len(comp_issues) >= 1, "Expected comparative-missing-qualifiers error with custom spec"


# ---------------------------------------------------------------------------
# Test 5: Claimless knowledge base
# ---------------------------------------------------------------------------


class TestClaimlessKnowledgeBase:
    """A spec with claims=None degrades gracefully: parse works, no claim validation fires."""

    def _claimless_spec(self) -> KnowledgeBaseSchema:
        return KnowledgeBaseSchema.model_validate(
            {
                "slug": "cooking-knowledge-base",
                "page_types": (
                    PageTypeSpec(
                        page_type="concept",
                        id_prefix="concept.",
                        canonical_sections=(CanonicalSection(slug="overview"), CanonicalSection(slug="recipe")),
                        claims_enabled=False,  # no claims anywhere
                    ),
                ),
                "claims": None,  # no claim grammar
            }
        )

    def test_parse_does_not_raise_for_claimless_knowledge_base(self):
        """Parse succeeds (no claims section, empty claim list) for a claimless knowledge base spec."""
        spec = self._claimless_spec()
        body = "## Overview\n\nbody\n\n## Recipe\n\ncook it\n"
        note = parse(_CUSTOM_CONCEPT_FM + "\n" + body, source_path="test.md", spec=spec)
        assert len(note.claims) == 0
        assert len(note.sections) == 2

    def test_validate_note_emits_no_claim_issues_for_claimless_knowledge_base(self):
        """validate_note with a claimless spec emits no claim-quote or comparative issues."""
        spec = self._claimless_spec()
        body = "## Overview\n\nbody\n"
        note = parse(_CUSTOM_CONCEPT_FM + "\n" + body, source_path="test.md", spec=spec)
        vocabulary = _make_research_vocabulary()
        report = validate_note(note, vocabulary, spec=spec)

        claim_rules = {"missing-quote", "comparative-missing-qualifiers", "missing-page_type", "missing-loc"}
        claim_issues = [i for i in report.errors if i.rule in claim_rules]
        assert len(claim_issues) == 0, f"Claimless knowledge base should have no claim validation issues; got {claim_issues}"


# ---------------------------------------------------------------------------
# Test 6: Claim section slug from spec (claims.section != "findings")
# ---------------------------------------------------------------------------


class TestSpecDrivesClaimSection:
    """A spec whose `claims.section` is not `"findings"` governs parse and render.

    Regression guard: `claims.section` was configurable in the spec but the parser and
    renderer hardcoded `"findings"`, so a knowledge base that scaffolded a differently-named
    claims section could never have claims parsed back from it.
    """

    def _evidence_spec(self) -> KnowledgeBaseSchema:
        return _make_spec(
            page_type="paper",
            id_prefix="paper.",
            canonical_sections=("evidence",),
            claims_enabled=True,
            claims=ClaimGrammarSpec(section="evidence", plain_fields=("loc", "kind", "quote")),
        )

    def test_parser_extracts_claims_from_configured_section(self):
        """A claim under `## Evidence` is parsed when the spec's claims.section is 'evidence'."""
        spec = self._evidence_spec()
        claim = "- Model A wins ^c1\n  - loc:: §3\n  - kind:: observation\n"
        note = parse(_PAPER_FM_TEMPLATE + "\n## Evidence\n\n" + claim + "\n", source_path="paper.md", spec=spec)

        assert len(note.claims) == 1
        assert note.claims[0].text == "Model A wins"

    def test_parser_ignores_findings_when_section_is_evidence(self):
        """With claims.section='evidence', a `## Findings` section yields no claims."""
        spec = self._evidence_spec()
        claim = "- Model A wins ^c1\n  - loc:: §3\n  - kind:: observation\n"
        note = parse(_PAPER_FM_TEMPLATE + "\n## Findings\n\n" + claim + "\n", source_path="paper.md", spec=spec)

        assert len(note.claims) == 0

    def test_renderer_renders_claims_under_supplied_evidence_section(self):
        """Claims parsed from `## Evidence` render back under `## Evidence` (round-trip)."""
        spec = self._evidence_spec()
        claim = "- Model A wins ^c1\n  - loc:: §3\n  - kind:: observation\n"
        note = parse(_PAPER_FM_TEMPLATE + "\n## Evidence\n\n" + claim + "\n", source_path="paper.md", spec=spec)
        rendered = render(note, spec=spec)

        assert "## Evidence" in rendered
        assert "## Findings" not in rendered
        assert "- Model A wins ^c1" in rendered
        assert "loc:: §3" in rendered

    def test_renderer_synthesizes_evidence_section_when_claims_have_no_section(self):
        """When claims exist but no claims section was supplied, the renderer synthesizes `## Evidence`."""
        from engram.kb.contracts import AuthoringPayload
        from engram.kb.domain.payload import AuthoringClaim

        spec = self._evidence_spec()
        payload = AuthoringPayload(
            frontmatter={
                "id": "paper.test-paper",
                "type": "paper",
                "title": "Test Paper",
                "status": "draft",
                "paper_type": "method",
                "year": 2025,
                "zotero_key": "TEST",
                "source_hash": "abc",
            },
            sections=(),
            claims=(AuthoringClaim(text="Model A wins", loc="§3", kind="observation"),),
            knowledge_base_schema=spec,
        )
        rendered = render(payload, spec=spec)

        assert "## Evidence" in rendered
        assert "## Findings" not in rendered
        assert "- Model A wins ^c1" in rendered

    def test_round_trip_through_evidence_section_is_idempotent(self):
        """render(parse(render(note))) is byte-identical with a custom claims section."""
        spec = self._evidence_spec()
        claim = "- Model A wins ^c1\n  - loc:: §3\n  - kind:: observation\n"
        note = parse(_PAPER_FM_TEMPLATE + "\n## Evidence\n\n" + claim + "\n", source_path="paper.md", spec=spec)
        first = render(note, spec=spec)
        second = render(parse(first, source_path="paper.md", spec=spec), spec=spec)

        assert first == second


# ---------------------------------------------------------------------------
# Test 7: A canonical section's declared heading governs the rendered H2 text
# ---------------------------------------------------------------------------


class TestSpecDrivesCanonicalHeadings:
    """A `CanonicalSection.heading` declaration wins over the generic title-cased slug at render time."""

    def _spec_with_declared_heading(self) -> KnowledgeBaseSchema:
        return KnowledgeBaseSchema.model_validate(
            {
                "slug": "test-cooking",
                "page_types": (
                    PageTypeSpec(
                        page_type="concept",
                        id_prefix="concept.",
                        canonical_sections=(CanonicalSection(slug="tl_dr", heading="TL;DR"), CanonicalSection(slug="overview")),
                    ),
                ),
            }
        )

    def _concept_payload(self, spec: KnowledgeBaseSchema, canon: str, body: str) -> AuthoringPayload:
        return AuthoringPayload(
            frontmatter={"id": "concept.test", "type": "concept", "title": "Test", "status": "draft"},
            sections=(AuthoringSection.model_validate({"canon": canon, "body": body}),),
            knowledge_base_schema=spec,
        )

    def test_a_spec_declared_heading_renders_verbatim_from_the_canon_alias(self):
        # The canon alias alone would otherwise title-case "tl_dr" to "Tl Dr".
        spec = self._spec_with_declared_heading()
        payload = self._concept_payload(spec, "tl_dr", "short")

        rendered = render(payload, spec=spec)

        assert "## TL;DR" in rendered
        assert "## Tl Dr" not in rendered

    def test_a_bare_slug_entry_still_title_cases(self):
        # "overview" carries no declared heading in this spec.
        spec = self._spec_with_declared_heading()
        payload = self._concept_payload(spec, "overview", "body")

        rendered = render(payload, spec=spec)

        assert "## Overview" in rendered

    def test_round_trip_through_the_canon_alias_still_resolves(self):
        spec = self._spec_with_declared_heading()
        payload = self._concept_payload(spec, "tl_dr", "short")

        rendered = render(payload, spec=spec)
        note = parse(rendered, source_path="test.md", spec=spec)

        section_by_slug = {s.canon: s for s in note.sections if s.canon is not None}
        assert "tl_dr" in section_by_slug

    def test_research_spec_paper_tl_dr_renders_the_declared_heading(self):
        """Grounded in the real research bundle's spec: `paper`'s `tl_dr` declares `TL;DR`."""
        spec = research_spec()
        payload = AuthoringPayload(
            frontmatter=_paper_frontmatter_dict(),
            sections=(AuthoringSection.model_validate({"canon": "tl_dr", "body": "short"}),),
            knowledge_base_schema=spec,
        )

        rendered = render(payload, spec=spec)

        assert "## TL;DR" in rendered
        assert "## Tl Dr" not in rendered


def _paper_frontmatter_dict() -> dict[str, str | int]:
    """The `_PAPER_FM_TEMPLATE` frontmatter as a payload dict, for the real research spec's `paper` page_type."""
    return {
        "id": "paper.test-paper",
        "type": "paper",
        "title": "Test Paper",
        "status": "draft",
        "paper_type": "method",
        "year": 2025,
        "zotero_key": "TEST",
        "source_hash": "abc",
    }
