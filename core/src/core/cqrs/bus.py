"""In-process CQRS message bus: commands route to one handler, events go to the outbox.

A command is routed to exactly one handler. The events the handler collected on its `events`
list are handed to the outbox after it returns, and the collector is cleared. Nothing is delivered
here: a job runner reads the outbox and calls the subscriptions this bus holds, once per subscriber,
so a subscriber observes committed state and a crash between the handler and the delivery loses
nothing that reached the outbox.

A handler that owns a unit of work should record its events through it instead, so they land in
the same transaction as its change. The `events` list is the path for a handler with no
transaction, and it is at most once across the moment between the handler's return and the
outbox write.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from core.cqrs.memory import MemoryOutbox

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from core.cqrs.domain import Command, Event
    from core.cqrs.internal_contracts import Outbox, TransactionalSubscriber


def narrow_command[CommandT: Command](command: Command, expected: type[CommandT], *, handler: str) -> CommandT:
    """`command` as the type a handler is registered for, or a refusal naming the handler.

    Args:
        command: The command the bus delivered.
        expected: The command type the handler is registered for.
        handler: The handler's name, as the refusal prints it.

    Returns:
        `command`, narrowed to `expected`.

    Raises:
        TypeError: `command` is not an `expected`.
    """
    if not isinstance(command, expected):
        msg = f"{handler} received {type(command).__name__}, expected {expected.__name__}"
        raise TypeError(msg)
    return command


@runtime_checkable
class _EventCollector(Protocol):
    """The collector surface the bus empties into the outbox after dispatch."""

    @property
    def events(self) -> list[Event]:
        """Domain events raised during handling, in emission order."""
        ...


class CommandHandler(_EventCollector, Protocol):
    """Handles exactly one command type and returns its result."""

    async def __call__(self, command: Command) -> object:
        """Execute the command.

        Args:
            command: The command to handle.

        Returns:
            The handler's result.
        """
        ...


class EventSubscriber(Protocol):
    """Reacts to one event type, with no transaction of its own."""

    async def __call__(self, event: Event) -> None:
        """React to the event.

        Args:
            event: The event being delivered.
        """
        ...


@dataclass(frozen=True)
class Subscription:
    """One named listener for one event type. The deliveries are recorded under its name."""

    name: str
    handler: EventSubscriber | TransactionalSubscriber


def _name_of(subscriber: EventSubscriber | TransactionalSubscriber) -> str:
    qualname = getattr(subscriber, "__qualname__", None)
    return qualname if isinstance(qualname, str) else type(subscriber).__qualname__


class MessageBus:
    """Routes commands to handlers, hands their events to the outbox, and holds the subscriptions."""

    def __init__(
        self,
        command_handlers: Mapping[type[Command], CommandHandler],
        event_subscribers: Mapping[type[Event], Sequence[EventSubscriber | TransactionalSubscriber]],
        *,
        outbox: Outbox | None = None,
    ) -> None:
        """Build the bus over its handlers, subscribers and outbox.

        Args:
            command_handlers: One handler per concrete command type.
            event_subscribers: Zero or more subscribers per concrete event type.
            outbox: Where collected events go. An in-memory outbox when omitted, which suits a one-shot process that
                delivers its own events before it exits and every unit test. A long-lived process passes the Postgres
                outbox.
        """
        self._command_handlers: dict[type[Command], CommandHandler] = dict(command_handlers)
        self._subscriptions: dict[type[Event], list[Subscription]] = {
            event_type: [Subscription(_name_of(s), s) for s in subscribers] for event_type, subscribers in event_subscribers.items()
        }
        self.outbox: Outbox = MemoryOutbox() if outbox is None else outbox

    def register(self, command_type: type[Command], handler: CommandHandler) -> None:
        """Additively route `command_type` to `handler` after construction.

        Args:
            command_type: The concrete command type.
            handler: The one handler of that type.

        Raises:
            ValueError: If a handler is already registered for `command_type`.
        """
        if command_type in self._command_handlers:
            msg = f"A handler is already registered for command {command_type.__name__}"
            raise ValueError(msg)
        self._command_handlers[command_type] = handler

    def register_subscriber(
        self, event_type: type[Event], subscriber: EventSubscriber | TransactionalSubscriber, *, name: str | None = None
    ) -> None:
        """Additively subscribe `subscriber` to `event_type`.

        Args:
            event_type: The concrete event type.
            subscriber: What reacts to it.
            name: The name its deliveries are recorded under. The subscriber's qualified name when omitted.
        """
        self._subscriptions.setdefault(event_type, []).append(Subscription(name or _name_of(subscriber), subscriber))

    def subscriptions_for(self, event_type: type[Event]) -> tuple[Subscription, ...]:
        """The subscriptions of `event_type`.

        Args:
            event_type: The concrete event type.

        Returns:
            The subscriptions, in registration order.
        """
        return tuple(self._subscriptions.get(event_type, ()))

    def event_types(self) -> Mapping[str, type[Event]]:
        """Wire name to event class, for every subscribed event type.

        Returns:
            Each event type keyed by its class name.
        """
        return {event_type.__name__: event_type for event_type in self._subscriptions}

    async def handle(self, command: Command) -> object:
        """Dispatch `command` to its handler, then hand the events it collected to the outbox.

        Args:
            command: The command to handle.

        Returns:
            The handler's result.

        Raises:
            KeyError: If no handler is registered for the command's type.
        """
        try:
            handler = self._command_handlers[type(command)]
        except KeyError:
            msg = f"No handler registered for command {type(command).__name__}"
            raise KeyError(msg) from None

        result = await handler(command)
        if isinstance(handler, _EventCollector) and handler.events:
            events = list(handler.events)
            handler.events.clear()
            await self.outbox.append(events)
        return result
