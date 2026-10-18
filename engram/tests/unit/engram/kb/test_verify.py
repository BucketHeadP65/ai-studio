"""Unit tests for quote-in-mirror verification (`src/engram/memory/verify.py`).

These pin the quote check the index *validate* stage runs over every
claim's `quote::` against the raw `Sources/` mirror.

Verification is Unicode-whitespace-normalized and nothing more: every run of
Unicode whitespace (ASCII spaces/tabs/newlines, U+00A0 NBSP, U+202F narrow NBSP,
and the other `str.isspace` separators) collapses to a single space, the ends are
stripped, then a plain substring test runs. Typographic glyph folding (curly vs
straight quotes, en/em dashes) is deliberately NOT done — the pandoc mirror emits
straight ASCII quotes and `--`/`---` for dashes — so a glyph-level mismatch must
NOT match (the boundary is pinned below).

`tests/fixtures/golden/mirror-excerpt.md` is a byte-for-byte copy of the LightRAG
mirror whose `## 3 The LightRAG Architecture` heading carries a genuine NBSP, and
whose body holds the exact passages the golden `lightrag-2025.md` claims quote.
"""

from __future__ import annotations

from engram.kb.contracts import quotes_in_mirror
from engram.kb.domain.verify import normalize_whitespace, quote_in_mirror
from engram.testing.knowledge_base_builder import GOLDEN_MIRROR_EXCERPT

_MIRROR_EXCERPT = GOLDEN_MIRROR_EXCERPT


class TestNormalizeWhitespace:
    """The single normalization primitive: collapse Unicode whitespace runs, strip."""

    def test_collapses_ascii_runs_and_strips(self):
        assert normalize_whitespace("  the   quick\tbrown\n\nfox  ") == "the quick brown fox"

    def test_collapses_nbsp_to_single_space(self):
        # U+00A0 (NBSP) and U+202F (narrow NBSP) are whitespace per the contract.
        assert normalize_whitespace("LightRAG Architecture") == "LightRAG Architecture"
        assert normalize_whitespace("a   b") == "a b"

    def test_idempotent(self):
        once = normalize_whitespace("foo   bar")
        assert normalize_whitespace(once) == once

    def test_does_not_fold_typographic_glyphs(self):
        # Contract scopes normalization to whitespace only — curly glyphs are left intact.
        assert normalize_whitespace("don’t") == "don’t"
        assert normalize_whitespace("a—b") == "a—b"


class TestQuoteInMirror:
    """`quote_in_mirror` — whitespace-normalized substring membership."""

    def test_exact_substring_matches(self):
        mirror = "In contrast, LightRAG optimizes this process by using fewer than 100 tokens."
        assert quote_in_mirror("LightRAG optimizes this process", mirror) is True

    def test_absent_quote_fails(self):
        mirror = "LightRAG indexes a knowledge base as an entity/relation graph."
        assert quote_in_mirror("GraphRAG community reports are cheap", mirror) is False

    def test_plain_space_quote_matches_nbsp_in_mirror(self):
        # The author writes a plain space; the pandoc mirror has a NBSP. Must match.
        mirror = "see the LightRAG Architecture section for details"
        assert quote_in_mirror("the LightRAG Architecture section", mirror) is True

    def test_nbsp_in_quote_matches_plain_space_in_mirror(self):
        # Symmetric: normalization is applied to both sides.
        mirror = "the LightRAG Architecture section"
        assert quote_in_mirror("the LightRAG Architecture section", mirror) is True

    def test_multiline_and_tab_runs_normalize(self):
        mirror = "graph-based\n   text   indexing\tparadigm"
        assert quote_in_mirror("graph-based text indexing paradigm", mirror) is True

    def test_leading_trailing_whitespace_ignored(self):
        mirror = "dual-level retrieval paradigm"
        assert quote_in_mirror("   dual-level retrieval paradigm   ", mirror) is True

    def test_case_sensitive_per_contract(self):
        # The contract normalizes whitespace only; case is significant (verbatim snippet).
        mirror = "LightRAG consistently outperforms GraphRAG"
        assert quote_in_mirror("lightrag consistently outperforms graphrag", mirror) is False

    def test_curly_quote_variant_does_not_match_straight_mirror(self):
        # Boundary: the contract folds whitespace, NOT glyphs. The real pandoc mirror
        # emits a straight apostrophe; a needle written with a curly apostrophe is a
        # genuine glyph mismatch and must fail (authors write the ASCII the mirror has).
        mirror = "GraphRAG's requirement to traverse each community"
        assert quote_in_mirror("GraphRAG’s requirement to traverse each community", mirror) is False
        # ...and the straight-apostrophe form the mirror actually contains matches.
        assert quote_in_mirror("GraphRAG's requirement to traverse each community", mirror) is True


