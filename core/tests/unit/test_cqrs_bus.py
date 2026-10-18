"""Unit tests for the CQRS message bus.

The bus dispatches each command to exactly one handler, returns that handler's
result, and hands the events the handler collected to the outbox. Delivery is
the worker's job, so no subscriber runs inside `handle`. An unknown command type
is a clear error, the message base models are frozen.
"""

from __future__ import annotations

import pydantic
import pytest

from core.cqrs.bus import Subscription
from core.cqrs.contracts import Command, Event, MemoryOutbox, MessageBus, UnitOfWork
from core.testing.fakes import FakeUnitOfWork
from tests.support.fakes.bus_spy import BusSpy, RecordingCommandHandler


class _Ping(Command):
    label: str = "ping"


class _OtherCommand(Command):
    pass


class _Pinged(Event):
    label: str = "pinged"


class _Cascaded(Event):
    pass


class _MutatingHandler:
    """Handler that mutates a shared sink, then collects an event."""

    def __init__(self, sink: list[str]) -> None:
        self.events: list[Event] = []
        self.sink = sink

    async def __call__(self, command: Command) -> object:
        del command
        self.sink.append("handler-ran")
        self.events.append(_Pinged())
        return "done"


class TestMessageBus:
    async def test_returns_handler_result(self):
        handler = RecordingCommandHandler(result="ok")
        bus = MessageBus({_Ping: handler}, {})
        assert await bus.handle(_Ping()) == "ok"
        assert handler.handled == [_Ping()]

    async def test_unknown_command_raises(self):
        bus = MessageBus({_Ping: RecordingCommandHandler()}, {})
        with pytest.raises(KeyError, match="_OtherCommand"):
            await bus.handle(_OtherCommand())

    async def test_collected_events_go_to_the_outbox_and_the_collector_is_cleared(self):
        sink: list[str] = []
        handler = _MutatingHandler(sink)
        outbox = MemoryOutbox()
        bus = MessageBus({_Ping: handler}, {}, outbox=outbox)
        await bus.handle(_Ping())
        assert sink == ["handler-ran"]
        assert outbox.appended == [_Pinged()]
        assert handler.events == []

    async def test_no_subscriber_runs_inside_handle(self):
        spy = BusSpy()
        bus = MessageBus({_Ping: _MutatingHandler([])}, {_Pinged: [spy]})
        await bus.handle(_Ping())
        assert spy.received == []

    async def test_the_default_outbox_is_in_memory(self):
        bus = MessageBus({_Ping: _MutatingHandler([])}, {})
        await bus.handle(_Ping())
        assert isinstance(bus.outbox, MemoryOutbox)
        assert len(await bus.outbox.undelivered()) == 1

    def test_subscriptions_are_named_after_the_subscriber_by_default(self):
        spy = BusSpy()
        bus = MessageBus({}, {_Pinged: [spy]})
        assert bus.subscriptions_for(_Pinged) == (Subscription(name="BusSpy", handler=spy),)
        assert bus.subscriptions_for(_Cascaded) == ()

    def test_event_types_are_the_subscribed_wire_names(self):
        bus = MessageBus({}, {_Pinged: [BusSpy()], _Cascaded: [BusSpy()]})
        assert bus.event_types() == {"_Pinged": _Pinged, "_Cascaded": _Cascaded}


class TestLateRegistration:
    async def test_registers_and_routes_a_new_command(self):
        bus = MessageBus({}, {})
        handler = RecordingCommandHandler(result="late")
        bus.register(_Ping, handler)
        assert await bus.handle(_Ping()) == "late"

    async def test_existing_routes_are_preserved(self):
        first = RecordingCommandHandler(result="first")
        bus = MessageBus({_Ping: first}, {})
        bus.register(_OtherCommand, RecordingCommandHandler(result="second"))
        assert await bus.handle(_Ping()) == "first"

    def test_duplicate_route_raises(self):
        bus = MessageBus({_Ping: RecordingCommandHandler()}, {})
        with pytest.raises(ValueError, match="_Ping"):
            bus.register(_Ping, RecordingCommandHandler())

    def test_register_subscriber_names_and_appends(self):
        first, second = BusSpy(), BusSpy()
        bus = MessageBus({}, {_Pinged: [first]})
        bus.register_subscriber(_Pinged, second, name="second-spy")
        assert [s.name for s in bus.subscriptions_for(_Pinged)] == ["BusSpy", "second-spy"]

    def test_register_subscriber_on_a_new_event_type(self):
        bus = MessageBus({}, {})
        spy = BusSpy()
        bus.register_subscriber(_Cascaded, spy)
        assert bus.subscriptions_for(_Cascaded) == (Subscription(name="BusSpy", handler=spy),)
        assert "_Cascaded" in bus.event_types()


class TestFrozenMessages:
    def test_command_is_immutable(self):
        command = _Ping()

        with pytest.raises(pydantic.ValidationError):
            command.label = "mutated"

    def test_event_is_immutable(self):
        event = _Pinged()

        with pytest.raises(pydantic.ValidationError):
            event.label = "mutated"

    def test_events_carry_a_fresh_id_and_a_time(self):
        first, second = _Pinged(), _Pinged()
        assert first.event_id != second.event_id
        assert first.occurred_at.tzinfo is not None

    def test_events_compare_by_content_not_identity(self):
        assert _Pinged() == _Pinged()
        assert _Pinged(label="a") != _Pinged(label="b")
        assert hash(_Pinged()) == hash(_Pinged())

    def test_events_of_different_types_are_never_equal(self):
        assert _Pinged() != _Cascaded()


class TestFakeUnitOfWork:
    """The reusable in-memory Unit of Work fake."""

    def test_satisfies_the_unit_of_work_port(self):
        assert isinstance(FakeUnitOfWork(), UnitOfWork)

    async def test_commit_marks_committed_and_skips_rollback(self):
        uow = FakeUnitOfWork()

        async with uow:
            await uow.commit()

        assert uow.committed is True
        assert uow.rolled_back is False

    async def test_exit_without_commit_rolls_back(self):
        uow = FakeUnitOfWork()

        async with uow:
            pass

        assert uow.committed is False
        assert uow.rolled_back is True

    async def test_a_recorded_event_is_dropped_on_rollback_and_kept_on_commit(self):
        rolled_back = FakeUnitOfWork()
        async with rolled_back:
            rolled_back.record(_Pinged())
        assert rolled_back.coordination.events == []

        committed = FakeUnitOfWork()
        async with committed:
            committed.record(_Pinged())
            await committed.commit()
        assert committed.coordination.events == [_Pinged()]
