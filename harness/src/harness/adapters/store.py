"""The databases this app reaches: the shared settings file's database section, the DSNs derived from it, and the pools opened on them.

Each database has one derivation here, so every caller that reaches it reaches the same place.
The `APP_*READER_URL` exports beat the file wherever it is read.
"""

from __future__ import annotations

import os

from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

from core.db import async_dsn, asyncpg_dsn, open_asyncpg_pool
from core.settings import read_infrastructure

from harness.settings import settings_file

if TYPE_CHECKING:
    from core.settings import DatabaseSettings

KNOWLEDGE_BASE_DB = "knowledge_bases"
STORE_DB = "engram_workflows"
"""The database the library of workflows and steps lives in, beside the schedules."""
SESSIONS_DB = "aifactory_sessions"

KNOWLEDGE_BASE_READER_ENV = "APP_KNOWLEDGE_BASE_READER_URL"
STORE_READER_ENV = "APP_STORE_READER_URL"
"""Full DSNs that override the shared settings file, one per database.

Each must be a whole DSN in libpq's spelling, never host or port parts. `ssl_terms` and
`core.db.async_dsn` translate it for the two clients that read it."""

_READER_OVERRIDES = {KNOWLEDGE_BASE_DB: KNOWLEDGE_BASE_READER_ENV, STORE_DB: STORE_READER_ENV}


@cache
def _database() -> DatabaseSettings:
    """The settings file's `infrastructure.database` section, read once per process and shared by every caller until a restart."""
    return read_infrastructure(settings_file()).database


def reader_override(database: str) -> str | None:
    """The full DSN the environment supplies for `database`, or `None` to fall back to the settings file.

    An empty export counts as absent. A database with no override export, such as the sessions
    database, always answers `None`.

    Args:
        database: One of this module's database names.

    Returns:
        The DSN, or `None` when no export supplies one.
    """
    name = _READER_OVERRIDES.get(database)
    return os.environ.get(name) or None if name else None


def ssl_terms(dsn: str) -> str | bool:
    """The `ssl` argument asyncpg takes for `dsn`, read from its `sslmode`.

    A DSN with no `sslmode` gets `require`. `disable` becomes `False`, and every other libpq mode
    passes through unchanged.

    Args:
        dsn: A DSN in libpq's spelling.

    Returns:
        `require`, `False`, or the DSN's own mode.
    """
    mode = dict(parse_qsl(urlsplit(dsn).query)).get("sslmode")
    if mode is None:
        return "require"
    return False if mode == "disable" else mode


def reader_dsn(database: str) -> str:
    """The read-only DSN for `database`: the environment's override when set, else the settings file's reader URL.

    The file's URL is pointed at `database` and keeps its `sslmode`, the one query term `ssl_terms`
    reads. Every other query term is dropped.

    Args:
        database: One of this module's database names.

    Returns:
        The DSN in libpq's spelling.

    Raises:
        RuntimeError: No override is set and the settings file has no `reader_url`.
    """
    if (override := reader_override(database)) is not None:
        return override
    url = _database().reader_url
    if not url:
        msg = f"no infrastructure.database.reader_url in {settings_file()}"
        raise RuntimeError(msg)
    return _libpq_with_sslmode(url, database)


async def store_connection() -> asyncpg.Pool:
    """Open the reader pool over the library's database. The caller closes it.

    It connects through `reader_dsn`: the `kb_reader` role from the settings file, or whatever role an `APP_STORE_READER_URL`
    export names, which may be a writer.
    Concurrent readers may share it.

    Returns:
        The open pool.

    Raises:
        RuntimeError: No `APP_STORE_READER_URL` override is set and the settings file has no `reader_url`.
    """
    dsn = reader_dsn(STORE_DB)
    return await open_asyncpg_pool(dsn, ssl=ssl_terms(dsn), target=f"to the library {STORE_DB!r}")


def _with_database(url: str, database: str) -> str:
    """The same DSN against a different database, credentials and query preserved."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, ""))




def _libpq_with_sslmode(url: str, database: str) -> str:
    """`url` in libpq's spelling against `database`, keeping `sslmode` as its only query term."""
    parts = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://"))
    mode = dict(parse_qsl(parts.query)).get("sslmode")
    # asyncpg sends any other query term to the server as a run-time setting, which the server refuses.
    query = urlencode({"sslmode": mode}) if mode else ""
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", query, ""))


def store_writer_pool_terms() -> tuple[str, str | bool]:
    """The library database's writer DSN in libpq's spelling, paired with the `ssl` argument raw asyncpg takes.

    The `APP_STORE_READER_URL` export beats the settings file here too. The `ssl` argument is read
    from the URL before any SQLAlchemy respelling, so a URL with no `sslmode` still gets `require`
    and only an explicit `disable` turns TLS off.

    Returns:
        The DSN with its query dropped, and the `ssl` argument for it.
    """
    raw = reader_override(STORE_DB)
    if raw is None:
        raw = _with_database(_database().url, STORE_DB)
    parts = urlsplit(asyncpg_dsn(raw))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")), ssl_terms(raw)


