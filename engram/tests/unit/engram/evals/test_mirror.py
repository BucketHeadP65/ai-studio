"""Unit specs for the drafting-text recipe (`engram.evals.generation.domain.mirror`).

The single home of the mirror recipe: `assemble_mirror` is the exact string `accept` verifies
quotes against, `mirror_hash` is its `content_hash`, and `pinpoint_bodies` / `gold_page_bodies`
build the archetype-specific mirror constituents. These specs pin recipe parity with the seeder's
`_nonempty` ordering and the `accept` join, so the recipe never drifts across its consumers.
"""

from __future__ import annotations

import pytest

from engram.evals.generation.contracts import mirror_hash
from engram.evals.generation.domain.mirror import assemble_mirror, gold_page_bodies, passage_bodies, pinpoint_bodies
from engram.indexing.contracts import content_hash

_BODY_A = "Graph index construction incurs substantial token cost."
_BODY_B = "The map-reduce summarization step dominates query-time latency."


class TestAssembleMirror:
    def test_joins_bodies_with_newline_preserving_order(self):
        assert assemble_mirror((_BODY_A, _BODY_B)) == f"{_BODY_A}\n{_BODY_B}"

    def test_reversed_order_yields_a_different_join(self):
        assert assemble_mirror((_BODY_A, _BODY_B)) != assemble_mirror((_BODY_B, _BODY_A))

    def test_empty_bodies_yield_empty_string(self):
        assert assemble_mirror(()) == ""

    def test_single_body_has_no_separator(self):
        assert assemble_mirror((_BODY_A,)) == _BODY_A

    def test_equals_the_exact_join_accept_verifies_against(self):
        bodies = (_BODY_A, _BODY_B)

        assert assemble_mirror(bodies) == "\n".join(bodies)


class TestMirrorHash:
    def test_is_content_hash_of_the_assembled_mirror(self):
        bodies = (_BODY_A, _BODY_B)

        assert mirror_hash(bodies) == content_hash(assemble_mirror(bodies))

    def test_is_stable_for_the_same_bodies(self):
        assert mirror_hash((_BODY_A, _BODY_B)) == mirror_hash((_BODY_A, _BODY_B))

    def test_changes_when_a_constituent_changes(self):
        assert mirror_hash((_BODY_A, _BODY_B)) != mirror_hash((_BODY_A, f"{_BODY_B} extra"))

    def test_is_sensitive_to_body_order(self):
        assert mirror_hash((_BODY_A, _BODY_B)) != mirror_hash((_BODY_B, _BODY_A))


class TestPinpointBodies:
    def test_orders_text_then_quote_then_page_body(self):
        assert pinpoint_bodies("claim text", "the quote", "page body") == ("claim text", "the quote", "page body")

    def test_drops_a_none_quote_and_page_body(self):
        assert pinpoint_bodies("claim text", None, None) == ("claim text",)

    def test_drops_empty_strings_preserving_order(self):
        assert pinpoint_bodies("claim text", "", "page body") == ("claim text", "page body")

    @pytest.mark.parametrize(
        ("text", "quote", "page_body", "expected"),
        [
            ("t", "q", "c", ("t", "q", "c")),
            ("t", None, "c", ("t", "c")),
            ("t", "q", None, ("t", "q")),
            ("t", None, None, ("t",)),
            ("t", "", "", ("t",)),
        ],
    )
    def test_matches_the_seeders_nonempty_order(self, text: str, quote: str | None, page_body: str | None, expected: tuple[str, ...]):
        # Parity with the seeder's former `_nonempty(text, quote, card)`: keep truthy values in order.
        assert pinpoint_bodies(text, quote, page_body) == expected


class TestGoldPageBodies:
    def test_returns_card_texts_in_gold_id_order(self):
        card_texts = {"p1": "body 1", "p2": "body 2"}

        assert gold_page_bodies(("p2", "p1"), card_texts) == ("body 2", "body 1")

    def test_skips_absent_ids(self):
        card_texts = {"p1": "body 1"}

        assert gold_page_bodies(("p1", "missing"), card_texts) == ("body 1",)

    def test_empty_gold_ids_yield_empty(self):
        assert gold_page_bodies((), {"p1": "b"}) == ()


class TestPassageBodies:
    def test_is_the_section_body_alone(self):
        assert passage_bodies("section body") == ("section body",)

    def test_an_empty_body_yields_nothing(self):
        assert passage_bodies("") == ()
