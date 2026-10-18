"""The spec renders the knowledge base's structure for a reader: page_types, sections, id grammar, claim addressing."""

from engram.kb.contracts import CanonicalSection, KnowledgeBaseSchema, PageTypeSpec
from engram.kb.domain.schema import RetrievalSpec
from engram.testing.specs import research_spec


def test_research_orientation_teaches_every_kind_with_its_sections():
    text = research_spec().orientation()

    for pt in research_spec().page_types:
        assert f"- {pt.page_type} (ids `{pt.id_prefix}*`)" in text
        for slug in pt.canonical_section_slugs:
            assert slug in text
    assert "this map lists pages. A page lists its sections" in text
    assert "`<page-id>#<section>`" in text


def test_claim_bearing_kinds_say_how_claims_are_addressed():
    spec = research_spec()
    claim_types = [pt.page_type for pt in spec.page_types if pt.claims_enabled]
    assert claim_types, "the research spec is expected to carry at least one claim-bearing page_type"

    text = spec.orientation()

    assert "`<page-id>#c1`" in text
    assert spec.claims is not None
    assert spec.claims.section in text


def test_free_form_and_undisclosed_kinds_are_taught_as_such():
    spec = KnowledgeBaseSchema(
        slug="notes",
        purpose="Scratch notes.",
        page_types=(
            PageTypeSpec(page_type="note", id_prefix="note."),
            PageTypeSpec(page_type="scratch", id_prefix="scr.", disclosed=False),
        ),
    )

    text = spec.orientation()

    assert "free-form sections (open the page to list them)" in text
    assert "never listed on maps, reached through search hits" in text


def test_prefixless_kind_omits_the_id_clause():
    spec = KnowledgeBaseSchema(
        slug="estate",
        purpose="Estate records.",
        page_types=(PageTypeSpec(page_type="adr", id_prefix="", canonical_sections=(CanonicalSection(slug="context"), CanonicalSection(slug="decision"))),),
    )

    text = spec.orientation()

    assert "(ids `*`)" not in text
    assert "- adr: sections [context, decision]" in text


def test_kind_grouped_knowledge_base_says_the_map_lists_page_types():
    """`group_by="page_type"` maps lists page types, not pages directly — the structural sentence must say so."""
    spec = KnowledgeBaseSchema(
        slug="notes",
        purpose="Scratch notes.",
        page_types=(PageTypeSpec(page_type="note", id_prefix="note."),),
        retrieval=RetrievalSpec(group_by="page_type"),
    )

    text = spec.orientation()

    assert "this map lists page types. Open one to list its pages" in text
    assert "this map lists pages; a page lists its sections" not in text


def test_app_grouped_knowledge_base_says_the_map_lists_the_apps_documented():
    """`group_by="app"` maps lists apps, not pages directly, and names the grouping key as apps."""
    spec = KnowledgeBaseSchema(
        slug="repo-docs",
        purpose="Repo docs.",
        page_types=(PageTypeSpec(page_type="adr", id_prefix="adr."),),
        retrieval=RetrievalSpec(group_by="app"),
    )

    text = spec.orientation()

    assert "this map lists the apps documented here. Open one to list its pages" in text
    assert "this map lists pages; a page lists its sections" not in text


def test_ungrouped_knowledge_base_keeps_todays_exact_sentence():
    """`group_by=None` (the default) reproduces the original, unqualified sentence verbatim."""
    spec = KnowledgeBaseSchema(slug="notes", purpose="Scratch notes.", page_types=(PageTypeSpec(page_type="note", id_prefix="note."),))

    text = spec.orientation()

    assert text.startswith("Structure: this map lists pages. A page lists its sections. A section carries the text.")
