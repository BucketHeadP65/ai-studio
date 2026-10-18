"""Wrap, unwrap and split a claim's `quote::` value.

A `quote::` value is written on disk as `"verbatim snippet"` and stored as the bare snippet.
`quote` wraps a stored value for disk and `unquote` is its exact inverse, so rendering a parsed note is byte-stable.
`split_snippets` breaks a value into the `"..."` snippets the quote check looks for in the mirror.
A value that already holds interior quotes, such as `"0.7535" vs "0.7463"`, is never wrapped or unwrapped.
"""

from __future__ import annotations

import re

# The straight ASCII double quote that wraps a stored snippet on disk.
_DOUBLE_QUOTE = '"'

# A `quote:: "snippet"` value needs at least an opening and closing quote to be a wrapped pair.
_QUOTE_PAIR_LEN = 2

# One double-quoted snippet inside a `quote::` value, capturing the bare text between the quotes.
# The body never spans a closing quote, so adjacent snippets stay distinct.
_QUOTED_SNIPPET_RE = re.compile(r'"([^"]*)"')


def quote(value: str) -> str:
    """Re-wrap a quote in double quotes unless it already contains one (inverse of `unquote`).

    Args:
        value: The stored quote value to render for disk.

    Returns:
        The value wrapped in a single pair of double quotes, or verbatim when it already contains one.
    """
    if _DOUBLE_QUOTE in value:
        return value
    return f"{_DOUBLE_QUOTE}{value}{_DOUBLE_QUOTE}"


def unquote(value: str) -> str:
    """Strip a single pair of wrapping double quotes from a `quote::` value.

    The exact inverse of `quote`. A value with interior quotes that is not one balanced wrapped string (e.g. a
    table-derived `"50.4%" vs "49.6%"`) is left verbatim so `split_snippets` can recover each snippet.

    Args:
        value: The raw `quote::` value read from disk.

    Returns:
        The bare snippet when `value` is a single balanced wrapped string, else `value` verbatim.
    """
    inner = value[1:-1]
    if len(value) >= _QUOTE_PAIR_LEN and value.startswith('"') and value.endswith('"') and '"' not in inner:
        return inner
    return value


def split_snippets(value: str) -> tuple[str, ...]:
    """Return the verbatim snippets a `quote::` value asserts against the mirror.

    Each `"..."` in the value is an independent assertion that must be found on its own.

    Args:
        value: The raw `quote::` value to split into snippets.

    Returns:
        The bare text of every `"..."` snippet in `value`, or the whole `value` as a single
        one-tuple when it contains no quoted snippet.
    """
    snippets = tuple(_QUOTED_SNIPPET_RE.findall(value))
    return snippets or (value,)
