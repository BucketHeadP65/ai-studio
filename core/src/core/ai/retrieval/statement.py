"""Check a statement a model wrote before it reaches the database.

The guard is defence in depth. The reading role's privileges and its read-only transactions stay the boundary.
"""

from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING, Final

from .sql_tokens import all_tokens, calls
from .unreadable_forms import UNREADABLE_FORM, unreadable_form

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .sql_tokens import SqlToken


class RefusedStatementError(Exception):
    """A statement the guard does not send. The message says why and what to write instead."""


MAX_STATEMENT_LENGTH: Final = 20_000
"""The most characters a statement may hold."""

MAX_BRACKET_DEPTH: Final = 100
"""The deepest a statement may nest its brackets, round and square together."""

NOT_ONE_STATEMENT: Final = "Send exactly one statement."
"""What the guard says when the text holds no statement or more than one."""

TOO_LONG: Final = f"The statement is longer than {MAX_STATEMENT_LENGTH:,} characters. Send a shorter one."
"""What the guard says when the text is over the size bound."""

TOO_DEEP: Final = f"The statement nests brackets more than {MAX_BRACKET_DEPTH} levels deep. Send a simpler one."
"""What the guard says when the statement nests its brackets too deep."""

UNREADABLE_TEXT: Final = "The statement holds a character the database cannot take. Remove it."
"""What the guard says when the text holds a NUL character or a character that cannot be written as UTF-8."""

RECURSIVE_QUERY: Final = "WITH RECURSIVE is not allowed. Walk links with the api functions that walk the graph."
"""What the guard says when the statement holds `WITH RECURSIVE`."""

FUNCTION_NOT_AVAILABLE: Final = "That function is not available. Query the api views and functions."
"""What the guard says when the statement calls a denied function."""

BACKSLASH_QUOTE: Final = "Write a quote inside a text or a name as two quotes in a row, not as a backslash and a quote."
"""What the guard says when a text or a name holds a backslash straight before a quote."""

UNICODE_ESCAPE: Final = "Write names plainly, without a U& escape."
"""What the guard says when a name is written with a Unicode escape."""

DENIED_FUNCTIONS: Final = frozenset(
    {
        "set_config",
        "pg_advisory_lock",
        "pg_advisory_lock_shared",
        "pg_try_advisory_lock",
        "pg_try_advisory_lock_shared",
        "pg_advisory_xact_lock",
        "pg_advisory_xact_lock_shared",
        "pg_try_advisory_xact_lock",
        "pg_try_advisory_xact_lock_shared",
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "query_to_xml",
        "query_to_xmlschema",
        "query_to_xml_and_xmlschema",
        "cursor_to_xml",
        "cursor_to_xmlschema",
        "table_to_xml",
        "table_to_xmlschema",
        "table_to_xml_and_xmlschema",
        "schema_to_xml",
        "schema_to_xmlschema",
        "schema_to_xml_and_xmlschema",
        "database_to_xml",
        "database_to_xmlschema",
        "database_to_xml_and_xmlschema",
        "pg_notify",
        "pg_cancel_backend",
        "pg_terminate_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
    }
)
"""The functions a statement may not call, named without schema, quotes or case. Every function named `lo_...` is denied too.

They change a setting of the session, take a lock another session waits on, hold a connection doing nothing, run SQL
held in a string, act on other sessions or the server, or read files.
"""

DENIED_PREFIXES: Final = ("lo_",)
"""The starts of the names of denied functions, which are the large object functions."""


