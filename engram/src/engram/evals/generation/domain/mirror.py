"""The drafting text of a case: the exact joined source text its support quotes were verified against.

`assemble_mirror` builds that text. `mirror_hash` is its `content_hash`, which `accept` records on `QaCaseMeta.mirror_hash`.
`pinpoint_bodies`, `passage_bodies` and `gold_page_bodies` build the source bodies of each archetype. Generation and the
freshness check both use this module, so a case is checked against the same text it was drafted from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.indexing.contracts import content_hash

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def assemble_mirror(source_bodies: Sequence[str]) -> str:
    """Assemble the exact mirror string `accept` verifies support quotes against.

    Args:
        source_bodies: The case's source bodies, in draft order.

    Returns:
        The order-preserving newline join of `source_bodies`.
    """
    return "\n".join(source_bodies)


def mirror_hash(source_bodies: Sequence[str]) -> str:
    """Hash the assembled mirror, the mirror-era staleness signal.

    Args:
        source_bodies: The case's source bodies, in draft order.

    Returns:
        The `content_hash` of the assembled mirror.
    """
    return content_hash(assemble_mirror(source_bodies))


def pinpoint_bodies(text: str, quote: str | None, page_body: str | None) -> tuple[str, ...]:
    """Build a pinpoint case's mirror constituents in claim-then-quote-then-page order.

    The falsy (`None` or empty) values are dropped and the order of the truthy ones is preserved.

    Args:
        text: The claim text.
        quote: The claim's support quote, or `None` when it has none.
        page_body: The owning page's card text, or `None` when it is absent.

    Returns:
        The truthy constituents in order.
    """
    return tuple(value for value in (text, quote, page_body) if value)


def passage_bodies(section_body: str) -> tuple[str, ...]:
    """Build a passage case's mirror constituents: the drawn section's body alone, never its page's card text.

    Args:
        section_body: The section's body.

    Returns:
        The body as the one constituent, or nothing when it is empty.
    """
    return (section_body,) if section_body else ()


def gold_page_bodies(gold_ids: Sequence[str], card_texts: Mapping[str, str]) -> tuple[str, ...]:
    """Build a survey, trace or synthesis case's mirror constituents: the gold pages' card texts in gold-id order.

    Args:
        gold_ids: The case's gold page ids, in draft order.
        card_texts: A page id to card text mapping (ids absent from it are skipped).

    Returns:
        The card texts of the present gold pages, in gold-id order.
    """
    return tuple(card_texts[page_id] for page_id in gold_ids if page_id in card_texts)
