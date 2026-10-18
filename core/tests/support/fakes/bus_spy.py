"""Event-recording helpers for exercising the message bus.

`BusSpy` is a `core.cqrs.bus.EventSubscriber` that appends every event it
receives to a public `received` list, so a test can assert the sequence of
events a bus dispatched. `RecordingCommandHandler` is a minimal
`core.cqrs.bus.CommandHandler`: it carries the bus-injected `events`
collector, records the commands it handled, and returns a scripted result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.cqrs.contracts import Command, Event


class BusSpy:
    """Subscriber that records the events the bus dispatches to it."""

    def __init__(self) -> None:
        self.received: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.received.append(event)


class RecordingCommandHandler:
    """Command handler that records calls and returns a scripted result.

    Mirrors the Cosmic Python handler shape: the bus injects the `events`
    collector before dispatch, the handler appends domain events to it during
    handling, and the bus hands them to the outbox afterwards.
    """

    def __init__(self, result: object = None) -> None:
        self.events: list[Event] = []
        self.result = result
        self.handled: list[Command] = []

    async def __call__(self, command: Command) -> object:
        self.handled.append(command)
        return self.result
