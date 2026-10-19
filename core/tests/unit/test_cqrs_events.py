"""The topic channel: coalescing on the way out, fan-out on the way in, and the frames it becomes."""

from __future__ import annotations

import asyncio
import json

from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.cqrs.contracts import Broadcaster, Job, TopicPublisher, change_frames
from core.cqrs.events import ChangeNotification, matches
from core.cqrs.infrastructure.broadcaster import SUBSCRIBER_QUEUE_LIMIT, TOPICS_CHANNEL
from tests.support.fakes.topics import FakeListener, FakeNotifier

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest

T0 = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _broadcaster(*, coalesce_seconds: float = 0.1) -> tuple[Broadcaster, FakeNotifier, FakeListener]:
    notifier, listener = FakeNotifier(), FakeListener()
    broadcaster = Broadcaster("postgresql://u:p@h/db", notifier=notifier, listener=listener, coalesce_seconds=coalesce_seconds)
    return broadcaster, notifier, listener


def _job(kind: str, key: str) -> Job:
    return Job(
        id=1,
        kind=kind,
        key=key,
        payload={},
        state="running",
        attempts=1,
        max_attempts=3,
        available_at=T0,
        locked_at=T0,
        locked_by="w1",
        lease_seconds=300,
        last_error=None,
        result=None,
        created_at=T0,
        started_at=T0,
        finished_at=None,
    )


class TestMatches:
    def test_no_filter_takes_everything(self):
        assert matches("run.wf.report", None)

    def test_a_root_matches_everything_under_it(self):
        assert matches("run.wf.report", frozenset({"run"}))
        assert matches("run", frozenset({"run"}))

    def test_a_root_never_matches_a_neighbour_that_starts_the_same(self):
        assert not matches("runs", frozenset({"run"}))
        assert not matches("sessions", frozenset({"session"}))

    def test_any_of_several_wanted_roots_matches(self):
        assert matches("schedules", frozenset({"jobs", "schedules"}))


class TestPublishing:
    async def test_publish_is_synchronous_and_sends_on_the_flush(self):
        broadcaster, notifier, _ = _broadcaster()
        broadcaster.publish("jobs")
        assert notifier.sent == []
        assert await broadcaster.flush() == 1
        assert notifier.sent == [(TOPICS_CHANNEL, "jobs")]

    async def test_the_same_topic_within_one_window_is_sent_once(self):
        broadcaster, notifier, _ = _broadcaster()
        for _ in range(5):
            broadcaster.publish("sessions")
        broadcaster.publish("schedules")
        assert await broadcaster.flush() == 2
        assert notifier.sent == [(TOPICS_CHANNEL, "sessions"), (TOPICS_CHANNEL, "schedules")]

    async def test_a_flushed_topic_is_not_sent_again(self):
        broadcaster, notifier, _ = _broadcaster()
        broadcaster.publish("jobs")
        await broadcaster.flush()
        assert await broadcaster.flush() == 0
        assert len(notifier.sent) == 1

    async def test_a_send_that_fails_is_logged_and_never_reaches_the_caller(self, caplog: pytest.LogCaptureFixture):
        broadcaster, notifier, _ = _broadcaster()
        notifier.fail_on = {"jobs"}
        broadcaster.publish("jobs")
        broadcaster.publish("sessions")
        await broadcaster.flush()
        assert notifier.sent == [(TOPICS_CHANNEL, "sessions")]
        assert "jobs" in caplog.text


