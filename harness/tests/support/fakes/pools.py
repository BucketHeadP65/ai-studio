"""Database handles that answer scripted rows and record the SQL, instead of opening a connection."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping, Sequence

    from core.ai.history.contracts import Connection, Database

    from harness.features.sessions.internal_contracts import ProjectionPool


@dataclass
class FakePool:
    """Answers `rows` to every read and keeps every statement it was handed, in order.

    Also its own transaction: `transaction` yields itself, so a multi-statement write inside
    `async with pool.transaction() as conn:` records into the same `statements` a direct
    `fetch`/`execute` call would.
    """

    rows: list[Mapping[str, object]] = field(default_factory=list)
    statements: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)

    async def execute(self, query: str, *args: object) -> str:
        """Record the statement and answer the way asyncpg reports a completed command."""
        self.statements.append((query, args))
        return "OK"

    async def executemany(self, query: str, args: Sequence[Sequence[object]]) -> None:
        """Record one statement per row of `args`."""
        for row in args:
            self.statements.append((query, tuple(row)))

    async def fetch(self, query: str, *args: object) -> list[Mapping[str, object]]:
        """Record the query and answer every scripted row."""
        self.statements.append((query, args))
        return list(self.rows)

    async def fetchrow(self, query: str, *args: object) -> Mapping[str, object] | None:
        """Record the query and answer the first scripted row, `None` when there is none."""
        self.statements.append((query, args))
        return self.rows[0] if self.rows else None

    async def fetchval(self, query: str, *args: object) -> object:
        """Record the query and answer the first column of the first scripted row, `None` when there is none."""
        self.statements.append((query, args))
        return next(iter(self.rows[0].values()), None) if self.rows else None

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[FakePool]:
        """This same pool, standing in for the connection a transaction runs on. Nothing to roll back."""
        yield self

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[FakePool]:
        """This same pool, standing in for a connection lent out of it, so what the record runs is recorded too."""
        yield self

    async def close(self) -> None:
        """Nothing to release. This pool never opened a connection."""


_proof: type[ProjectionPool] = FakePool
"""Pyright fails this module the moment `FakePool` drifts from the `ProjectionPool` port."""

_connection_proof: type[Connection] = FakePool
"""The same for the record's `Connection`: this pool answers as the connection one is acquired from."""

_database_proof: type[Database] = FakePool
"""And for the record's `Database`, which is what holds `acquire` in place: the record's schema is
applied over this pool, so every statement it runs is recorded rather than sent."""


@dataclass(frozen=True)
class FakeSessionsDatabase:
    """The database the projection port is opened over, in memory."""

    pool: FakePool = field(default_factory=FakePool)


@dataclass(frozen=True)
class FakeRecordDatabase:
    """The pool the record lives in, in memory."""

    pool: FakePool = field(default_factory=FakePool)


@dataclass(frozen=True)
class FakeRecordPool:
    """Opens nothing: answers the pool it was built with, and keeps the sessions pool it was asked over."""

    pool: FakePool
    asked_over: list[FakePool] = field(default_factory=list)

    async def __call__(self, sessions: FakePool) -> FakePool:
        """The pool this stands in for, after recording which sessions pool it was opened against."""
        self.asked_over.append(sessions)
        return self.pool


@dataclass(frozen=True)
class FakeScheduleDatabase:
    """Where the standing schedules live, in memory."""

    pool: FakePool = field(default_factory=FakePool)
