"""The four stock `EmbedStrategy` implementations: page-card, section, claim, source-chunk.

Each wraps the matching `card` builder verbatim. `PageCardStrategy`, `SectionStrategy` and
`ClaimStrategy` take a `ResolvedNote`. `SourceChunkStrategy` takes a `MirrorDoc`, since a source
mirror carries no note to resolve.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.indexing.domain import card
from engram.indexing.domain.mirror_chunks import DEFAULT_CHUNK_SIZES, chunk_mirror

from .unit import EmbedStrategyUnit

if TYPE_CHECKING:
    from collections.abc import Iterable

    from engram.indexing.domain.mirror_chunks import ChunkSizes
    from engram.indexing.domain.models import MirrorDoc, ResolvedNote


class PageCardStrategy:
    """One page-level unit per note: `card.card_text`, keyed by the page id."""

    level = "page"

    def __init__(self, card_facets: tuple[str, ...] = ()) -> None:
        """Hold the facets the card prints.

        Args:
            card_facets: The facet keys the card prints, in order. Empty prints no facet line.
        """
        self._card_facets = card_facets

    def units(self, item: ResolvedNote) -> Iterable[EmbedStrategyUnit]:
        """Yield the note's single page-card unit.

        Args:
            item: The resolved note.

        Yields:
            One unit keyed by the page id.
        """
        note = item.note
        yield EmbedStrategyUnit(id=note.frontmatter.id, level=self.level, text=card.card_text(note, card_facets=self._card_facets))


class SectionStrategy:
    """One unit per section: `card.section_embed_input`, keyed `<page-id>#<section-path>`."""

    level = "section"

    def units(self, item: ResolvedNote) -> Iterable[EmbedStrategyUnit]:
        """Yield one unit per section, in the note's section order.

        Args:
            item: The resolved note.

        Yields:
            One unit per section, keyed `<page-id>#<section-path>`.
        """
        note = item.note
        for section in note.sections:
            section_id = f"{note.frontmatter.id}#{section.path}"
            text = card.section_embed_input(note.frontmatter.title, section)
            yield EmbedStrategyUnit(id=section_id, level=self.level, text=text)


class ClaimStrategy:
    """One unit per resolved claim: `card.claim_embed_input`, keyed `<page-id>#<block-id>`."""

    level = "claim"

    def __init__(self, claim_fields: tuple[str, ...] = ()) -> None:
        """Hold the declared claim field order the embed text follows.

        Args:
            claim_fields: The declared claim field names in the schema's order. Empty keeps each claim's own order.
        """
        self._claim_fields = claim_fields

    def units(self, item: ResolvedNote) -> Iterable[EmbedStrategyUnit]:
        """Yield one unit per resolved claim, in `item.claims` order.

        Args:
            item: The resolved note.

        Yields:
            One unit per claim, keyed `<page-id>#<block-id>`, and nothing for a note without claims.
        """
        note = item.note
        for resolved_claim in item.claims:
            claim_id = f"{note.frontmatter.id}#{resolved_claim.block_id}"
            text = card.claim_embed_input(note.frontmatter.title, resolved_claim, claim_fields=self._claim_fields)
            yield EmbedStrategyUnit(id=claim_id, level=self.level, text=text)


class SourceChunkStrategy:
    """One unit per raw chunk of a mirror: `card.raw_chunk_embed_input`, keyed `<source-key>:<ord>`."""

    level = "chunk"

    def __init__(self, chunk_sizes: ChunkSizes = DEFAULT_CHUNK_SIZES) -> None:
        """Hold the chunk sizes the mirror is cut by.

        Args:
            chunk_sizes: How many tokens a chunk aims for and may hold.
        """
        self._chunk_sizes = chunk_sizes

    def units(self, item: MirrorDoc) -> Iterable[EmbedStrategyUnit]:
        """Yield one unit per chunk `chunk_mirror` cuts the mirror's body into under the held chunk sizes.

        Args:
            item: The source mirror.

        Yields:
            One unit per chunk, keyed `<source-key>:<ord>`.
        """
        title = str(item.frontmatter.get("title") or item.source_key)
        for chunk in chunk_mirror(item.text, self._chunk_sizes):
            chunk_id = f"{item.source_key}:{chunk.ord}"
            text = card.raw_chunk_embed_input(title, section_path=chunk.section_path, heading=chunk.heading, body=chunk.body)
            yield EmbedStrategyUnit(id=chunk_id, level=self.level, text=text)
