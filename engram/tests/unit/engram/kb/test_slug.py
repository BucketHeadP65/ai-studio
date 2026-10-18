"""The heading-slug contract: ltree-safe, transliterated, never empty, distinct."""

from core.markdown import heading_for_slug, heading_slug


class TestHeadingSlug:
    def test_ascii_is_unchanged(self):
        # Pure-ASCII headings slug under the plain `[^a-z0-9]+ -> _` rule.
        assert heading_slug("Overview") == "overview"
        assert heading_slug("TL;DR") == "tl_dr"
        assert heading_slug("Graph Indexing") == "graph_indexing"

    def test_greek_transliterates_instead_of_collapsing_to_empty(self):
        # An all-Greek heading must not slug to "" — that collides across every such heading.
        assert heading_slug("Τιμές") == "times"
        assert heading_slug("Παραλίες") == "paralies"

    def test_distinct_non_latin_headings_get_distinct_slugs(self):
        assert heading_slug("Τιμές") != heading_slug("Παραλίες")

    def test_accented_latin_is_folded(self):
        assert heading_slug("Café") == "cafe"
        assert heading_slug("Résumé") == "resume"

    def test_untransliterable_heading_falls_back_to_a_stable_nonempty_slug(self):
        slug = heading_slug("🎉🎊")
        assert slug  # non-empty — a valid ltree label, not ""
        assert slug == heading_slug("🎉🎊")  # stable across calls

    def test_result_is_always_ltree_safe(self):
        for heading in ("Τιμές", "Café", "TL;DR", "🎉", "## weird ##", "数据科学"):
            slug = heading_slug(heading)
            assert slug
            assert all(c.islower() or c.isdigit() or c == "_" for c in slug)


class TestHeadingForSlug:
    """`heading_for_slug` is the inverse direction: a slug's synthesized display heading round-trips."""

    def test_single_word_slug_title_cased(self):
        assert heading_for_slug("findings") == "Findings"

    def test_multi_word_slug_each_word_title_cased(self):
        assert heading_for_slug("open_questions") == "Open Questions"

    def test_round_trips_through_heading_slug(self):
        for slug in ("findings", "open_questions", "tl_dr", "related_work"):
            assert heading_slug(heading_for_slug(slug)) == slug
