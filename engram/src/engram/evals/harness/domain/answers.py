"""The answer rules: how an answer is normalized, and when it matches an expected answer strictly or by word overlap.

No model grades an answer. The strict match compares two normalized answers. The official match is the
benchmark's own word-overlap rule, which credits any shared word.
"""

from __future__ import annotations

import re
import string
import unicodedata

_A_AN_THE = re.compile(r"\b(a|an|the)\b")
_BRACKETED_TEXT = re.compile(r"\[[^\]]*\]|【[^】]*】")


def normalize_answer(text: str) -> str:
    """Drop bracketed text from `text`, lowercase it, delete its punctuation, drop a, an and the, and collapse spaces.

    Bracketed text is anything inside `[` and `]` or inside `【` and `】`, the brackets included. It is replaced by a
    space before punctuation is deleted, so the words inside never reach the normalized answer. Punctuation is every
    ASCII punctuation character, symbols such as `$` and `+` included, and every character Unicode calls punctuation.
    It is deleted rather than replaced, so a hyphenated word joins into one.

    Args:
        text: The answer to normalize.

    Returns:
        The normalized answer.
    """
    lowered = _BRACKETED_TEXT.sub(" ", text).lower()
    without_punctuation = "".join(character for character in lowered if not _is_punctuation(character))
    without_a_an_the = _A_AN_THE.sub(" ", without_punctuation)
    return " ".join(without_a_an_the.split())


def _is_punctuation(character: str) -> bool:
    """Whether `character` is ASCII punctuation or what Unicode calls punctuation."""
    return character in string.punctuation or unicodedata.category(character).startswith("P")


def is_strict_match(answer: str, expected: str) -> bool:
    """Whether `answer` equals `expected` once both are normalized.

    Args:
        answer: The agent's answer.
        expected: The case's expected answer.

    Returns:
        True when the two normalized answers are equal.
    """
    return normalize_answer(answer) == normalize_answer(expected)


def is_official_match(answer: str, expected: str) -> bool:
    """Whether `answer` and `expected` share at least one word, after lowercasing and splitting on whitespace.

    Punctuation stays part of the word it touches, as the benchmark's own rule leaves it.

    Args:
        answer: The agent's answer.
        expected: The case's expected answer.

    Returns:
        True when the two answers share a word.
    """
    return bool(set(answer.lower().split()) & set(expected.lower().split()))
