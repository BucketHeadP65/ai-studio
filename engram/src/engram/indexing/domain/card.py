r"""The card / embed-input assembler, pure text assembly + content hashing.

Builds the exact embed-input string for each of the four embedded tables, plus the stable
content hash that gates re-embedding (a unit is re-embedded only when its embed input changes).
Every embedded unit is prefixed with a breadcrumb naming its container.

The contract table:

| Table        | Embed input                                                          |
|--------------|----------------------------------------------------------------------|
| `pages`    | `card_text` = `"{page_type}: {title} — {summary}"` + one facet line  |
|              | (the facets the schema marks `card`)                                 |
| `sections` | `"{page_title} > {heading_path}\n\n{body}"`                        |
| `claims`   | `"{page_title}: {text}"` + compact `[link targets; text fields]`  |
| `raw_chunks`| `"{source_title} > §{section_path} {heading}\n\n{body}"`          |

The page facet line lists the page's values for each facet the schema marks `card`, in the schema's
facet order, joined as a breadcrumb. The claim suffix lists the claim's declared link fields' targets
as written, then its declared text fields, in the schema's declaration order.

No IO.
"""

from __future__ import annotations

import hashlib

from typing import TYPE_CHECKING

import tiktoken

if TYPE_CHECKING:
    from collections.abc import Mapping

    from engram.indexing.domain.models import ResolvedClaim
    from engram.kb.contracts import KnowledgeBaseNote, Section

# The em dash the contract uses to join title and summary in `card_text`.
_EM_DASH = "—"

# The embedding model's tokenizer. `disallowed_special=()` treats every special-token marker as
# ordinary text, so arbitrary mirror text never raises.
_ENCODING = tiktoken.get_encoding("cl100k_base")

# The token ceiling for any embed input, under the embedding model's 8192-token limit. The stored
# row body stays whole. The builders truncate, so the content hash and the embedded text cover the
# same string.
EMBED_TOKEN_BUDGET = 8_000


def token_count(text: str) -> int:
    """Count `text`'s tokens under the indexing slice's `cl100k_base` encoding.

    Every caller that measures stored content counts with this function.

    Args:
        text: The text to count.

    Returns:
        The number of tokens.
    """
    return len(_ENCODING.encode(text, disallowed_special=()))


def truncate_embed_input(text: str) -> str:
    """Cut `text` to at most `EMBED_TOKEN_BUDGET` tokens.

    Text within the budget comes back byte-identical. Longer text comes back as its first
    `EMBED_TOKEN_BUDGET` tokens decoded. The same input always yields the same result.

    Args:
        text: The embed input to cut.

    Returns:
        The text, cut to the budget.
    """
    tokens = _ENCODING.encode(text, disallowed_special=())
    if len(tokens) <= EMBED_TOKEN_BUDGET:
        return text
    return _ENCODING.decode(tokens[:EMBED_TOKEN_BUDGET])


def card_text(note: KnowledgeBaseNote, *, card_facets: tuple[str, ...]) -> str:
    """Assemble `kb.pages.card_text`: the `"{page_type}: {title} — {summary}"` header and a facet line.

    The header omits the `" — {summary}"` part when the page has no summary. The facet line
    is appended on its own line only when the page has at least one value for a facet in `card_facets`.

    Args:
        note: The parsed note.
        card_facets: The facet keys the card prints, in order.

    Returns:
        The card text, cut to the token budget.
    """
    frontmatter = note.frontmatter
    header = f"{frontmatter.type}: {frontmatter.title}"
    if frontmatter.summary:
        header = f"{header} {_EM_DASH} {frontmatter.summary}"

    facet_line = _facet_line(note, card_facets)
    return truncate_embed_input(f"{header}\n{facet_line}" if facet_line else header)


def _facet_line(note: KnowledgeBaseNote, card_facets: tuple[str, ...]) -> str:
    """The one breadcrumb facet line: every value of each facet in `card_facets`, `/`-joined."""
    values: list[str] = []
    for facet in card_facets:
        values.extend(note.frontmatter.facet_values(facet))
    return " / ".join(values)


def section_embed_input(page_title: str, section: Section) -> str:
    r"""Assemble a `kb.sections` embed input: `"{page_title} > {heading_path}\n\n{body}"`.

    Heading sections use their ltree path (`findings` / `approach.graph_indexing`), while a
    preamble uses its reader-facing heading instead of its reserved storage path.

    Args:
        page_title: The title of the page the section belongs to.
        section: The section.

    Returns:
        The embed input, cut to the token budget.
    """
    breadcrumb = section.heading if section.kind == "preamble" else section.path
    return truncate_embed_input(f"{page_title} > {breadcrumb}\n\n{section.body}")


def claim_embed_input(page_title: str, claim: ResolvedClaim, *, claim_fields: tuple[str, ...]) -> str:
    """Assemble a `kb.claims` embed input: `"{page_title}: {text}"` and a field suffix.

    The suffix is a compact `[a; b; c]` tag: the claim's link targets as written, then its non-empty text fields.
    Within each, the fields `claim_fields` names come first in that order, and any other field follows in the
    claim's own order. The suffix is omitted when the claim carries no link target and no text field.

    Args:
        page_title: The title of the page the claim belongs to.
        claim: The resolved claim.
        claim_fields: The declared claim field names in the schema's order. Empty keeps the claim's own order.

    Returns:
        The embed input, cut to the token budget.
    """
    block = claim.block
    head = f"{page_title}: {block.text}"

    parts = [link.target for links in _in_field_order(block.links, claim_fields) for link in links]
    parts.extend(value for value in _in_field_order(block.fields, claim_fields) if value)

    return truncate_embed_input(f"{head} [{'; '.join(parts)}]" if parts else head)


def _in_field_order[T](values: Mapping[str, T], claim_fields: tuple[str, ...]) -> list[T]:
    """The values `claim_fields` names in that order, then every other value in its own order."""
    named = [name for name in claim_fields if name in values]
    named.extend(name for name in values if name not in claim_fields)
    return [values[name] for name in named]


def raw_chunk_embed_input(source_title: str, *, section_path: str | None, heading: str | None, body: str) -> str:
    r"""Assemble a `kb.raw_chunks` embed input: `"{source_title} > §{section_path} {heading}\n\n{body}"`.

    The breadcrumb names the source section. The `§{section_path}` marker is
    dropped when the chunk has no resolved section path (an unnumbered span).

    Args:
        source_title: The mirror's title.
        section_path: The chunk's ltree section path, or `None` when it has none.
        heading: The chunk's heading text, or `None` for the preamble.
        body: The chunk's body text.

    Returns:
        The embed input, cut to the token budget.
    """
    crumbs = [c for c in (f"§{section_path}" if section_path else None, heading) if c]
    breadcrumb = " ".join(crumbs)
    return truncate_embed_input(f"{source_title} > {breadcrumb}\n\n{body}")


def content_hash(text: str) -> str:
    """The stable sha256 hex digest of an embed input, the key that gates re-embedding.

    The same input always yields the same digest. The Load stage stores it as `body_hash` and
    re-embeds only when it changes.

    Args:
        text: The embed input.

    Returns:
        The sha256 hex digest of `text` encoded as UTF-8.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
