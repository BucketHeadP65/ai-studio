"""`is_locator` and `swapped_locator_quotes`: a claim whose quote is only a locator, and whose loc is the source's words."""

from __future__ import annotations

import pytest

from engram.authoring.domain import is_locator, swapped_locator_quotes


@pytest.mark.parametrize(
    "text",
    ["§7", "§ 4.1", '"§8"', "§5.1, Table 2", "p. 4", "pp. 4-5", "page 12", "Section 3", "Sec. 3.2", "Appendix A", "Table 2", "Fig. 3", "figure 12"],
)
def test_a_locator_alone_is_recognized(text: str) -> None:
    assert is_locator(text)


@pytest.mark.parametrize("text", ["Table 2 shows the gap", "second", "Section", "§", "", "   ", "we believe that a good code LM", "p4 of the appendix"])
def test_words_are_not_a_locator(text: str) -> None:
    assert not is_locator(text)


def _in_mirror(text: str) -> bool:
    return text == "a good code LM still ought to reason about execution"


def test_a_locator_quote_whose_loc_is_in_the_mirror_is_swapped() -> None:
    claims = [{"text": "LMs should reason.", "kind": "author-claim", "quote": '"§7"', "loc": "a good code LM still ought to reason about execution"}]

    swapped, positions = swapped_locator_quotes(claims, _in_mirror)

    assert positions == (0,)
    assert swapped[0]["quote"] == "a good code LM still ought to reason about execution"
    assert swapped[0]["loc"] == "§7", "the locator loses its wrapping quotes"
    assert swapped[0]["text"] == "LMs should reason.", "every other field is kept"


def test_a_locator_quote_whose_loc_is_not_in_the_mirror_stays() -> None:
    claims = [{"text": "x", "quote": "§7", "loc": "words the mirror does not hold"}]
    swapped, positions = swapped_locator_quotes(claims, _in_mirror)
    assert positions == ()
    assert swapped == claims


def test_a_real_quote_stays_even_when_its_loc_is_in_the_mirror() -> None:
    claims = [{"text": "x", "quote": "a real sentence", "loc": "a good code LM still ought to reason about execution"}]
    assert swapped_locator_quotes(claims, _in_mirror)[1] == ()


def test_a_claim_with_no_string_quote_or_loc_stays() -> None:
    claims = [{"text": "x", "loc": "§7"}, {"text": "y", "quote": None, "loc": "a good code LM still ought to reason about execution"}]
    assert swapped_locator_quotes(claims, _in_mirror) == (claims, ())


def test_only_the_claims_that_qualify_are_swapped_and_their_positions_named() -> None:
    claims = [
        {"text": "a", "quote": "a real sentence", "loc": "§1"},
        {"text": "b", "quote": "§7", "loc": "a good code LM still ought to reason about execution"},
    ]
    swapped, positions = swapped_locator_quotes(claims, _in_mirror)
    assert positions == (1,)
    assert swapped[0] == claims[0]


@pytest.mark.parametrize(("quote", "loc"), [("§5", "5"), ("§4", "4.2"), ("§2", "the")])
def test_a_locator_quote_whose_loc_is_a_short_token_is_not_swapped(quote: str, loc: str) -> None:
    claims = [{"text": "x", "quote": quote, "loc": loc}]
    swapped, positions = swapped_locator_quotes(claims, lambda _text: True)
    assert positions == ()
    assert swapped == claims


def test_a_locator_quote_whose_loc_is_a_passage_of_eleven_words_is_swapped() -> None:
    passage = "one two three four five six seven eight nine ten eleven"
    swapped, positions = swapped_locator_quotes([{"text": "x", "quote": "§8", "loc": passage}], lambda text: text == passage)
    assert positions == (0,)
    assert swapped[0]["quote"] == passage