def store_writer_dsn() -> str:
    """The library database's writer DSN in SQLAlchemy's asyncpg spelling, the one credential a save writes with.

    The `APP_STORE_READER_URL` export overrides it too, so a save writes to the database the
    reader reads. A read-only DSN there makes Postgres refuse the write.

    Returns:
        The DSN.
    """
    if (override := reader_override(STORE_DB)) is not None:
        return async_dsn(override)
    return async_dsn(_with_database(_database().url, STORE_DB))


def sessions_writer_dsn() -> str:
    """The sessions database's writer DSN, spelled for raw asyncpg rather than for SQLAlchemy.

    It has no reader role and no environment override. It is the writer URL of the shared settings
    file pointed at `aifactory_sessions`, its scheme and `sslmode` normalised the way `reader_dsn`
    normalises the others.

    Returns:
        The DSN in libpq's spelling.
    """
    return _libpq_with_sslmode(_database().url, SESSIONS_DB)


async def sessions_pool() -> asyncpg.Pool:
    """Open a writer pool over the sessions database, creating the database when it does not exist yet.

    It creates the database only. Its tables come from
    `harness.features.sessions.infrastructure.schema.ensure_schema`.

    Returns:
        The open pool. The caller closes it.

    Raises:
        RuntimeError: asyncpg returned no pool.
    """
    dsn = sessions_writer_dsn()
    target = f"to the sessions projection {SESSIONS_DB!r}"
    try:
        return await open_asyncpg_pool(dsn, ssl=ssl_terms(dsn), target=target)
    except asyncpg.InvalidCatalogNameError:
        admin_dsn = asyncpg_dsn(_database().url)
        admin = await asyncpg.connect(admin_dsn, ssl=ssl_terms(admin_dsn))
        try:
            await admin.execute(f'CREATE DATABASE "{SESSIONS_DB}"')
        finally:
            await admin.close()
        return await open_asyncpg_pool(dsn, ssl=ssl_terms(dsn), target=target)


def knowledge_base_reader_dsn() -> str:
    """The read-only DSN for the knowledge base database, spelled the way engram's own database layer reads it.

    Every reader of that database must take its DSN from here, so all of them read the same one.
    It connects as the reader role, so no tool reaching it can write.

    Returns:
        The DSN.
    """
    # The file is read only when no export overrides it. An eager read fails a checkout that has
    # no settings file even when the export supplies a DSN.
    if (override := reader_override(KNOWLEDGE_BASE_DB)) is None:
        override = _with_database(_database().reader_url, KNOWLEDGE_BASE_DB)
    return async_dsn(override)


KNOWLEDGE_BASE_READER_SECRET_ENV = "APP_INFRASTRUCTURE__DATABASE__READER_SECRET"  # noqa: S105 (the name of an export, not a secret)
"""The export that overrides the settings file's reader secret, under the name the studio reads it by too."""


def knowledge_base_reader_secret() -> str:
    """The secret each private knowledge base's reader password is derived from, or empty when none is set.

    The export wins over the settings file, which is read only when the export is absent. Every reader of the knowledge
    base database must take its secret from here, so a private knowledge base's role is reached the same way everywhere.

    Returns:
        The secret.
    """
    return os.environ.get(KNOWLEDGE_BASE_READER_SECRET_ENV) or _database().reader_secret


def store_admin_dsn() -> str:
    """The knowledge base database's admin DSN, which reaches the tables the `kb_reader` role is not granted.

    `list_undigested` reads `kb.sources_raw` through it. It is the settings file's own database URL
    pointed at the knowledge base database, with no environment override.

    Returns:
        The DSN, in SQLAlchemy's asyncpg spelling.
    """
    return async_dsn(_with_database(_database().url, KNOWLEDGE_BASE_DB))


def bound_databases() -> str:
    """One line naming the database each reader binds, and whether an export or the settings file supplied it.

    Credentials never appear in it, only host, port, database and source.

    Returns:
        The line, one `label=host:port/database (source)` entry per database.
    """
    parts = []
    for label, database, dsn in (("knowledge-base", KNOWLEDGE_BASE_DB, knowledge_base_reader_dsn()), ("store", STORE_DB, reader_dsn(STORE_DB))):
        where = urlsplit(dsn)
        source = "env override" if reader_override(database) else "application_settings.json FALLBACK"
        parts.append(f"{label}={where.hostname}:{where.port or 5432}/{database} ({source})")
    return "; ".join(parts)


def bound_databases_fell_back() -> bool:
    """Whether either reader DSN came from the settings file rather than an explicit export.

    Returns:
        True when either `APP_*READER_URL` export is absent.
    """
    return reader_override(KNOWLEDGE_BASE_DB) is None or reader_override(STORE_DB) is None


@dataclass(frozen=True)
class DatabaseBinding:
    """Which databases this process opens, said once before it opens them."""

    description: str
    """The line `bound_databases` writes."""
    named: bool
    """False when either reader DSN fell back to the settings file."""


@dataclass(frozen=True)
class SessionsDatabase:
    """The pool the session projection reads and writes."""

    pool: asyncpg.Pool


@dataclass(frozen=True)
class ScheduleDatabase:
    """The pool the standing schedules live in."""

    pool: asyncpg.Pool


RECORD_NAMESPACE = "history"
"""The schema the record's tables live in, inside the sessions database.

The session projection's tables stay in `public`. Both sets have a table named `messages`.
"""


@dataclass(frozen=True)
class RecordDatabase:
    """The pool the record's runs, steps and messages live in."""

    pool: asyncpg.Pool
