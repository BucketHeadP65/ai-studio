"""The embed-strategy registries, name-keyed dispatch over `EmbedStrategy` implementations.

Two thin `core.ai.registry.Registry` subclasses (one per indexed-unit shape: a resolved note,
a source mirror), built once by `build_note_embed_strategies` / `build_source_embed_strategies`
and held by the row builder, so adding or overriding a strategy is a mapping edit at the
builder, no edit to the row builder itself.
"""

from __future__ import annotations

from core.ai.registry import Registry

from engram.indexing.domain.models import MirrorDoc, ResolvedNote

from .strategy import EmbedStrategy


class NoteEmbedStrategies(Registry[str, EmbedStrategy[ResolvedNote]]):
    """Singleton bag of note-level `EmbedStrategy` implementations keyed by name."""


class SourceEmbedStrategies(Registry[str, EmbedStrategy[MirrorDoc]]):
    """Singleton bag of source-mirror `EmbedStrategy` implementations keyed by name."""
