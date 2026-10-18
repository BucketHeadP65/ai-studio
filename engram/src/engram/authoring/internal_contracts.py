"""The port the authoring slice asks its host to implement."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from engram.authoring.domain import AuthoringKnowledgeBase, ReindexResult


class Reindex(Protocol):
    """Make a written file an indexed page, and say whether the knowledge base as a whole came out clean."""

    async def __call__(self, knowledge_base: AuthoringKnowledgeBase) -> ReindexResult:
        """Reindex `knowledge_base` incrementally, reporting the outcome rather than raising for a failed index.

        Args:
            knowledge_base: The knowledge base to index again.

        Returns:
            Whether the index came out clean, found validation errors on other pages, or did not run.
        """
        ...
