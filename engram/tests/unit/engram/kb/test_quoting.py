"""Tests for `memory.quoting` — the single home of the quote escape/unescape/split symmetry.

The renderer's `quote`, the parser's `unquote`, and verify's `split_snippets` are three
faces of one on-disk grammar. These tests pin the render to parse round-trip stability and
the multi-snippet split so the consolidated module cannot silently drift from the call sites
that delegate to it.
"""

from __future__ import annotations

import pytest

from engram.kb.domain import quoting

# Values whose stored (unwrapped) form survives quote then unquote unchanged. A bare snippet
# with no interior double quote re-wraps to `"..."` and unwraps back to itself; a value that
# already carries a `"` (table-derived multi-snippet, a stray interior quote, the empty-string
# degenerate) is left verbatim by both directions, so the round-trip is still the identity.
_ROUND_TRIP_VALUES = [
    "",  # empty: re-wraps to "" then unwraps back to empty
    "a verbatim snippet",  # plain text, no interior quote -> wraps and unwraps cleanly
    "spans\na newline",  # an embedded newline is opaque to the quote grammar
    "has, commas; and: punctuation",  # delimiter-looking punctuation is not a quote delimiter
    "has a | pipe and a \\| escaped pipe",  # unit link-alias pipes are opaque to quoting
    '"0.7535" vs "0.7463"',  # multi-snippet (interior quotes) -> left verbatim both ways
    '"a"; "b"',  # semicolon-packed multi-snippet -> left verbatim both ways
    'a stray " interior quote',  # an unbalanced interior quote -> left verbatim both ways
]


class TestRoundTrip:
    """`unquote(quote(x)) == x` — the render to parse round-trip is the identity on stored values."""

    @pytest.mark.parametrize("value", _ROUND_TRIP_VALUES)
    def test_unquote_of_quote_is_identity(self, value: str):
        assert quoting.unquote(quoting.quote(value)) == value

    def test_quote_wraps_a_bare_snippet(self):
        # The render direction: a snippet with no interior quote gains a wrapping pair.
        assert quoting.quote("verbatim snippet") == '"verbatim snippet"'

    def test_quote_leaves_a_value_with_an_interior_quote_verbatim(self):
        # A table-derived value already carries quotes; re-wrapping would corrupt it.
        assert quoting.quote('"50.4%" vs "49.6%"') == '"50.4%" vs "49.6%"'

    def test_unquote_strips_a_single_wrapping_pair(self):
        assert quoting.unquote('"verbatim snippet"') == "verbatim snippet"

    def test_unquote_leaves_a_multi_snippet_value_verbatim(self):
        # Interior quotes -> not one balanced wrapped string -> kept verbatim for split_snippets.
        value = '"50.4%" vs "49.6%"'
        assert quoting.unquote(value) == value

    def test_degenerate_empty_pair_unquotes_to_empty(self):
        # A bare `""` is a balanced wrapped pair with no interior quote, so unquote strips it to
        # empty. It is therefore never a STORED form (which is why it is excluded from the
        # round-trip identity list) — the parser would already have unwrapped it on read.
        assert quoting.unquote('""') == ""


class TestSplitSnippets:
    """`split_snippets` — the verify direction that breaks a value into its independent assertions."""

    def test_multi_snippet_value_splits_into_each_bare_snippet(self):
        assert quoting.split_snippets('"0.7535" vs "0.7463" vs "0.7407"') == ("0.7535", "0.7463", "0.7407")

    def test_semicolon_packed_value_splits_into_each_bare_snippet(self):
        assert quoting.split_snippets('"reduces generation time"; "provided dynamically"') == (
            "reduces generation time",
            "provided dynamically",
        )

    def test_single_snippet_value_is_a_one_tuple_of_its_bare_text(self):
        assert quoting.split_snippets('"a verbatim snippet"') == ("a verbatim snippet",)

    def test_value_with_no_quoted_snippet_is_a_one_tuple_of_the_whole_value(self):
        # The already-unwrapped single-quote case: the whole value is one snippet.
        assert quoting.split_snippets("LightRAG optimizes this process") == ("LightRAG optimizes this process",)


class TestSymmetryAgreement:
    """The three operations agree: a rendered multi-snippet value splits and unquotes back to its parts."""

    def test_split_of_rendered_snippets_unquotes_each_back(self):
        # Render each bare snippet, pack them into one value the way a table-derived claim does,
        # then split it back: each snippet round-trips through the same grammar the call sites share.
        parts = ("0.7535", "0.7463")
        packed = " vs ".join(quoting.quote(part) for part in parts)
        assert quoting.split_snippets(packed) == parts
        # Each rendered snippet, taken alone, unquotes back to its bare text.
        for part in parts:
            assert quoting.unquote(quoting.quote(part)) == part
