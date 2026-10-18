"""The price table refreshes itself for as long as the app runs."""

from __future__ import annotations

from core.observability import start_price_updates

from tests.support.fakes.prices import FakePriceUpdater


def test_start_returns_the_stop() -> None:
    updater = FakePriceUpdater()

    stop = start_price_updates(updater)

    assert updater.started is True
    assert updater.stopped is False
    stop()
    assert updater.stopped is True


def test_a_failing_stop_is_swallowed() -> None:
    updater = FakePriceUpdater()
    updater.failure = RuntimeError("boom")

    stop = start_price_updates(updater)
    stop()

    assert updater.stopped is True
