"""The answer rules: normalization, the strict match after it, and the benchmark's own word-overlap match."""

from __future__ import annotations

import pytest

from engram.evals.harness.domain.answers import is_official_match, is_strict_match, normalize_answer


class TestNormalizeAnswer:
    def test_lowercases_and_strips_punctuation(self) -> None:
        assert normalize_answer("Yes.") == "yes"

    def test_drops_the_words_a_an_and_the(self) -> None:
        assert normalize_answer("The fox, an owl and a hare") == "fox owl and hare"

    def test_keeps_a_or_an_or_the_inside_a_longer_word(self) -> None:
        assert normalize_answer("Theodora and Anabel") == "theodora and anabel"

    def test_collapses_spaces(self) -> None:
        assert normalize_answer("  quiet \t river\n bank  ") == "quiet river bank"

    def test_deletes_a_hyphen_rather_than_splitting_on_it(self) -> None:
        assert normalize_answer("Ada Lovelace-King") == "ada lovelaceking"

    def test_strips_ascii_symbols_unicode_does_not_call_punctuation(self) -> None:
        assert normalize_answer("$5 + 3 = 8 ~ x|y") == "5 3 8 xy"

    def test_strips_punctuation_outside_ascii(self) -> None:
        assert normalize_answer("“No” – not at all…") == "no not at all"

    def test_drops_text_in_square_brackets(self) -> None:
        assert normalize_answer("North tower [kb.a; kb.b]") == "north tower"

    def test_drops_text_in_lenticular_brackets(self) -> None:
        assert normalize_answer("Ada Lovelace-King【kb-x:0】") == "ada lovelaceking"

    def test_keeps_the_words_either_side_of_bracketed_text_apart(self) -> None:
        assert normalize_answer("north[kb.a]tower") == "north tower"

    def test_deletes_an_unclosed_bracket_as_punctuation(self) -> None:
        assert normalize_answer("North [tower") == "north tower"


class TestStrictMatch:
    def test_a_trailing_full_stop_and_case_do_not_matter(self) -> None:
        assert is_strict_match("Yes.", "yes")

    def test_a_currency_sign_does_not_matter(self) -> None:
        assert is_strict_match("$1.5 billion", "1.5 billion")

    def test_a_hyphenated_name_matches_its_joined_form(self) -> None:
        assert is_strict_match("Ada Lovelace-King", "ada lovelaceking")

    def test_a_hyphenated_name_does_not_match_its_spaced_form(self) -> None:
        assert not is_strict_match("Ada Lovelace-King", "ada lovelace king")

    def test_a_leading_the_does_not_matter(self) -> None:
        assert is_strict_match("The lighthouse", "lighthouse")

    def test_an_answer_with_more_words_does_not_match(self) -> None:
        assert not is_strict_match("Yes, it did", "Yes")

    @pytest.mark.parametrize(
        ("answer", "expected"),
        [("North tower [kb.a; kb.b]", "North tower"), ("Ada Lovelace-King【kb-x:0】", "Ada Lovelace-King")],
    )
    def test_bracketed_text_does_not_matter(self, answer: str, expected: str) -> None:
        assert is_strict_match(answer, expected)


class TestOfficialMatch:
    def test_one_shared_word_is_enough(self) -> None:
        assert is_official_match("Yes it did", "yes")

    def test_case_does_not_matter(self) -> None:
        assert is_official_match("NORTH tower", "north")

    def test_punctuation_stays_part_of_the_word(self) -> None:
        assert not is_official_match("No, it did not", "no")

    def test_a_shared_the_counts_as_a_shared_word(self) -> None:
        assert is_official_match("the river", "The lake")

    def test_bracketed_text_still_counts_toward_the_overlap(self) -> None:
        assert is_official_match("Unknown [kb.a; north wing]", "north")

    @pytest.mark.parametrize(("answer", "expected"), [("", "yes"), ("north", "south")])
    def test_no_shared_word_is_no_match(self, answer: str, expected: str) -> None:
        assert not is_official_match(answer, expected)
