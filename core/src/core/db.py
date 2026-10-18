"""Database helpers: engine and session factories, DSN spellings, insert batches, and Postgres notifications.

This module holds no engine or session factory of its own. Each application builds and shares its own.
`create_tables` takes the metadata of the caller's own `DeclarativeBase`.
"""

from __future__ import annotations

import asyncio

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

from sqlalchemy import NullPool, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Iterator, Mapping, Sequence

    from sqlalchemy import MetaData
    from sqlalchemy.ext.asyncio import AsyncEngine

MAX_BIND_PARAMETERS = 32767
"""asyncpg's ceiling on the arguments one statement may bind. A multi-row INSERT binds rows times columns of them."""


def async_dsn(dsn: str) -> str:
    """A Postgres DSN respelled for the async engine, whatever spelling arrived.

    A `postgresql://` or `postgres://` scheme becomes `postgresql+asyncpg://`. `sslmode=disable` is
    dropped and any other `sslmode` becomes `ssl` with the same mode, so TLS is never dropped. A DSN
    naming another driver or a non-Postgres scheme passes through unchanged.

    Args:
        dsn: The DSN, in libpq or SQLAlchemy spelling.

    Returns:
        The DSN in the async engine's spelling.
    """
    parts = urlsplit(dsn)
    if parts.scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"
    elif parts.scheme == "postgresql+asyncpg":
        scheme = parts.scheme
    else:
        return dsn
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "sslmode"]
    mode = dict(parse_qsl(parts.query)).get("sslmode")
    if mode is not None and mode != "disable":
        query.append(("ssl", mode))
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def build_engine(database_url: str, *, pool_pre_ping: bool = True, pool_recycle: int = 1800) -> AsyncEngine:
    """Create the async SQLAlchemy engine for `database_url`, with a pool that survives dropped idle connections.

    `pool_pre_ping` checks each connection at checkout and replaces one the server has closed.
    `pool_recycle` discards a connection older than that many seconds, so none is handed out past
    the server's idle cutoff. Both do nothing for an in-process SQLite engine.

    Args:
        database_url: The database URL, in any spelling `async_dsn` accepts, such as a libpq
            `postgresql://...?sslmode=require`.
        pool_pre_ping: Revalidate a pooled connection before handing it out.
        pool_recycle: Recycle connections older than this many seconds.

    Returns:
        The configured async engine.
    """
    return create_async_engine(async_dsn(database_url), pool_pre_ping=pool_pre_ping, pool_recycle=pool_recycle)


def build_reader_engine(dsn: str) -> AsyncEngine:
    """The engine for the read-only role over `dsn`, with no connection pool.

    The DSN may be in any spelling `async_dsn` accepts. Each checkout opens a fresh connection, so
    a long-lived process holds no idle reader connection.

    Args:
        dsn: The reader role's DSN.

    Returns:
        The engine.
    """
    return create_async_engine(async_dsn(dsn), poolclass=NullPool)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Wrap an engine in a session factory whose sessions keep their objects loaded after a commit.

    Args:
        engine: The engine the sessions open on.

    Returns:
        The session factory.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_tables(engine: AsyncEngine, metadata: MetaData) -> None:
    """Create every table defined on `metadata` that does not exist yet.

    Args:
        engine: The engine to create the tables through.
        metadata: The metadata of the caller's own `DeclarativeBase`.
    """
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)


