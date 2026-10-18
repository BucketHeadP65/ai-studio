r"""The primitives every bounded read tool shares: one text clip, one fit check, one last repair.

A bounded tool answers with the largest complete answer that fits a character budget. Each tool
keeps its own accumulate loop, because the answer shapes differ. This module holds what every one
of them needs: shortening display text at a code-point boundary with a visible ellipsis, measuring
a complete answer one way, and swapping an oversized next-move block for a short fixed one as the
last step before an answer leaves.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, Self

from pydantic_core import to_json

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.ai.retrieval.contracts import NextSteps
    from pydantic import JsonValue

__all__ = ["BoundedAnswer", "clip_text", "fits_budget", "longest_fitting", "with_fallback_steps"]


class BoundedAnswer(Protocol):
    """The slice of a result model the budget drives: render the whole answer, or copy it with one field replaced."""

    def model_dump_json(self) -> str:
        """Return this answer's complete JSON serialization, every field, nothing elided.

        Returns:
            The JSON text.
        """
        ...

    def model_copy(self, *, update: Mapping[str, NextSteps] | None = None) -> Self:
        """Return this answer with `update`'s fields replaced.

        Args:
            update: The fields to replace, by name, or None for none.

        Returns:
            The copy.
        """
        ...


def clip_text(text: str | None, limit: int) -> str | None:
    """`text` cut to at most `limit` characters, ending in an ellipsis when anything was removed.

    Python strings are sequences of code points, so a slice never splits one: a cut of multibyte
    text stays valid text. `None` and text already within `limit` pass through untouched.

    Args:
        text: The display text to bound, or `None`.
        limit: The longest result in characters. `0` empties the text entirely.

    Returns:
        The bounded text, or `None` when `text` was `None`.
    """
    if text is None or len(text) <= limit:
        return text
    return text[: limit - 1] + "…" if limit > 0 else ""


def fits_budget(answer: BoundedAnswer | Mapping[str, JsonValue], max_chars: int) -> bool:
    """Whether `answer`'s complete serialization stays within `max_chars`.

    It measures the whole answer, never the items alone, as compact JSON with non-ASCII text
    unescaped. A mapping holding a date, a numeric or a UUID is measured rather than raising.

    Args:
        answer: The complete answer: a result model, or the mapping a tool serializes directly.
        max_chars: The longest serialization to answer with.

    Returns:
        Whether the answer fits.
    """
    rendered = to_json(answer).decode() if isinstance(answer, Mapping) else answer.model_dump_json()
    return len(rendered) <= max_chars


def with_fallback_steps[AnswerT: BoundedAnswer](answer: AnswerT, max_chars: int, *, steps: NextSteps) -> AnswerT:
    """`answer` when it already fits `max_chars`, else the same answer carrying `steps` instead of its own.

    Call it as the last check before any answer leaves, an empty answer and a budget refusal
    included. The whole next-move block is replaced, never clipped.

    Args:
        answer: The complete answer, already accumulated.
        max_chars: The longest serialization to answer with.
        steps: The short fixed next-move block to fall back to.

    Returns:
        The answer that goes out, within budget.
    """
    if fits_budget(answer, max_chars):
        return answer
    return answer.model_copy(update={"next_steps": steps})


def longest_fitting[ItemT](item: ItemT, *, shorten: Callable[[ItemT, int], ItemT], fits: Callable[[ItemT], bool], longest: int) -> ItemT | None:
    """The longest form of `item` that still fits, or `None` when even its display-free form does not.

    Whole first: an item that fits untouched is kept untouched. Otherwise a bounded binary search
    over the display-text length finds the longest prefix that still fits, since serialized length
    grows with that length.

    Args:
        item: The item to keep, whole or shortened.
        shorten: Returns `item` with its display text cut to the given length.
        fits: Whether a form of the item keeps the whole answer within its budget.
        longest: The length of the item's longest display text.

    Returns:
        The longest form that fits, or `None` when none does.
    """
    if fits(item):
        return item
    if longest == 0 or not fits(shorten(item, 0)):
        return None
    low, high = 0, longest  # low always fits and high never does. The loop narrows to the longest that fits.
    while high - low > 1:
        middle = (low + high) // 2
        if fits(shorten(item, middle)):
            low = middle
        else:
            high = middle
    return shorten(item, low)
