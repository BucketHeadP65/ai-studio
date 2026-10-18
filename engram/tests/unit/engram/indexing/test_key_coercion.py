"""The key-string coercer takes NOT NULL keys, and the frontmatter coercer keeps an absent value absent."""

from engram.indexing.service.row_builder import _as_str, id_str


def test_the_key_coercer_states_it_takes_non_null_keys() -> None:
    assert id_str.__doc__ is not None
    assert "NOT NULL" in id_str.__doc__


def test_the_key_coercer_turns_any_key_into_its_text() -> None:
    assert id_str("page.a") == "page.a"
    assert id_str(7) == "7"


def test_the_frontmatter_coercer_keeps_an_absent_value_absent() -> None:
    assert _as_str(None) is None
    assert _as_str(2024) == "2024"
