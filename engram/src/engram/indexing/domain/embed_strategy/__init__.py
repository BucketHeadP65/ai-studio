"""The embedding-composition seam: which text feeds the embedder for each indexed unit.

This package abstracts what gets embedded so a knowledge base can plug in its own text composition
without touching the indexing pipeline (the strategy-registry seam). It holds the
`EmbedStrategy` Protocol (the contract), `EmbedStrategyUnit` (one `(id, level, text)` triple),
the two name-keyed registries (`NoteEmbedStrategies` over a resolved note,
`SourceEmbedStrategies` over a source mirror), the four stock strategies extracted from
today's inline index code (`page-card`, `section`, `claim`, `source-chunk`), and
`active_levels`, which derives a knowledge base's granularity-level vocabulary from its declared
`KnowledgeBaseSchema.active_embed_strategies()` rather than a hardcoded list.

The names below are the package's curated public surface, consumers import from
`engram.indexing.domain.embed_strategy` rather than reaching into the sub-modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.indexing.domain.mirror_chunks import DEFAULT_CHUNK_SIZES

from .registry import NoteEmbedStrategies, SourceEmbedStrategies
from .stock import ClaimStrategy, PageCardStrategy, SectionStrategy, SourceChunkStrategy
from .strategy import EmbedStrategy
from .unit import EmbedStrategyUnit

if TYPE_CHECKING:
    from collections.abc import Mapping

    from engram.indexing.domain.mirror_chunks import ChunkSizes
    from engram.indexing.domain.models import MirrorDoc, ResolvedNote
    from engram.kb.contracts import KnowledgeBaseSchema

__all__ = [
    "ClaimStrategy",
    "EmbedStrategy",
    "EmbedStrategyUnit",
    "NoteEmbedStrategies",
    "PageCardStrategy",
    "SectionStrategy",
    "SourceChunkStrategy",
    "SourceEmbedStrategies",
    "active_levels",
    "build_note_embed_strategies",
    "build_source_embed_strategies",
]


def build_note_embed_strategies(
    overrides: Mapping[str, EmbedStrategy[ResolvedNote]] | None = None,
    *,
    card_facets: tuple[str, ...] = (),
    claim_fields: tuple[str, ...] = (),
) -> NoteEmbedStrategies:
    """Build the note-level embed-strategy registry, extended or overridden per `overrides`.

    Args:
        overrides: Strategy names to add or replace over the stock set (`page-card`, `section`,
            `claim`), the seam a knowledge base with unusual structure plugs its own strategy through
            without editing this package.
        card_facets: The facet keys the page card prints, in order. Empty prints no facet line.
        claim_fields: The declared claim field names in the schema's order, which the claim embed text follows.
            Empty keeps each claim's own order.

    Returns:
        A `NoteEmbedStrategies` registry: the stock strategies merged with `overrides`.
    """
    stock: dict[str, EmbedStrategy[ResolvedNote]] = {
        "page-card": PageCardStrategy(card_facets),
        "section": SectionStrategy(),
        "claim": ClaimStrategy(claim_fields),
    }
    return NoteEmbedStrategies(by_key={**stock, **(overrides or {})})


def build_source_embed_strategies(
    overrides: Mapping[str, EmbedStrategy[MirrorDoc]] | None = None, *, chunk_sizes: ChunkSizes = DEFAULT_CHUNK_SIZES
) -> SourceEmbedStrategies:
    """Build the source-mirror embed-strategy registry, extended or overridden per `overrides`.

    Args:
        overrides: Strategy names to add or replace over the stock set (`source-chunk`).
        chunk_sizes: How many tokens a chunk of a mirror aims for and may hold, which the stock `source-chunk` cuts by.

    Returns:
        A `SourceEmbedStrategies` registry: the stock strategies merged with `overrides`.
    """
    stock: dict[str, EmbedStrategy[MirrorDoc]] = {"source-chunk": SourceChunkStrategy(chunk_sizes)}
    return SourceEmbedStrategies(by_key={**stock, **(overrides or {})})


def active_levels(
    spec: KnowledgeBaseSchema,
    *,
    note_strategies: NoteEmbedStrategies | None = None,
    source_strategies: SourceEmbedStrategies | None = None,
) -> tuple[str, ...]:
    """The granularity levels `spec`'s active embed strategies emit, declared-order, deduplicated.

    Combines `spec.active_embed_strategies()` (which strategy NAMES are active) with each
    named strategy's own `level` attribute (never a hardcoded list here), so a knowledge base's
    granularity-level vocabulary is derived from its declared strategies instead of a module
    constant. A name active in the spec but registered in neither registry is skipped rather
    than raised, since a knowledge base may declare a strategy this index's wiring has not built yet.

    Args:
        spec: The knowledge base spec whose `active_embed_strategies()` names the active strategies.
        note_strategies: The note-level registry to resolve names against, defaults to the
            stock registry.
        source_strategies: The source-mirror registry to resolve names against, defaults to
            the stock registry.

    Returns:
        The distinct `level` values of `spec`'s active strategies, in declaration order.
    """
    notes = note_strategies if note_strategies is not None else build_note_embed_strategies()
    sources = source_strategies if source_strategies is not None else build_source_embed_strategies()
    seen: dict[str, None] = {}
    for name in spec.active_embed_strategies():
        strategy = notes.by_key.get(name) or sources.by_key.get(name)
        if strategy is not None:
            seen.setdefault(strategy.level, None)
    return tuple(seen)
