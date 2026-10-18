"""Spend counters for `SpendLimits` kept in one file, so every process on one machine shares them and a restart keeps them.

`FileSpendStore` is a `BatchSpendStore`. Every write takes the file's cooperative lock, reloads the
document, applies every entry of the call and writes the whole new document through a temporary file
before the lock is released. A reader always sees a whole document, never part of a write.

Supported for processes on one POSIX device. No claim is made about a network filesystem.
"""

from __future__ import annotations

import asyncio

from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from pydantic_ai_harness.spend import Spent

from core.files import atomic_write, exclusive_lock

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from pydantic_ai_harness.spend import SpendEntry

DEDUP_RETAIN = timedelta(hours=1)
"""How long a store remembers a `SpendEntry.token` it applied, unless it is told otherwise."""

_MONEY_PRECISION = 40
"""The significant digits every sum of dollars is worked out at, whatever the caller's decimal context says."""


class _Counter(BaseModel):
    """One key's totals and the moment it stops counting, if it ever does."""

    usd: Decimal = Decimal(0)
    tokens: int = 0
    requests: int = 0
    unpriced_requests: int = 0
    expires_at: datetime | None = None

    def spent(self) -> Spent:
        """The totals as `SpendLimits` reads them."""
        return Spent(usd=self.usd, tokens=self.tokens, requests=self.requests, unpriced_requests=self.unpriced_requests)


class _Document(BaseModel):
    """The whole file: every live counter, and every applied token by key with the moment it is forgotten."""

    counters: dict[str, _Counter] = Field(default_factory=dict[str, _Counter])
    applied: dict[str, dict[str, datetime]] = Field(default_factory=dict[str, dict[str, datetime]])


class FileSpendStore:
    """`BatchSpendStore` over one JSON file that every process on one machine shares and a restart keeps.

    A counter past its horizon reads as zero. A token already applied to a key within `dedup_retain`
    is not applied again, whichever process applied it first. An entry with no token is always applied.
    Building a store writes nothing. The file and its folder are created by the first write.
    """

    def __init__(self, path: Path, *, clock: Callable[[], datetime], dedup_retain: timedelta = DEDUP_RETAIN) -> None:
        """Keep the counters in `path`, beside a `.lock` file of the same name.

        Args:
            path: The file that holds the counters.
            clock: Supplies the time a horizon and a token's retention are measured against.
            dedup_retain: How long an applied `SpendEntry.token` is remembered. Never longer than
                the counter it was applied to.
        """
        self._path = path
        self._lock = path.with_name(f"{path.name}.lock")
        self._clock = clock
        self._dedup_retain = dedup_retain

    async def get_many(self, keys: Sequence[str]) -> Mapping[str, Spent]:
        """What each key has accumulated, reading a key never written or past its horizon as zero.

        Args:
            keys: The keys to read.

        Returns:
            Each key's totals.

        Raises:
            pydantic.ValidationError: The file holds something this store did not write.
        """
        return await asyncio.to_thread(self._get_many, keys)

    async def add_many(self, entries: Sequence[SpendEntry]) -> Mapping[str, Spent]:
        """Apply every entry together and return each key's new total.

        Either every entry of the call is written or none is.

        Args:
            entries: One response's share of each window, or a correction.

        Returns:
            Each key's total after the call.

        Raises:
            pydantic.ValidationError: The file holds something this store did not write.
        """
        return await asyncio.to_thread(self._add_many, entries)

    def _get_many(self, keys: Sequence[str]) -> Mapping[str, Spent]:
        now = self._clock()
        document = self._read()
        return {key: _live(document, key, now).spent() for key in keys}

    def _add_many(self, entries: Sequence[SpendEntry]) -> Mapping[str, Spent]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(self._lock):
            now = self._clock()
            document = _swept(self._read(), now)
            totals: dict[str, Spent] = {}
            for entry in entries:
                counter = _live(document, entry.key, now)
                applied = document.applied.setdefault(entry.key, {})
                if entry.token is not None and entry.token in applied:
                    totals[entry.key] = counter.spent()
                    continue
                with localcontext(prec=_MONEY_PRECISION):
                    usd = counter.usd + entry.usd
                counter = _Counter(
                    usd=usd,
                    tokens=counter.tokens + entry.tokens,
                    requests=counter.requests + entry.requests,
                    unpriced_requests=counter.unpriced_requests + entry.unpriced,
                    expires_at=None if entry.ttl is None else now + entry.ttl,
                )
                document.counters[entry.key] = counter
                if entry.token is not None:
                    applied[entry.token] = now + self._retention(entry)
                totals[entry.key] = counter.spent()
            document.applied = {key: tokens for key, tokens in document.applied.items() if tokens}
            atomic_write(self._path, document.model_dump_json())
            return totals

    def _read(self) -> _Document:
        """The document on disk, or an empty one when no write has happened yet."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _Document()
        return _Document.model_validate_json(text)

    def _retention(self, entry: SpendEntry) -> timedelta:
        """How long this entry's token is remembered: `dedup_retain`, never past the counter's own horizon."""
        return self._dedup_retain if entry.ttl is None else min(self._dedup_retain, entry.ttl)


def _live(document: _Document, key: str, now: datetime) -> _Counter:
    """The counter at `key`, or an empty one when it was never written or its horizon has passed."""
    counter = document.counters.get(key)
    if counter is None or (counter.expires_at is not None and now >= counter.expires_at):
        return _Counter()
    return counter


def _swept(document: _Document, now: datetime) -> _Document:
    """`document` without the counters past their horizon and the tokens past their retention."""
    return _Document(
        counters={
            key: counter for key, counter in document.counters.items() if counter.expires_at is None or now < counter.expires_at
        },
        applied={
            key: {token: until for token, until in tokens.items() if now < until}
            for key, tokens in document.applied.items()
        },
    )
