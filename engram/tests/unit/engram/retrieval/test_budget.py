"""One longest-fitting search serves the search, graph and paths answers."""

from engram.retrieval import queries, tools
from engram.retrieval.budget import longest_fitting


def _shorten(text: str, limit: int) -> str:
    return text[:limit]


def test_an_item_that_fits_whole_is_kept_whole() -> None:
    assert longest_fitting("abcdef", shorten=_shorten, fits=lambda text: len(text) <= 10, longest=6) == "abcdef"


def test_the_longest_prefix_that_fits_is_kept() -> None:
    assert longest_fitting("abcdef", shorten=_shorten, fits=lambda text: len(text) <= 4, longest=6) == "abcd"


def test_nothing_is_kept_when_even_the_empty_form_does_not_fit() -> None:
    assert longest_fitting("abcdef", shorten=_shorten, fits=lambda _text: False, longest=6) is None


def test_nothing_is_kept_when_there_is_no_text_to_shorten() -> None:
    assert longest_fitting("", shorten=_shorten, fits=lambda _text: False, longest=0) is None


def test_the_search_and_graph_bounds_share_one_search() -> None:
    assert not hasattr(queries, "_longest_fitting")
    assert tools.longest_fitting is longest_fitting
    assert queries.longest_fitting is longest_fitting