def batches[RowT: Mapping[str, object]](rows: Sequence[RowT]) -> Iterator[Sequence[RowT]]:
    """Slices of `rows` small enough that one multi-row INSERT never binds more than `MAX_BIND_PARAMETERS` arguments.

    The slice size comes from the first row's width, so every row must carry the same keys.

    Args:
        rows: The rows of one table.

    Yields:
        Consecutive slices of `rows`, nothing when `rows` is empty.
    """
    if not rows:
        return
    size = max(1, MAX_BIND_PARAMETERS // max(1, len(rows[0])))
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def redact_db(url: str) -> str:
    """A credential-free identifier for a database URL, `host:port/dbname`, safe to log.

    A username or password in `url` never appears in the result.

    Args:
        url: The database URL.

    Returns:
        The host, port and database name. A missing host prints as `?`.
    """
    parsed = make_url(url)
    port = f":{parsed.port}" if parsed.port else ""
    database = f"/{parsed.database}" if parsed.database else ""
    return f"{parsed.host or '?'}{port}{database}"


class SessionScopedFactory[T]:
    """A factory that opens one fresh session per call and yields what `build` makes over it.

    Each call opens `AsyncSession(engine, expire_on_commit=False)` and closes it when the block exits.
    """

    def __init__(self, engine: AsyncEngine, build: Callable[[AsyncSession], T]) -> None:
        """Bind the engine and what to build over each session.

        Args:
            engine: The engine each session is opened on.
            build: Makes the yielded object from the open session.
        """
        self._engine = engine
        self._build = build

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[T]:
        """Open one session and yield what `build` makes over it, closing the session when the block exits.

        Yields:
            What `build` makes over the open session.
        """
        async with AsyncSession(self._engine, expire_on_commit=False) as session:
            yield self._build(session)


def asyncpg_dsn(dsn: str) -> str:
    """The libpq spelling of an async-engine DSN, for a raw asyncpg connection.

    The inverse of `async_dsn`: the `+asyncpg` driver suffix comes off the scheme and an `ssl`
    query key becomes `sslmode`, which is what `asyncpg.connect` reads from a URL. A DSN not in
    the async-engine spelling passes through unchanged.

    Args:
        dsn: The DSN, in the async engine's spelling.

    Returns:
        The DSN in libpq spelling.
    """
    parts = urlsplit(dsn)
    if parts.scheme != "postgresql+asyncpg":
        return dsn
    query = [("sslmode" if key == "ssl" else key, value) for key, value in parse_qsl(parts.query)]
    return urlunsplit(("postgresql", parts.netloc, parts.path, urlencode(query), parts.fragment))


async def open_asyncpg_pool(  # noqa: PLR0913 (the terms of one pool, as asyncpg takes them)
    dsn: str,
    *,
    target: str,
    ssl: str | bool | None = None,
    min_size: int = 1,
    max_size: int = 6,
    server_settings: Mapping[str, str] | None = None,
) -> asyncpg.Pool:
    """Open an asyncpg pool over `dsn`. The caller closes it.

    Args:
        dsn: The DSN, in libpq spelling.
        target: What the pool reaches, as the refusal names it, such as `to the schedule store`.
        ssl: The `ssl` argument asyncpg takes, or `None` for asyncpg's own default.
        min_size: The connections the pool keeps open.
        max_size: The most connections the pool opens.
        server_settings: Settings every connection starts with, such as a `search_path`.

    Returns:
        The open pool.

    Raises:
        RuntimeError: asyncpg returned no pool.
    """
    settings = dict(server_settings) if server_settings is not None else None
    pool = await asyncpg.create_pool(dsn, ssl=ssl, min_size=min_size, max_size=max_size, server_settings=settings)
    if pool is None:  # pragma: no cover (asyncpg returns None only when told not to connect)
        msg = f"could not open a pool {target}"
        raise RuntimeError(msg)
    return pool


class Listener:
    """One dedicated connection listening on one Postgres channel.

    It opens its own asyncpg connection from the libpq spelling of `dsn`. `get` waits for the next
    payload and raises `ConnectionError` once the connection is lost, whether the server dropped it
    or restarted. The caller reopens with `open` and calls `get` again. Close it when done.
    """

    def __init__(self, dsn: str, channel: str) -> None:
        """Hold the connection details. Nothing connects until `open`.

        Args:
            dsn: The database DSN, in either spelling.
            channel: The Postgres channel to listen on.
        """
        self._dsn = asyncpg_dsn(dsn)
        self._channel = channel
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._connection: asyncpg.Connection | None = None

    async def open(self) -> None:
        """Connect and start listening. Calling it twice is harmless."""
        if self._connection is None:
            connection = await asyncpg.connect(self._dsn)
            try:
                await connection.add_listener(self._channel, self._on_notify)
            except Exception:
                await connection.close()
                raise
            connection.add_termination_listener(self._on_terminate)
            self._connection = connection

    def _on_notify(self, _connection: asyncpg.Connection, _pid: int, _channel: str, payload: str) -> None:
        self._queue.put_nowait(payload)

    def _on_terminate(self, _connection: asyncpg.Connection) -> None:
        """Clear the held connection and wake a waiting `get` with the lost-connection signal."""
        self._connection = None
        self._queue.put_nowait(None)

    async def get(self) -> str:
        """The next notification payload, waiting for one.

        Returns:
            The payload.

        Raises:
            ConnectionError: When the connection is lost. Reopen with `open` before calling `get` again.
        """
        await self.open()
        payload = await self._queue.get()
        if payload is None:
            msg = "listener connection lost"
            raise ConnectionError(msg)
        return payload

    def drain(self) -> int:
        """Discard payloads that arrived and were not yet read.

        Returns:
            How many payloads were discarded.
        """
        count = 0
        while not self._queue.empty():
            self._queue.get_nowait()
            count += 1
        return count

    async def close(self) -> None:
        """Stop listening and close the connection."""
        if self._connection is not None:
            self._connection.remove_termination_listener(self._on_terminate)
            await self._connection.remove_listener(self._channel, self._on_notify)
            await self._connection.close()
            self._connection = None


class Notifier:
    """One dedicated connection for sending notifications on any channel.

    The sending half of `Listener`. It holds its own connection, so a send never waits behind other
    work on a pool. It connects on the first send and stays connected. Close it when done.
    """

    def __init__(self, dsn: str) -> None:
        """Hold the connection details. Nothing connects until the first send.

        Args:
            dsn: The database DSN, in either spelling.
        """
        self._dsn = asyncpg_dsn(dsn)
        self._connection: asyncpg.Connection | None = None
        self._lock = asyncio.Lock()

    async def notify(self, channel: str, payload: str) -> None:
        """Send `payload` on `channel`, connecting on the first call.

        Concurrent calls are safe. They take turns on the one connection.

        Args:
            channel: The Postgres channel to send on.
            payload: The notification payload.
        """
        async with self._lock:
            connection = self._connection
            if connection is None:
                connection = await asyncpg.connect(self._dsn)
                self._connection = connection
            await connection.execute("SELECT pg_notify($1, $2)", channel, payload)

    async def close(self) -> None:
        """Close the connection. Calling it twice is harmless."""
        async with self._lock:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None
