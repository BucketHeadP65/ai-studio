"""The tokens of a statement as sqlparse reads them, and how the name of a call among them is compared."""

from __future__ import annotations

import re

from typing import TYPE_CHECKING, Final, NamedTuple

import sqlparse.lexer

from sqlparse.tokens import Comment, Name, Operator, Punctuation, String, Whitespace, _TokenType

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

    Tokenize = Callable[[str], Iterable[tuple[_TokenType, str]]]

PLAIN_NAME: Final = r"[A-Za-z_][A-Za-z0-9_$]*"
"""The pattern of a name written without quotes: ASCII letters, digits, underscores and dollars, from a letter or an underscore."""

WORD: Final = re.compile(rf'{PLAIN_NAME}|"(?:[^"]|"")*"')
"""A name as the database reads it, written plainly or between double quotes."""


class SqlToken(NamedTuple):
    """One token of a statement, whitespace and comments included, with where it starts in the statement."""

    kind: _TokenType
    value: str
    start: int

    @property
    def end(self) -> int:
        """Where the token ends in the statement, as the first position after it."""
        return self.start + len(self.value)

    @property
    def is_whitespace(self) -> bool:
        """Whether the token is spaces, tabs or line breaks."""
        return self.kind in Whitespace

    @property
    def is_comment(self) -> bool:
        """Whether the token is a comment of any kind, a hint included."""
        return self.kind in Comment

    @property
    def is_code(self) -> bool:
        """Whether the token is neither whitespace nor a comment."""
        return not (self.is_whitespace or self.is_comment)

    @property
    def is_text(self) -> bool:
        """Whether the token is a string written between single quotes."""
        return self.kind in String.Single

    @property
    def is_quoted_name(self) -> bool:
        """Whether the token is a name written between double quotes."""
        return self.kind in String.Symbol

    @property
    def is_word(self) -> bool:
        """Whether the token is a name, quoted or not, or a keyword."""
        return WORD.fullmatch(self.value) is not None

    @property
    def is_name_prefix(self) -> bool:
        """Whether the token is a plain name, which is how a prefix such as `E` or `U` before a quote is read."""
        return self.kind in Name

    def is_symbol(self, value: str) -> bool:
        """Whether the token is the punctuation or the operator `value`.

        Args:
            value: The symbol, such as `(` or `=>`.

        Returns:
            True when the token is that symbol.
        """
        return self.value == value and (self.kind in Punctuation or self.kind in Operator)


def all_tokens(statement: str, tokenize: Tokenize = sqlparse.lexer.tokenize) -> tuple[SqlToken, ...]:
    """Read every token of the statement, whitespace and comments included, in order.

    Args:
        statement: The text to read.
        tokenize: The lexer, sqlparse's own unless a test passes another.

    Returns:
        The tokens, each with the position where it starts in `statement`. Joined, their values are `statement`.

    Raises:
        ValueError: When the lexer lost part of the text, so no position can be trusted.
    """
    tokens = []
    position = 0
    for kind, value in tokenize(statement):
        tokens.append(SqlToken(kind, value, position))
        position += len(value)
    if position != len(statement):
        message = "The lexer dropped part of the statement."
        raise ValueError(message)
    return tuple(tokens)


def code_tokens(statement: str) -> tuple[SqlToken, ...]:
    """Read every token of the statement that is neither whitespace nor a comment, in order.

    Args:
        statement: The text to read.

    Returns:
        The tokens, each with the position where it starts in `statement`.

    Raises:
        ValueError: When the lexer lost part of the text, so no position can be trusted.
    """
    return tuple(token for token in all_tokens(statement) if token.is_code)


def name_of(token: SqlToken) -> str:
    """The name a token holds as the database compares it: without double quotes, and in lower case.

    Args:
        token: A token for which `is_word` is true.

    Returns:
        The name.
    """
    value = token.value
    if token.is_quoted_name:
        value = value.removeprefix('"').removesuffix('"').replace('""', '"')
    return value.lower()


def calls(tokens: Sequence[SqlToken]) -> Iterator[tuple[int, str]]:
    """Find every place where a name stands straight before an opening bracket.

    A schema before the name is dropped, so `pg_catalog.set_config(` and `set_config(` are the same call.

    Args:
        tokens: The tokens of a statement, without whitespace and comments.

    Yields:
        The position of the opening bracket in `tokens`, and the name before it.
    """
    for position in range(1, len(tokens)):
        if tokens[position].is_symbol("(") and tokens[position - 1].is_word:
            yield position, name_of(tokens[position - 1])
