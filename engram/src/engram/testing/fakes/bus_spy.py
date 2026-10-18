"""Recording helpers for tests of the message bus.

``BusSpy`` is an event subscriber that appends every event it receives to a public `received`
list. `RecordingCommandHandler` is a minimal command handler. It carries the `events` collector
the bus sets, records the commands it handled and returns a scripted result.
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
        """Record the event.

        Args:
            event: The event the bus dispatched.
        """
        self.received.append(event)


class RecordingCommandHandler:
    """Command handler that records calls and returns a scripted result.

    The bus sets the `events` collector before dispatch and drains it afterwards.
    """

    def __init__(self, result: object = None) -> None:
        """Script the result.

        Args:
            result: What every call returns.
        """
        self.events: list[Event] = []
        self.result = result
        self.handled: list[Command] = []

    async def __call__(self, command: Command) -> object:
        """Record the command and answer the scripted result.

        Args:
            command: The command the bus dispatched.

        Returns:
            The scripted result.
        """
        self.handled.append(command)
        return self.result