class TestQuotesInMirror:
    """`quotes_in_mirror` — every `"..."` snippet in a (possibly multi-snippet) value must match.

    Table-derived claims store several quoted snippets in one `quote::` value
    (`parser._unquote` preserves these verbatim — interior quotes are not
    stripped), e.g. `"0.7535" vs "0.7463"` or `"a"; "b"`. The quote check
    check passes only when EVERY individual snippet is found in the mirror; a
    value with no `"..."` snippet is treated as one snippet (unchanged from
    :func:`quote_in_mirror`).
    """

    def test_multi_snippet_all_present_passes(self):
        mirror = "Method A scored 0.7535 while B reached 0.7463 and C only 0.7407 on the run."
        assert quotes_in_mirror('"0.7535" vs "0.7463" vs "0.7407"', mirror) is True

    def test_multi_snippet_one_missing_fails(self):
        # First two snippets are present; the third ("0.7407") is not in the mirror.
        mirror = "Method A scored 0.7535 while B reached 0.7463 on the run."
        assert quotes_in_mirror('"0.7535" vs "0.7463" vs "0.7407"', mirror) is False

    def test_two_snippets_semicolon_separated_all_present(self):
        mirror = "the cache reduces generation time; it is provided dynamically during inference here."
        assert quotes_in_mirror('"reduces generation time"; "provided dynamically during inference"', mirror) is True

    def test_plain_value_with_no_snippets_unchanged(self):
        # No `"..."` in the value → whole value is one snippet (matches quote_in_mirror).
        mirror = "LightRAG optimizes this process by using fewer than 100 tokens."
        assert quotes_in_mirror("LightRAG optimizes this process", mirror) is True
        assert quotes_in_mirror("a phrase that is absent entirely", mirror) is False

    def test_single_wrapped_snippet_matches_when_present(self):
        mirror = "the dual-level retrieval paradigm is central here"
        assert quotes_in_mirror('"dual-level retrieval paradigm"', mirror) is True

    def test_whitespace_normalized_per_snippet(self):
        # Each snippet is matched under the same whitespace normalization.
        mirror = "scored 0.7535\n   on   the\trun and also 0.7463 elsewhere"
        assert quotes_in_mirror('"0.7535" vs "0.7463"', mirror) is True

    def test_ellipsis_elided_single_snippet_fails(self):
        # The genuine drift case (paper.simgrag-2024 ^c5): an author-elided quote with a
        # literal `...` is NOT a verbatim span — extracted as one snippet, it must fail.
        mirror = (
            "For queries involving up to 3 hops in the MetaQA dataset, our method achieves the accuracies of 98% and 93%, respectively."
        )
        quote = '"For queries involving up to 3 hops in the MetaQA ... achieves the accuracies of 98% and 93%, respectively"'
        assert quotes_in_mirror(quote, mirror) is False

    def test_empty_snippet_is_ignored_not_a_vacuous_pass(self):
        # A degenerate `""` snippet carries nothing to verify; it must not flip the
        # result to pass when a real sibling snippet is absent.
        mirror = "only the first value 0.7535 appears here"
        assert quotes_in_mirror('"0.7535" and "" and "0.9999"', mirror) is False


class TestRealGoldenQuotesAgainstRealMirror:
    """The regression net: real golden-fixture quotes verified against the real mirror excerpt."""

    def test_golden_c1_quote_verifies(self):
        mirror_text = _MIRROR_EXCERPT.read_text(encoding="utf-8")
        # lightrag-2025.md ^c1 quote, verbatim:
        quote = "LightRAG consistently outperforms GraphRAG, particularly in larger datasets with complex language contexts"
        assert quote_in_mirror(quote, mirror_text) is True

    def test_golden_c8_quote_verifies(self):
        mirror_text = _MIRROR_EXCERPT.read_text(encoding="utf-8")
        # lightrag-2025.md ^c8 quote, verbatim:
        quote = "using fewer than 100 tokens for keyword generation and retrieval, requiring only a single API call"
        assert quote_in_mirror(quote, mirror_text) is True

    def test_nbsp_heading_matches_plain_space_quote(self):
        # The fixture heading is "## 3 The LightRAG Architecture" with a real NBSP;
        # a plain-space quote of it must verify (the headline NBSP case).
        mirror_text = _MIRROR_EXCERPT.read_text(encoding="utf-8")
        assert " " in mirror_text  # guard: the fixture really does carry a NBSP
        assert quote_in_mirror("The LightRAG Architecture", mirror_text) is True

    def test_quote_absent_from_mirror_fails(self):
        mirror_text = _MIRROR_EXCERPT.read_text(encoding="utf-8")
        assert quote_in_mirror("a sentence that does not appear anywhere in the mirror excerpt", mirror_text) is False