class TestFanOut:
    async def test_every_subscriber_hears_every_delivered_topic(self):
        broadcaster, _, _ = _broadcaster()
        first, second = broadcaster.subscribe(), broadcaster.subscribe()
        heard: list[ChangeNotification] = []

        async def read(stream: AsyncIterator[ChangeNotification]) -> None:
            heard.append(await anext(stream))

        readers = [asyncio.create_task(read(first)), asyncio.create_task(read(second))]
        await asyncio.sleep(0)
        broadcaster.deliver("sessions")
        await asyncio.gather(*readers)
        assert [event.topic for event in heard] == ["sessions", "sessions"]
        assert all(event.occurred_at.tzinfo is not None for event in heard)
        await first.aclose()
        await second.aclose()

    async def test_a_dropped_subscriber_stops_receiving(self):
        broadcaster, _, _ = _broadcaster()
        stream = broadcaster.subscribe()
        reader = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        broadcaster.deliver("jobs")
        assert (await reader).topic == "jobs"
        await stream.aclose()
        broadcaster.deliver("jobs")
        assert broadcaster.subscriber_count == 0

    async def test_the_listener_feeds_the_fan_out_once_started(self):
        broadcaster, _, listener = _broadcaster()
        await broadcaster.start()
        stream = broadcaster.subscribe()
        reader = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        listener.arrive("kb.research")
        assert (await asyncio.wait_for(reader, timeout=1)).topic == "kb.research"
        await stream.aclose()
        await broadcaster.stop()
        assert listener.closed


    async def test_a_reader_that_never_reads_keeps_only_the_newest_events(self):
        broadcaster, _, _ = _broadcaster()
        stream = broadcaster.subscribe()
        for index in range(SUBSCRIBER_QUEUE_LIMIT + 5):
            broadcaster.deliver(f"run.{index}")
        heard = [(await anext(stream)).topic for _ in range(SUBSCRIBER_QUEUE_LIMIT)]
        assert heard[0] == "run.5"
        assert heard[-1] == f"run.{SUBSCRIBER_QUEUE_LIMIT + 4}"
        await stream.aclose()


class TestLifecycle:
    async def test_start_opens_the_listener_and_stop_closes_both_connections(self):
        broadcaster, notifier, listener = _broadcaster()
        await broadcaster.start()
        assert listener.opened
        await broadcaster.stop()
        assert listener.closed
        assert notifier.closed

    async def test_the_publisher_task_sends_what_was_queued(self):
        broadcaster, notifier, _ = _broadcaster(coalesce_seconds=0.01)
        await broadcaster.start()
        broadcaster.publish("jobs")
        broadcaster.publish("jobs")
        await asyncio.sleep(0.05)
        await broadcaster.stop()
        assert notifier.sent == [(TOPICS_CHANNEL, "jobs")]


class TestReconnect:
    async def test_a_dropped_connection_reopens_and_keeps_delivering(self):
        broadcaster, _, listener = _broadcaster(coalesce_seconds=0.01)
        listener.fail_next(ConnectionError("listener connection lost"))
        await broadcaster.start()
        stream = broadcaster.subscribe()
        reader = asyncio.create_task(anext(stream))
        listener.arrive("kb.research")
        assert (await asyncio.wait_for(reader, timeout=1)).topic == "kb.research"
        assert listener.open_count == 4
        await stream.aclose()
        await broadcaster.stop()

    async def test_a_long_outage_doubles_the_wait_up_to_the_cap_and_a_reopen_resets_it(self):
        waits: list[float] = []

        async def record(seconds: float) -> None:
            waits.append(seconds)
            await asyncio.sleep(0)

        listener = FakeListener()
        broadcaster = Broadcaster(
            "postgresql://u:p@h/db", notifier=FakeNotifier(), listener=listener, coalesce_seconds=0.01, backoff_cap_seconds=0.08, sleep=record
        )
        await broadcaster.start()
        for _ in range(6):
            listener.fail_next_open(OSError("connection refused"))
        stream = broadcaster.subscribe()
        reader = asyncio.create_task(anext(stream))
        listener.arrive("kb.research")
        assert (await asyncio.wait_for(reader, timeout=1)).topic == "kb.research"
        assert waits == [0.01, 0.02, 0.04, 0.08, 0.08, 0.08]

        listener.fail_next(ConnectionError("listener connection lost"))
        reader = asyncio.create_task(anext(stream))
        listener.arrive("kb.research")
        assert (await asyncio.wait_for(reader, timeout=1)).topic == "kb.research"
        assert waits[-1] == 0.01
        await stream.aclose()
        await broadcaster.stop()

    async def test_a_reopen_that_fails_is_retried_until_it_succeeds(self):
        broadcaster, _, listener = _broadcaster(coalesce_seconds=0.01)
        await broadcaster.start()
        listener.fail_next(ConnectionError("listener connection lost"))
        listener.fail_next_open(OSError("connection refused"))
        stream = broadcaster.subscribe()
        reader = asyncio.create_task(anext(stream))
        listener.arrive("kb.research")
        assert (await asyncio.wait_for(reader, timeout=1)).topic == "kb.research"
        await stream.aclose()
        await broadcaster.stop()


