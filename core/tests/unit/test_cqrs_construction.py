"""Lock the message bus's full-routing-at-construction invariant.

A `MessageBus` built from a complete `{Command: handler}` map plus its
`{Event: [subscriber]}` map is fully wired the moment construction returns:
every command routes to its handler, every handler-raised event reaches the
outbox, and every constructor-passed subscriber is already registered, with no
post-construction `register` or `register_subscriber` call. This pins the
contract a caller relies on when it builds the bus fully up front rather than
using the late-registration seam. The additive seam itself is exercised
separately in `test_cqrs_bus.py`.
"""

from __future__ import annotations

from core.cqrs.bus import Subscription
from core.cqrs.contracts import Command, Event, MemoryOutbox, MessageBus


class _Compile(Command):
    pass


class _Sync(Command):
    pass


class _Compiled(Event):
    pass


class _FakeHandler:
    """Handler that records its calls, raises a scripted event, and returns a tag.

    Mirrors the bus's handler shape: the public `events` collector carries the
    domain events the bus drains after dispatch, and `handled` records every
    command this handler received so the test can assert routing went here.
    """

    def __init__(self, result: str, raises: Event | None = None) -> None:
        self.events: list[Event] = []
        self.result = result
        self.raises = raises
        self.handled: list[Command] = []

    async def __call__(self, command: Command) -> object:
        self.handled.append(command)
        if self.raises is not None:
            self.events.append(self.raises)
        return self.result


class _FakeSubscriber:
    """Subscriber that records every event a caller delivers to it directly."""

    def __init__(self) -> None:
        self.received: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.received.append(event)


class TestFullyRoutedAtConstruction:
    """A bus built from a complete map needs no post-construction registration."""

    async def test_every_command_routes_to_its_handler_without_register(self):
        compile_handler = _FakeHandler(result="compiled")
        sync_handler = _FakeHandler(result="synced")
        bus = MessageBus(command_handlers={_Compile: compile_handler, _Sync: sync_handler}, event_subscribers={})

        compile_command, sync_command = _Compile(), _Sync()
        compile_result = await bus.handle(compile_command)
        sync_result = await bus.handle(sync_command)

        assert compile_result == "compiled"
        assert sync_result == "synced"
        assert compile_handler.handled == [compile_command]
        assert sync_handler.handled == [sync_command]

    async def test_handler_event_reaches_the_outbox_without_register(self):
        compiled = _Compiled()
        compile_handler = _FakeHandler(result="compiled", raises=compiled)
        sync_handler = _FakeHandler(result="synced")
        subscriber = _FakeSubscriber()
        outbox = MemoryOutbox()
        bus = MessageBus(
            command_handlers={_Compile: compile_handler, _Sync: sync_handler},
            event_subscribers={_Compiled: [subscriber]},
            outbox=outbox,
        )

        await bus.handle(_Compile())
        await bus.handle(_Sync())

        assert outbox.appended == [compiled]
        assert bus.subscriptions_for(_Compiled) == (Subscription(name="_FakeSubscriber", handler=subscriber),)
