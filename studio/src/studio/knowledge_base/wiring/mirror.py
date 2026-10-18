"""What the mirror slice registers: the mirror store over the knowledge base's `Sources/` folder."""

from __future__ import annotations

from engram.sources.contracts import MirrorStore
from engram.sources.infrastructure.mirror import ObsidianMirrorStore
from wireup import injectable

from studio.knowledge_base.settings import KnowledgeBaseSettings


@injectable
def mirror_store_provider(knowledge_base: KnowledgeBaseSettings) -> MirrorStore:
    """Provide the Obsidian mirror store over the knowledge base's `Sources/` folder.

    Args:
        knowledge_base: The knowledge base whose `Sources/` folder the store reads and writes.

    Returns:
        The mirror store.
    """
    return ObsidianMirrorStore(knowledge_base)


REGISTRATIONS: list[object] = [mirror_store_provider]
"""Everything this slice puts in the container, each under the type its consumers declare."""