def single_statement(sql: str) -> str:
    """The statement in `sql` without comments or a closing semicolon, after checking it is safe to send.

    The text it returns is the text that runs, and the database reads it as the guard did. The statement must be the
    only one, within the size bound, read the same way by sqlparse and the database, free of `WITH RECURSIVE` and free
    of every denied function. Every comment is replaced by whitespace.

    Args:
        sql: The text the model sent.

    Returns:
        The statement, with its comments, surrounding whitespace and one closing semicolon removed.

    Raises:
        RefusedStatementError: When `sql` is over the size bound, holds a character the database cannot take, holds no
            statement or more than one, nests brackets more than 100 levels deep, holds a form the two readers split
            differently, holds `WITH RECURSIVE`, or calls a denied function.
    """
    if len(sql) > MAX_STATEMENT_LENGTH:
        raise RefusedStatementError(TOO_LONG)
    if cannot_be_sent(sql):
        raise RefusedStatementError(UNREADABLE_TEXT)
    tokens = _tokens_of(sql)
    code = [token for token in tokens if token.is_code]
    if not _is_one_statement(code):
        raise RefusedStatementError(NOT_ONE_STATEMENT)
    if _nests_too_deep(code):
        raise RefusedStatementError(TOO_DEEP)
    if _has_backslash_quote(code):
        raise RefusedStatementError(BACKSLASH_QUOTE)
    if (message := unreadable_form(tokens)) is not None:
        raise RefusedStatementError(message)
    if _has_unicode_escaped_name(code):
        raise RefusedStatementError(UNICODE_ESCAPE)
    if _is_recursive(code):
        raise RefusedStatementError(RECURSIVE_QUERY)
    if _calls_denied_function(code):
        raise RefusedStatementError(FUNCTION_NOT_AVAILABLE)
    return _written(tokens, code)


def cannot_be_sent(text: str) -> bool:
    """Whether the text holds a NUL character or a character, such as a lone surrogate, that has no UTF-8 form.

    Args:
        text: The text that would be sent to the database.

    Returns:
        True when the database cannot take the text.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return "\x00" in text


def _tokens_of(sql: str) -> tuple[SqlToken, ...]:
    """Every token of the text, or a refusal when the lexer cannot account for the whole text."""
    try:
        return all_tokens(sql)
    except ValueError:
        raise RefusedStatementError(UNREADABLE_FORM) from None


def _is_one_statement(code: Sequence[SqlToken]) -> bool:
    """Whether the code holds something besides semicolons, and no semicolon but a last one."""
    return any(not token.is_symbol(";") for token in code) and not any(token.is_symbol(";") for token in code[:-1])


def _nests_too_deep(code: Sequence[SqlToken]) -> bool:
    """Whether the brackets nest deeper than the bound, where a closing bracket with nothing open takes no depth away."""
    depth = 0
    for token in code:
        if token.is_symbol("(") or token.is_symbol("["):
            depth += 1
            if depth > MAX_BRACKET_DEPTH:
                return True
        elif token.is_symbol(")") or token.is_symbol("]"):
            depth = max(depth - 1, 0)
    return False


def _written(tokens: Sequence[SqlToken], code: Sequence[SqlToken]) -> str:
    """The tokens as text, each comment as whitespace and the closing semicolon dropped."""
    closing = code[-1].start if code[-1].is_symbol(";") else None
    parts = [_whitespace_for(token) if token.is_comment else token.value for token in tokens if token.start != closing]
    return "".join(parts).strip(" \t\r\n\f")


def _whitespace_for(comment: SqlToken) -> str:
    """The whitespace that stands where the comment stood, a line break when the comment ended its line."""
    return "\n" if comment.value.endswith(("\n", "\r")) else " "


def _is_recursive(tokens: Sequence[SqlToken]) -> bool:
    """Whether the keyword `WITH` stands straight before the keyword `RECURSIVE`, outside strings."""
    words = [token.value.upper() for token in tokens if not token.is_text and not token.is_quoted_name]
    return any(first == "WITH" and second == "RECURSIVE" for first, second in pairwise(words))


def _calls_denied_function(tokens: Sequence[SqlToken]) -> bool:
    """Whether a denied function is called, however its schema, quotes, case and spacing are written."""
    return any(name in DENIED_FUNCTIONS or name.startswith(DENIED_PREFIXES) for _, name in calls(tokens))


def _has_unicode_escaped_name(tokens: Sequence[SqlToken]) -> bool:
    """Whether a quoted name is written `U&"..."`, which spells a function name the deny-list cannot read."""
    return any(
        first.is_name_prefix and first.value.upper() == "U" and second.is_symbol("&") and third.is_quoted_name
        for first, second, third in zip(tokens, tokens[1:], tokens[2:], strict=False)
    )


def _has_backslash_quote(tokens: Sequence[SqlToken]) -> bool:
    """Whether a text or a name holds a backslash straight before a quote."""
    return any((token.is_text and "\\'" in token.value) or (token.is_quoted_name and '\\"' in token.value) for token in tokens)
