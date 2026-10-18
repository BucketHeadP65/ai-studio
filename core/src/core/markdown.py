"""Markdown helpers: frontmatter split and render, heading slugs, and a paragraph split of a section body.

A `---`-fenced YAML block splits from its body and renders back unchanged, with field order kept.
The data must already be YAML-safe, with enums as their values and datetimes as ISO strings.
"""

from __future__ import annotations

import hashlib
import re

from typing import TYPE_CHECKING

import yaml

from text_unidecode import unidecode

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import JsonValue

_NON_SLUG_RUN = re.compile(r"[^a-z0-9]+")
_FENCE = "---\n"
# A frontmatter note splits into three parts on the fence: opening fence, the YAML block, and the body.
_FENCED_PARTS = 3


def split_frontmatter(text: str) -> tuple[dict[str, JsonValue], str]:
    """Split `---`-fenced YAML frontmatter from the Markdown body.

    Args:
        text: The whole note.

    Returns:
        `(data, body)`. When `text` has no leading frontmatter fence, or the YAML is not a mapping,
        `data` is an empty dict and `body` is the original text unchanged.
    """
    if not text.startswith(_FENCE):
        return {}, text
    parts = text.split(_FENCE, 2)
    if len(parts) < _FENCED_PARTS:
        return {}, text
    parsed = yaml.safe_load(parts[1])
    if not isinstance(parsed, dict):
        return {}, text
    return parsed, parts[2]


def render_frontmatter(data: Mapping[str, JsonValue], body: str) -> str:
    """Compose a `---`-fenced YAML frontmatter block followed by the Markdown body.

    Every scalar stays on one line however long it is, so a line-wise reader sees each value whole.

    Args:
        data: The frontmatter fields, YAML-safe, in the order they are written.
        body: The Markdown body.

    Returns:
        The whole note.
    """
    block = yaml.safe_dump(dict(data), sort_keys=False, allow_unicode=True, width=float("inf"))
    return f"{_FENCE}{block}{_FENCE}{body}"


def heading_slug(heading: str) -> str:
    """The ltree-safe slug for a heading, in any script: transliterated, lowercased, never empty.

    `Τιμές` gives `times`, `café` gives `cafe` and `TL;DR` gives `tl_dr`. An ASCII heading keeps its
    letters. A heading with nothing to transliterate, such as pure punctuation or emoji, gives a
    stable `s<hash>` of its text, so distinct headings stay distinct.

    Args:
        heading: The raw heading text.

    Returns:
        A non-empty slug of `[a-z0-9_]`.
    """
    folded: str = unidecode(heading)
    slug = _NON_SLUG_RUN.sub("_", folded.lower()).strip("_")
    if slug:
        return slug
    digest = hashlib.blake2s(heading.encode("utf-8"), digest_size=4).hexdigest()
    return f"s{digest}"


def heading_for_slug(slug: str) -> str:
    """A title-cased heading for a section slug, whose slug is that slug again.

    `findings` gives `Findings` and `open_questions` gives `Open Questions`.

    Args:
        slug: The ltree-safe section slug, lowercase words joined by `_`.

    Returns:
        The heading.
    """
    return slug.replace("_", " ").title()


def split_section_body(body: str, *, token_budget: int, count_tokens: Callable[[str], int]) -> tuple[str, ...]:
    """Split a section body at paragraph boundaries into parts within `token_budget`.

    Paragraphs are gathered greedily, and a blank line inside a code fence never splits. A single
    paragraph over the budget becomes its own part. A body that cannot be divided comes back as one part.

    Args:
        body: The section's markdown body.
        token_budget: The most tokens one part may hold.
        count_tokens: How many tokens a piece of text holds.

    Returns:
        The parts in document order, whitespace-trimmed, at least one.
    """
    paragraphs = _paragraphs(body)
    parts: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for paragraph in paragraphs:
        paragraph_tokens = count_tokens(paragraph)
        if current and current_tokens + paragraph_tokens > token_budget:
            parts.append("\n\n".join(current))
            current = []
            current_tokens = 0
        current.append(paragraph)
        current_tokens += paragraph_tokens
    if current:
        parts.append("\n\n".join(current))
    return tuple(parts) if parts else (body,)


def _paragraphs(body: str) -> list[str]:
    """Split `body` into paragraphs at blank lines outside code fences, dropping empty ones."""
    blocks: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and not line.strip():
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [block for block in blocks if block]
