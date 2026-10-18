"""One knowledge base's source queue, claimed by the connectors its schema declares while its sources are on."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from engram.sources.contracts import SourceQueue, claimants_for

if TYPE_CHECKING:
    from pathlib import Path

    from engram.kb.contracts import KnowledgeBaseSchema


SOURCE_QUEUE_PATH = PurePosixPath("_meta") / "source-queue.yaml"
"""The source queue file, relative to a knowledge base's folder."""

QUEUE_CONNECTOR = "intake"
"""The connector that resolves a queued URL, by the name the schema's `sources.connectors` lists it under."""


def source_queue_at(root: Path, schema: KnowledgeBaseSchema | None) -> SourceQueue:
    """The source queue at `<root>/_meta/source-queue.yaml`.

    A knowledge base whose sources are off, or whose schema could not be read, has a queue no connector claims, so every URL
    it takes is an article keyed by the URL itself.

    Args:
        root: The knowledge base's folder.
        schema: Its schema, or `None` when it could not be read.

    Returns:
        The source queue.
    """
    connectors = schema.sources.connectors if schema is not None and schema.sources.enabled else ()
    return SourceQueue(root / SOURCE_QUEUE_PATH, connectors=claimants_for(connectors))
