"""QA-generation routing across every knowledge base this process serves.

Each knowledge base keeps its own pending-generation queue. `KnowledgeBaseQaRouter` reads the collection off an
event, finds the knowledge base that indexes into it, and hands the digest page ids to that knowledge base's queue. An
event whose collection no knowledge base here indexes into is dropped with a log line and never raised, so it
never blocks delivery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.observability import get_logger
from engram.evals.generation.contracts import PendingPagesQueue, QaGenerationSubscriber
from engram.indexing.contracts import CitingPagesChanged
from engram.kb.contracts import load_knowledge_base_schema

from studio.knowledge_base.settings import read_registry

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from core.cqrs.contracts import Event
    from engram.evals.generation.contracts import AutogenMode, QaGenerationQueue

_logger = get_logger(__name__)

type QueueFactory = Callable[[str, Path], QaGenerationQueue]
"""Builds the pending-generation queue for a collection name and the root of the knowledge base that indexes into it."""


def pending_pages_queues(datasets_dir: Path) -> QueueFactory:
    """One JSON-file queue per knowledge base, beside its datasets.

    Args:
        datasets_dir: Where the datasets live. The queues sit in its parent. A relative path is anchored
            inside each knowledge base's own root, and an absolute path is one shared location used as given.

    Returns:
        A factory that builds the queue at `pending_pages.<collection>.json` for a collection and its knowledge base root.
    """

    def factory(collection: str, root: Path) -> QaGenerationQueue:
        anchored = datasets_dir if datasets_dir.is_absolute() else root / datasets_dir
        return PendingPagesQueue(anchored.parent / f"pending_pages.{collection}.json")

    return factory


class KnowledgeBaseQaRouter:
    """Delivers an index's `CitingPagesChanged` to the queue of the knowledge base that indexed it."""

    def __init__(self, *, settings_path: Path, active_knowledge_base: Path | None, queue_for: QueueFactory, autogen: AutogenMode) -> None:
        """Bind the settings file to route by, the knowledge base this process was built for, and the queues.

        Args:
            settings_path: The settings file whose `knowledge_bases` map names the knowledge bases served
                here. It is read again whenever an unknown collection arrives, so a knowledge base
                registered since this process started is routed without a restart.
            active_knowledge_base: The root of the knowledge base this process was built for, routed whether
                or not it is registered. `None` when this process has no knowledge base of its own, so routing
                comes from the registry of knowledge bases alone.
            queue_for: Builds one knowledge base's pending-generation queue from its collection name and root.
            autogen: `"off"` turns auto-generation off, and `"queue"` or `"inline"` enqueue.
        """
        self._settings_path = settings_path
        self._active_knowledge_base = active_knowledge_base
        self._queue_for = queue_for
        self._autogen: AutogenMode = autogen
        self._subscribers: dict[str, QaGenerationSubscriber] = {}

    async def __call__(self, event: Event) -> None:
        """Enqueue a `CitingPagesChanged` event's digest page ids on its knowledge base's queue, or drop it with a log line.

        Args:
            event: The event to route. Any event but `CitingPagesChanged` is ignored.
        """
        if not isinstance(event, CitingPagesChanged):  # pragma: no cover (only subscribed to CitingPagesChanged)
            return
        subscriber = self._subscriber_for(event.collection)
        if subscriber is None:
            _logger.info("no knowledge base served here indexes into collection {collection!r}, dropping its QA generation signal", collection=event.collection)
            return
        await subscriber(event)

    def _subscriber_for(self, collection: str) -> QaGenerationSubscriber | None:
        """The subscriber onto that collection's queue, `None` when no knowledge base here indexes into it."""
        known = self._subscribers.get(collection)
        if known is not None:
            return known
        root = self._roots_by_collection().get(collection)
        if root is None:
            return None
        subscriber = QaGenerationSubscriber(self._queue_for(collection, root), autogen=self._autogen)
        self._subscribers[collection] = subscriber
        return subscriber

    def _roots_by_collection(self) -> dict[str, Path]:
        """Map each collection to its knowledge base root, skipping what does not load, the active knowledge base winning a tie."""
        roots: list[Path] = []
        try:
            registry, _broken = read_registry(self._settings_path)
        except (OSError, ValueError) as exc:
            _logger.warning("QA generation routing could not read the registry at {path}: {reason}", path=str(self._settings_path), reason=str(exc))
        else:
            roots.extend(entry.path for entry in registry.values())
        if self._active_knowledge_base is not None:
            roots.append(self._active_knowledge_base)
        collections: dict[str, Path] = {}
        for root in roots:
            try:
                schema = load_knowledge_base_schema(root)
            except (OSError, ValueError) as exc:
                _logger.warning("QA generation routing skips {root}, its knowledge-base-schema.json did not load: {reason}", root=str(root), reason=str(exc))
                continue
            collections[schema.retrieval.collection] = root
        return collections
