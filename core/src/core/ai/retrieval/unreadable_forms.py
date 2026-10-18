"""Find the forms of a statement that sqlparse and the database read differently, so the guard never trusts them."""

from __future__ import annotations

import re

from typing import TYPE_CHECKING, Final

from sqlparse.tokens import Assignment, Comment, Literal, Name, Number, Operator, Punctuation, Wildcard

from .sql_tokens import PLAIN_NAME

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .sql_tokens import SqlToken

NON_ASCII: Final = (
    "Write names with ASCII letters, digits and underscores. Other letters are allowed inside texts and double-quoted names."
)
"""What the guard says when a character outside ASCII stands outside a text, a quoted name and a comment."""

DOLLAR_QUOTE: Final = "Write a text between single quotes. A dollar-quoted text is not available."
"""What the guard says when a text is dollar-quoted."""

UNREADABLE_FORM: Final = (
    "The statement holds a form the guard cannot read the way the database does. "
    "Write plain names, quoted names and texts between single quotes."
)
"""What the guard says when a token is not a plain name, a plain text, a number, an operator or a comment."""

_BREAK: Final = r"[ \t\r\n\f]+"
_WORDS: Final = f"{PLAIN_NAME}(?:{_BREAK}{PLAIN_NAME})*"
_WHITESPACE: Final = re.compile(_BREAK)
_NUMBER: Final = re.compile(r"-?(?:0[xX][0-9A-Fa-f]+|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE]-?[0-9]+)?)")
_SINGLE_QUOTED: Final = re.compile(r"'(?:[^']|'')*'")
_DOUBLE_QUOTED: Final = re.compile(r'"(?:[^"]|"")+"')
_TIME_ZONE: Final = rf"AT{_BREAK}TIME{_BREAK}ZONE{_BREAK}'[A-Za-z0-9_/:+. -]+'"
_WORD_FORMS: Final = re.compile(f"{_WORDS}|{_TIME_ZONE}", re.IGNORECASE)
_KEY_OPERATOR: Final = re.compile(r"\?")
_OPERATORS: Final = re.compile(r"[-+*/<>=~!@%^&|?:;(),.\[\]]+")


def unreadable_form(tokens: Sequence[SqlToken]) -> str | None:
    """Find the first form in the statement that the guard cannot read the way the database does.

    The guard trusts only plain names, texts, quoted names, numbers, operators and comments that sqlparse and the database
    split the same way. Every other form is unreadable, and so is an operator that runs into a comment.

    Args:
        tokens: Every token of the statement, whitespace and comments included.

    Returns:
        What to tell the model about the first unreadable form, or None when every token is readable.
    """
    for token in tokens:
        if (message := _refusal_of(token)) is not None:
            return message
    return UNREADABLE_FORM if _has_hidden_comment_start(tokens) else None


def _refusal_of(token: SqlToken) -> str | None:
    """What to tell the model about the token, or None when the token is readable."""
    if token.is_comment:
        return None if _is_readable_comment(token) else UNREADABLE_FORM
    if token.kind == Literal:
        return DOLLAR_QUOTE
    if not token.is_text and not token.is_quoted_name and not token.value.isascii():
        return NON_ASCII
    return None if _is_readable(token) else UNREADABLE_FORM


def _is_readable_comment(token: SqlToken) -> bool:
    """Whether the comment ends where the database ends it: a line comment, or a block comment that holds no second opening."""
    if token.kind in Comment.Multiline:
        return token.value.startswith("/*") and token.value.endswith("*/") and "/*" not in token.value[2:]
    return token.value.startswith("--")


def _is_readable(token: SqlToken) -> bool:
    """Whether the token is a plain form that sqlparse and the database split the same way."""
    pattern = _literal_pattern(token) or _word_or_sign_pattern(token)
    return pattern is not None and pattern.fullmatch(token.value) is not None


def _literal_pattern(token: SqlToken) -> re.Pattern[str] | None:
    """The only form a whitespace, a text, a quoted name or a number may take, or None for any other token."""
    if token.is_whitespace:
        return _WHITESPACE
    if token.is_text:
        return _SINGLE_QUOTED
    if token.is_quoted_name:
        return _DOUBLE_QUOTED
    return _NUMBER if token.kind in Number else None


def _word_or_sign_pattern(token: SqlToken) -> re.Pattern[str] | None:
    """The only form a word, a placeholder or a sign may take, or None for any other token."""
    if token.kind in Name.Placeholder:
        return _KEY_OPERATOR
    if token.value[:1].isalpha() or token.value[:1] == "_":
        return _WORD_FORMS
    return _OPERATORS if _is_operator_like(token) else None


def _is_operator_like(token: SqlToken) -> bool:
    """Whether sqlparse read the token as an operator, a wildcard, an assignment or punctuation."""
    return any(token.kind in family for family in (Operator, Wildcard, Assignment, Punctuation))


def _has_hidden_comment_start(tokens: Sequence[SqlToken]) -> bool:
    """Whether a run of signs holds `--` or `/*`, which the database reads as a comment even where sqlparse did not."""
    run = ""
    for token in tokens:
        if _is_operator_like(token) or token.kind in Name.Placeholder:
            run += token.value
            continue
        if "--" in run or "/*" in run:
            return True
        run = ""
    return "--" in run or "/*" in run
