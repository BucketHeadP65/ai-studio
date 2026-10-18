"""`FileSpendStore`: spend counters every process on one machine shares, which a restart keeps."""

from __future__ import annotations

import asyncio

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic_ai_harness.spend import BatchSpendStore, SpendEntry, Spent

from core.ai.spend_store import FileSpendStore

if TYPE_CHECKING:
    from pathlib import Path

START = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


class _Clock:
    """A clock a test moves by hand."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def _store(path: Path, clock: _Clock | None = None, *, dedup_retain: timedelta = timedelta(hours=1)) -> FileSpendStore:
    return FileSpendStore(path, clock=clock if clock is not None else _Clock(), dedup_retain=dedup_retain)


def test_it_is_a_batch_spend_store(tmp_path: Path) -> None:
    assert isinstance(_store(tmp_path / "spend.json"), BatchSpendStore)


def test_building_one_writes_nothing(tmp_path: Path) -> None:
    _store(tmp_path / "spend" / "counters.json")

    assert list(tmp_path.iterdir()) == []


async def test_a_key_never_written_reads_as_zero(tmp_path: Path) -> None:
    assert await _store(tmp_path / "spend.json").get_many(["day"]) == {"day": Spent()}


async def test_a_fresh_store_over_the_same_file_reads_what_the_last_one_added(tmp_path: Path) -> None:
    path = tmp_path / "spend" / "counters.json"
    await _store(path).add_many([SpendEntry(key="day", usd=Decimal("0.25"), tokens=10, requests=1)])
    await _store(path).add_many([SpendEntry(key="day", usd=Decimal("0.5"), tokens=5, requests=1, unpriced=1)])

    read = await _store(path).get_many(["day"])

    assert read == {"day": Spent(usd=Decimal("0.75"), tokens=15, requests=2, unpriced_requests=1)}


async def test_every_window_of_one_response_is_applied_and_returned(tmp_path: Path) -> None:
    store = _store(tmp_path / "spend.json")

    totals = await store.add_many([SpendEntry(key="run", usd=Decimal(1), requests=1), SpendEntry(key="day", usd=Decimal(1), requests=1)])

    assert totals == {"run": Spent(usd=Decimal(1), requests=1), "day": Spent(usd=Decimal(1), requests=1)}
    assert await store.get_many(["run", "day"]) == totals


async def test_a_replayed_token_is_applied_once_even_by_a_fresh_store(tmp_path: Path) -> None:
    path = tmp_path / "spend.json"
    entry = SpendEntry(key="day", usd=Decimal(1), requests=1, token="response-1")
    await _store(path).add_many([entry])

    totals = await _store(path).add_many([entry])

    assert totals == {"day": Spent(usd=Decimal(1), requests=1)}


async def test_a_token_past_its_retention_is_applied_again(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path / "spend.json", clock, dedup_retain=timedelta(minutes=5))
    entry = SpendEntry(key="day", usd=Decimal(1), requests=1, token="response-1")
    await store.add_many([entry])
    clock.now += timedelta(minutes=6)

    totals = await store.add_many([entry])

    assert totals == {"day": Spent(usd=Decimal(2), requests=2)}


async def test_an_entry_with_no_token_is_applied_every_time(tmp_path: Path) -> None:
    store = _store(tmp_path / "spend.json")
    correction = SpendEntry(key="day", usd=Decimal("-0.5"))
    await store.add_many([SpendEntry(key="day", usd=Decimal(2), requests=1), correction])

    totals = await store.add_many([correction])

    assert totals == {"day": Spent(usd=Decimal(1), requests=1)}


async def test_a_counter_past_its_horizon_reads_as_zero(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path / "spend.json", clock)
    await store.add_many([SpendEntry(key="run", usd=Decimal(1), requests=1, ttl=timedelta(hours=24))])
    clock.now += timedelta(hours=24)

    assert await store.get_many(["run"]) == {"run": Spent()}
    assert await store.add_many([SpendEntry(key="run", usd=Decimal(1), requests=1, ttl=timedelta(hours=24))]) == {
        "run": Spent(usd=Decimal(1), requests=1)
    }


async def test_a_counter_with_no_horizon_is_kept(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path / "spend.json", clock)
    await store.add_many([SpendEntry(key="total", usd=Decimal(1), requests=1)])
    clock.now += timedelta(days=400)

    assert await store.get_many(["total"]) == {"total": Spent(usd=Decimal(1), requests=1)}


async def test_stores_adding_at_once_lose_no_spend(tmp_path: Path) -> None:
    path = tmp_path / "spend.json"
    stores = [_store(path) for _ in range(8)]

    await asyncio.gather(*(store.add_many([SpendEntry(key="day", usd=Decimal("0.1"), requests=1)]) for store in stores for _ in range(5)))

    assert await _store(path).get_many(["day"]) == {"day": Spent(usd=Decimal("4.0"), requests=40)}
