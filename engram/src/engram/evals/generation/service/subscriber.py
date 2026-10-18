r"""The subscriber that queues an index's changed pages for dataset generation.

`QaGenerationSubscriber` receives `CitingPagesChanged` and, unless auto-generation is `off`, enqueues
its page ids for dataset generation (`studio evals gen-dataset`). It never generates during an index,
and `inline` enqueues exactly as `queue` does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from engram.indexing.contracts import CitingPagesChanged

if TYPE_CHECKING:
    from core.cqrs.contracts import Event

    from engram.evals.generation.internal_contracts import QaGenerationQueue

AutogenMode = Literal["queue", "inline", "off"]
"""How an index treats changed pages: `queue` and `inline` both enqueue them for generation, `off` ignores them."""


class QaGenerationSubscriber:
    """Enqueues an index's changed pages for dataset generation, unless the auto-generation mode is `off`."""

    def __init__(self, queue: QaGenerationQueue, *, autogen: AutogenMode) -> None:
        """Bind the queue and the autogen mode.

        Args:
            queue: The pending-generation queue to enqueue changed pages onto.
            autogen: `"off"` disables auto-generation entirely, `"queue"` / `"inline"` enqueue.
        """
        self._queue = queue
        self._autogen: AutogenMode = autogen

    async def __call__(self, event: Event) -> None:
        """Enqueue the event's page ids unless auto-generation is off or the event carries none.

        Any event other than `CitingPagesChanged` is ignored.

        Args:
            event: The event the bus dispatched.
        """
        if not isinstance(event, CitingPagesChanged):  # pragma: no cover (only subscribed to CitingPagesChanged)
            return
        if self._autogen == "off" or not event.page_ids:
            return
        await self._queue.enqueue(event.page_ids)