class TestTopicPublisher:
    def test_every_transition_announces_jobs(self):
        broadcaster, _, _ = _broadcaster()
        TopicPublisher(broadcaster).transitioned(_job("anything", "x"))
        assert broadcaster.pending == ["jobs"]

    def test_a_declared_kind_also_announces_its_key_under_the_declared_root(self):
        broadcaster, _, _ = _broadcaster()
        publisher = TopicPublisher(broadcaster, key_topics={"kb.refresh": "knowledge-base"})
        publisher.transitioned(_job("kb.refresh", "research"))
        assert broadcaster.pending == ["jobs", "knowledge-base.research"]

    def test_a_kind_outside_the_mapping_announces_jobs_only(self):
        broadcaster, _, _ = _broadcaster()
        publisher = TopicPublisher(broadcaster, key_topics={"kb.refresh": "knowledge-base"})
        publisher.transitioned(_job("mail.send", "digest"))
        assert broadcaster.pending == ["jobs"]

    def test_a_declared_kind_with_no_key_announces_jobs_only(self):
        broadcaster, _, _ = _broadcaster()
        publisher = TopicPublisher(broadcaster, key_topics={"kb.refresh": "knowledge-base"})
        publisher.transitioned(_job("kb.refresh", ""))
        assert broadcaster.pending == ["jobs"]


class TestChangeFrames:
    async def test_a_matching_topic_becomes_one_change_frame(self):
        broadcaster, _, _ = _broadcaster()
        frames = change_frames(broadcaster, None)
        reader = asyncio.create_task(anext(frames))
        await asyncio.sleep(0)
        broadcaster.deliver("run.wf.report")
        frame = await asyncio.wait_for(reader, timeout=1)
        head, body, blank, tail = frame.split("\n")
        assert head == "event: change"
        assert blank == ""
        assert tail == ""
        assert json.loads(body.removeprefix("data: "))["topic"] == "run.wf.report"
        await frames.aclose()

    async def test_a_topic_outside_the_filter_yields_nothing(self):
        broadcaster, _, _ = _broadcaster()
        frames = change_frames(broadcaster, frozenset({"jobs"}), ping_seconds=0.05)
        reader = asyncio.create_task(anext(frames))
        await asyncio.sleep(0)
        broadcaster.deliver("sessions")
        assert await asyncio.wait_for(reader, timeout=1) == ": ping\n\n"
        await frames.aclose()

    async def test_a_narrow_filter_still_pings_while_it_keeps_dropping_events(self):
        broadcaster, _, _ = _broadcaster()
        frames = change_frames(broadcaster, frozenset({"jobs"}), ping_seconds=0.05)
        reader = asyncio.create_task(anext(frames))
        await asyncio.sleep(0)

        async def keep_dropping() -> None:
            for _ in range(30):
                await asyncio.sleep(0.01)
                broadcaster.deliver("sessions")

        dropper = asyncio.create_task(keep_dropping())
        try:
            assert await asyncio.wait_for(reader, timeout=0.15) == ": ping\n\n"
        finally:
            dropper.cancel()
            with suppress(asyncio.CancelledError):
                await dropper
            await frames.aclose()

    async def test_an_idle_stream_pings(self):
        broadcaster, _, _ = _broadcaster()
        frames = change_frames(broadcaster, None, ping_seconds=0.01)
        assert await asyncio.wait_for(anext(frames), timeout=1) == ": ping\n\n"
        await frames.aclose()

    async def test_closing_the_generator_drops_the_subscriber(self):
        broadcaster, _, _ = _broadcaster()
        frames = change_frames(broadcaster, None, ping_seconds=0.01)
        await anext(frames)
        await frames.aclose()
        assert broadcaster.subscriber_count == 0
