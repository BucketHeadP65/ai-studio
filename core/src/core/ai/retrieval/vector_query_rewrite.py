"""Give each meaning text of a model's statement its vector, so the model never handles a vector.

A model writes `vector_query => '<text>'` inside a call. The rewrite finds each such text by reading the statement's tokens,
so a word inside a string or a comment is never touched, and writes the statement again with `query_vec => '[...]'` in its
place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from .sql_tokens import code_tokens, name_of
from .statement import RefusedStatementError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .sql_tokens import SqlToken

MAX_TEXTS: Final = 4
"""The most meaning texts one statement may hold, each place a text stands counting once."""

MAX_TEXT_LENGTH: Final = 2_000
"""The most characters one meaning text may hold."""

VECTOR_QUERY_FORM: Final = "Pass vector_query by name, as one quoted text."
"""What the rewrite says when a meaning text is written in any form but one plain quoted text after the name `vector_query`."""

NO_QUERY_VEC: Final = "Pass vector_query => '<text>' and the server embeds it. Never pass query_vec or query_model yourself."
"""What the rewrite says when the statement passes `query_vec` or `query_model` by name, which only the rewrite writes."""

TOO_MANY_TEXTS: Final = f"Use at most {MAX_TEXTS} vector_query texts in one statement."
"""What the rewrite says when the statement holds more meaning texts than it may."""

TEXT_TOO_LONG: Final = f"A vector_query text holds at most {MAX_TEXT_LENGTH:,} characters."
"""What the rewrite says when a meaning text is over the length bound."""

VECTOR_COUNT: Final = "Give one vector for each text, in the order of the texts."
"""What the rewrite says when it is given a number of vectors that is not the number of texts."""

_VECTOR_QUERY: Final = "vector_query"
_QUERY_VEC: Final = "query_vec"
_QUERY_MODEL: Final = "query_model"
_ARROW: Final = "=>"


class VectorQueryRewrite:
    """The meaning texts of one statement, and the statement written again with their vectors in place of them.

    Build it with `of`. Identical texts are held once, so each is embedded once.
    """

    texts: tuple[str, ...]
    """Each distinct meaning text, in the order it first appears. The vectors given to `render` follow this order."""

    def __init__(self, statement: str, places: Sequence[tuple[int, int, str]]) -> None:
        """Hold the statement and where each text stands in it. Build the rewrite with `of`.

        Args:
            statement: The text the rewrite writes again.
            places: Each `vector_query` argument as its start, its end and its text, in the order they appear.
        """
        self._statement = statement
        self._places = tuple(places)
        self.texts = tuple(dict.fromkeys(text for _start, _end, text in self._places))

    @classmethod
    def of(cls, statement: str) -> VectorQueryRewrite:
        """Find every meaning text in the statement.

        The statement is the text the guard returned, so the database reads it as the guard did. A text is the plain quoted
        text that is the whole argument after `vector_query =>`.

        Args:
            statement: What the guard returned for the model's statement.

        Returns:
            The rewrite, with no text when the statement holds none.

        Raises:
            RefusedStatementError: When a text is written in any other form, the statement passes `query_vec` or
                `query_model` by name, it holds more than four texts, or one of them holds more than 2,000 characters.
        """
        tokens = code_tokens(statement)
        places: list[tuple[int, int, str]] = []
        for position in range(len(tokens) - 1):
            name = tokens[position]
            if not name.is_word or not tokens[position + 1].is_symbol(_ARROW):
                continue
            if name_of(name) in {_QUERY_VEC, _QUERY_MODEL}:
                raise RefusedStatementError(NO_QUERY_VEC)
            if name_of(name) == _VECTOR_QUERY:
                text = _plain_text_after(tokens, position + 1)
                if text is None:
                    raise RefusedStatementError(VECTOR_QUERY_FORM)
                places.append((name.start, text.end, _text_of(text)))
        if len(places) > MAX_TEXTS:
            raise RefusedStatementError(TOO_MANY_TEXTS)
        if any(len(text) > MAX_TEXT_LENGTH for _start, _end, text in places):
            raise RefusedStatementError(TEXT_TOO_LONG)
        return cls(statement, places)

    def render(self, vectors: Sequence[Sequence[float]] | None, vector_type: str = "halfvec", model: str | None = None) -> str:
        """Write the statement with each meaning text replaced by its vector, and the model that embedded it.

        Args:
            vectors: One vector for each of `texts`, in the same order, or None when the texts could not be embedded. Without
                vectors each argument becomes `query_vec => NULL`, so the search runs its keyword legs alone.
            vector_type: The SQL type the vector is cast to. The caller takes it from the search schema, never from the model.
            model: The embedding model the vectors come from, written as `query_model` beside each vector so the search
                compares them only with that model's. None writes no `query_model`.

        Returns:
            The statement.

        Raises:
            ValueError: When `vectors` is not None and holds a different number of vectors than `texts`.
        """
        if vectors is not None and len(vectors) != len(self.texts):
            raise ValueError(VECTOR_COUNT)
        parts = []
        position = 0
        for start, end, text in self._places:
            parts.append(self._statement[position:start])
            if vectors is None:
                parts.append(f"{_QUERY_VEC} => NULL")
            else:
                named = "" if model is None else f", {_QUERY_MODEL} => '{model.replace("'", "''")}'"
                parts.append(f"{_QUERY_VEC} => '{vector_text(vectors[self.texts.index(text)])}'::{vector_type}{named}")
            position = end
        parts.append(self._statement[position:])
        return "".join(parts)


def _plain_text_after(tokens: Sequence[SqlToken], arrow: int) -> SqlToken | None:
    """The single-quoted text that is the whole argument after the arrow, or None when the argument is anything else."""
    value, after = tokens[arrow + 1 : arrow + 2], tokens[arrow + 2 : arrow + 3]
    if value and value[0].is_text and after and (after[0].is_symbol(",") or after[0].is_symbol(")")):
        return value[0]
    return None


def _text_of(token: SqlToken) -> str:
    """The characters of a single-quoted text, without its quotes and with each doubled quote as one."""
    return token.value[1:-1].replace("''", "'")


def vector_text(vector: Sequence[float]) -> str:
    """Write a vector as the database reads one: square brackets around comma-separated numbers.

    Args:
        vector: The numbers of the vector.

    Returns:
        The text, such as `[0.5,1.0]`.
    """
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"
