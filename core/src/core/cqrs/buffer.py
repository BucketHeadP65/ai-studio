"""What a unit of work holds for the coordination layer until it commits.

A handler records events, and a transactional subscriber's delivery is recorded, through the unit
of work. The unit of work keeps them here and flushes them onto its own session right before it
commits, so the change, its events, and the delivery record land in one transaction or not at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

    from core.cqrs.domain import Event


class CoordinationBuffer:
    """Events and deliveries recorded during one transaction, in order."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.deliveries: list[tuple[UUID, str]] = []

    def record(self, *events: Event) -> None:
        """Queue `events` for the outbox.

        Args:
            *events: The events, in the order they happened.
        """
        self.events.extend(events)

    def record_delivery(self, event_id: UUID, subscriber: str) -> None:
        """Queue one row for the `event_deliveries` table.

        Args:
            event_id: The event's id.
            subscriber: The subscription's name.
        """
        self.deliveries.append((event_id, subscriber))

    def clear(self) -> None:
        """Forget everything, after a flush or a rollback."""
        self.events.clear()
        self.deliveries.clear()
