"""The claim quote check: whether a claim's `quote::` appears verbatim in the raw source mirror it cites.

Exactly one transform is applied to both the quote and the mirror before a plain substring test: every maximal
run of Unicode whitespace (ASCII space, tab, newline, CR, FF, VT, U+00A0, U+202F, and every other code point
`str.isspace` accepts) collapses to a single ASCII space, then the ends are stripped.

No other folding is performed. Curly and straight quotes, and dashes and hyphens, never match each other.
Comparison is case-sensitive.
"""

from __future__ import annotations

import re

from dataclasses import dataclass

from engram.kb.domain.quoting import split_snippets

# A run of one or more Unicode whitespace characters, U+00A0 and U+202F included.
_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_whitespace(text: str) -> str:
    """Collapse every Unicode whitespace run to a single space and strip the ends.

    Applying it twice gives the same text as applying it once.

    Args:
        text: The text to normalize.

    Returns:
        The normalized text.
    """
    return _WHITESPACE_RUN.sub(" ", text).strip()


@dataclass(frozen=True, slots=True)
class QuoteMatch:
    """Where a quote was found inside the whitespace-normalized mirror text.

    `start` and `end` index into `normalize_whitespace(mirror_text)`, not the raw mirror.
    `found` is False, with both spans -1, when the quote does not occur.
    """

    found: bool
    start: int = -1
    end: int = -1


def find_quote(quote: str, mirror_text: str) -> QuoteMatch:
    """Locate `quote` within `mirror_text` under whitespace normalization.

    Both sides are whitespace-normalized (see `normalize_whitespace`), then a
    plain substring search runs. An empty quote never matches.

    Args:
        quote: The quote to look for.
        mirror_text: The raw mirror text to search.

    Returns:
        A `QuoteMatch`, `found` is False with sentinel -1 spans when the
        quote does not occur.
    """
    needle = normalize_whitespace(quote)
    if not needle:
        return QuoteMatch(found=False)

    haystack = normalize_whitespace(mirror_text)
    index = haystack.find(needle)
    if index < 0:
        return QuoteMatch(found=False)
    return QuoteMatch(found=True, start=index, end=index + len(needle))


def quote_in_mirror(quote: str, mirror_text: str) -> bool:
    """Return whether `quote` appears in `mirror_text` under whitespace normalization.

    See `find_quote` for the matched span. For a `quote::` value, which may carry
    several snippets, call `quotes_in_mirror` instead.

    Args:
        quote: The single quote to look for.
        mirror_text: The raw mirror text to search.

    Returns:
        True when the quote occurs, False otherwise.
    """
    return find_quote(quote, mirror_text).found


def split_quote_snippets(value: str) -> tuple[str, ...]:
    """Return the verbatim snippets a `quote::` value asserts against the mirror.

    Args:
        value: The raw `quote::` value to split into snippets.

    Returns:
        The bare text of every `"..."` snippet in `value`, or the whole `value` as a single one-tuple when it
        contains no quoted snippet (the already-unwrapped single-quote case, matching `quote_in_mirror`).
    """
    return split_snippets(value)


def quotes_in_mirror(value: str, mirror_text: str) -> bool:
    """Return whether every quoted snippet in `value` appears in `mirror_text`.

    `value` is a raw `quote::` value: one verbatim snippet, or several joined into one
    table-derived value (see `split_quote_snippets`). All snippets must be present, each
    under the whitespace normalization of `quote_in_mirror`. An empty snippet never
    matches, so an empty or whitespace-only value fails the check.

    Args:
        value: The raw `quote::` value.
        mirror_text: The raw mirror text to search.

    Returns:
        True when every snippet occurs, False otherwise.
    """
    return all(quote_in_mirror(snippet, mirror_text) for snippet in split_quote_snippets(value))
