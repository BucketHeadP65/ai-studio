"""Postgres fixtures for the knowledge base integration and e2e tiers, registered as a pytest plugin.

The session-scoped :func:`pg` fixture starts one container of the selected bed, and the
function-scoped :func:`migrated` fixture gives each test a freshly created, fully migrated
database inside it.

`KNOWLEDGE_BASE_TEST_DIALECT` selects the bed. The default `pg_textsearch` bed runs the prebuilt
`engram-postgres:latest` image (`pg_textsearch` BM25 and Apache AGE) under the
`pg_textsearch` dialect and the `age` graph backend. The opt-in `pg_search` bed runs
`paradedb/paradedb:latest-pg17` under the `pg_search` dialect and the `cte` graph backend.
Both reach the migration through alembic's `config.attributes`, never through an `APP_*` environment variable.

The migrated schema is engram's, read from the `alembic.ini` and `alembic/` of the engram checkout
holding this module. Both fixtures skip the whole tier when Docker is unavailable.
"""

from __future__ import annotations

import asyncio
import os
import uuid

from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
import pytest

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from engram.retrieval.contracts import SqlReaderScopes, private_reader_url

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# The engram checkout root holding the alembic chain, four folders above this module. Resolved at
# import, so a layout change fails here rather than mid-migration.
_ENGRAM_ROOT = Path(__file__).resolve().parents[4]
_ALEMBIC_INI = _ENGRAM_ROOT / "alembic.ini"
_ALEMBIC_DIR = _ENGRAM_ROOT / "alembic"
assert _ALEMBIC_INI.is_file(), f"no alembic.ini at {_ALEMBIC_INI} -- the engram checkout moved relative to this file"
assert _ALEMBIC_DIR.is_dir(), f"no alembic chain at {_ALEMBIC_DIR} -- the engram checkout moved relative to this file"

_PARADEDB_IMAGE = "paradedb/paradedb:latest-pg17"

# The prebuilt local Postgres: postgres:17 with pg_textsearch, Apache AGE, pgvector and
# ltree, and `shared_preload_libraries='age,pg_textsearch'` baked in, so it needs no initdb mount.
_LOCAL_POSTGRES_IMAGE = "engram-postgres:latest"


class _TestDialect(StrEnum):
    """The integration beds `KNOWLEDGE_BASE_TEST_DIALECT` selects between."""

    PG_TEXTSEARCH = "pg_textsearch"
    PG_SEARCH = "pg_search"


# The bed for this run, the analog by default. `KNOWLEDGE_BASE_TEST_DIALECT=pg_search` selects the
# ParadeDB bed. An unknown value raises `ValueError` at collection time.
_TEST_DIALECT = _TestDialect(os.environ.get("KNOWLEDGE_BASE_TEST_DIALECT", _TestDialect.PG_TEXTSEARCH))

# The container image, the BM25 dialect and the graph backend the selected bed migrates and serves under.
_BED_IMAGE = _LOCAL_POSTGRES_IMAGE if _TEST_DIALECT is _TestDialect.PG_TEXTSEARCH else _PARADEDB_IMAGE
_BED_DIALECT = "pg_textsearch" if _TEST_DIALECT is _TestDialect.PG_TEXTSEARCH else "pg_search"
_BED_GRAPH_BACKEND = "age" if _TEST_DIALECT is _TestDialect.PG_TEXTSEARCH else "cte"

# The BM25 extension and the graph backend of the selected bed, for tests whose assertions depend on the bed.
BED_BM25_EXTENSION = "pg_textsearch" if _TEST_DIALECT is _TestDialect.PG_TEXTSEARCH else "pg_search"
BED_GRAPH_BACKEND = _BED_GRAPH_BACKEND

# The read-only login the migrations create.
_READER_ROLE = "kb_reader"


def _reader_password() -> str:
    """The kb_reader dev password that revision 0014 sets when migrations run under these fixtures."""
    return f"{_READER_ROLE}_dev"


def _docker_available() -> bool:
    """Whether a Docker daemon is reachable."""
    try:
        from testcontainers.core.docker_client import DockerClient
    except ImportError:
        return False
    try:
        return bool(DockerClient().client.ping())
    except Exception:  # noqa: BLE001 (any docker or transport failure means no Docker)
        return False


@pytest.fixture(scope="session")
def pg() -> Iterator[str]:
    """Start one container of the selected bed for the session.

    Skips the whole tier when Docker is not running. The user, password and database are all `kb`.

    Yields:
        The container's admin asyncpg URL.
    """
    if not _docker_available():
        pytest.skip("Docker unavailable: skipping the integration tests")

    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(_BED_IMAGE, username="kb", password="kb", dbname="kb", driver="asyncpg")
    container.start()
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture
async def migrated(pg: str) -> str:
    """Create a fresh `test_<uuid>` database in the shared container and migrate it to head.

    Args:
        pg: The container's admin URL.

    Returns:
        The new database's admin asyncpg URL.
    """
    target_url = await new_test_database(pg)
    run_upgrade_head(target_url)
    return target_url


async def new_test_database(pg: str) -> str:
    """Create a fresh, unmigrated `test_<uuid>` database inside the shared bed container.

    Migrate it yourself with :func:`run_upgrade`.

    Args:
        pg: The container's admin URL.

    Returns:
        The new database's admin asyncpg URL.
    """
    db_name = f"test_{uuid.uuid4().hex}"
    admin_dsn = _asyncpg_dsn(pg)

    # CREATE DATABASE cannot run inside a transaction block, and asyncpg autocommits simple execs.
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()

    return _with_database(pg, db_name)


def run_upgrade_head(url: str) -> None:
    """Run `alembic upgrade head` against `url`.

    Args:
        url: The database's admin URL.
    """
    run_upgrade(url, "head")


def run_upgrade(url: str, revision: str) -> None:
    """Run `alembic upgrade <revision>` against `url`.

    Args:
        url: The database's admin URL.
        revision: Any point in the chain, such as `"head"`, a revision id like `"0024"`, or a relative `"+1"`.
    """
    _run_alembic_command(command.upgrade, url, revision)


def run_downgrade(url: str, revision: str) -> None:
    """Run `alembic downgrade <revision>` against `url`.

    Args:
        url: The database's admin URL.
        revision: The revision to downgrade to.
    """
    _run_alembic_command(command.downgrade, url, revision)


def _run_alembic_command(fn: Callable[[Config, str], None], url: str, revision: str) -> None:
    """Run an alembic command synchronously, on a worker thread when this thread has a running loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _run_sync(fn, url, revision)  # no running loop on this thread, so run inline.
        return

    # A loop is running here. The command calls `asyncio.run()`, so run it on a fresh thread and wait.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_run_sync, fn, url, revision).result()


def _run_sync(fn: Callable[[Config, str], None], url: str, revision: str) -> None:
    """Build the bed-matched alembic `Config` and call `fn(config, revision)` on a thread with no running loop."""
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("script_location", str(_ALEMBIC_DIR))
    config.attributes["sqlalchemy_url"] = url
    config.attributes["dialect"] = _BED_DIALECT
    config.attributes["graph_backend"] = _BED_GRAPH_BACKEND
    fn(config, revision)


@pytest.fixture
def reader_url(migrated: str) -> str:
    """The `migrated` database's URL, authenticated as the read-only `kb_reader` role.

    The role reads the `api` schema only, in read-only transactions with a 10 second timeout.

    Args:
        migrated: The migrated database's admin URL.

    Returns:
        The same URL with the reader's credentials.
    """
    return reader_url_for(migrated)


def reader_url_for(admin_url: str) -> str:
    """The same database's URL, authenticated as the read-only `kb_reader` role the migrations create.

    Args:
        admin_url: A database's admin URL.

    Returns:
        The URL that logs in as `kb_reader`.
    """
    return _with_userinfo(admin_url, _READER_ROLE, _reader_password())


TEST_READER_SECRET = "reader-secret-for-tests"  # noqa: S105 (a throwaway secret for disposable test databases)
"""The reader secret every private reader role in these fixtures derives its password from."""


async def provision_private_reader(admin_url: str, collection: str) -> str:
    """Give `collection` its own reader role in the database at `admin_url`, and answer the URL that logs in as it.

    The role is provisioned the way the indexer provisions it, under `TEST_READER_SECRET`. Roles belong to the whole
    server, so a second database gets the same role and its own scope row.

    Args:
        admin_url: A migrated database's admin URL.
        collection: The private collection to give a role.

    Returns:
        The database's URL, authenticated as the collection's reader role.
    """
    engine = create_async_engine(admin_url, poolclass=NullPool)
    try:
        await SqlReaderScopes(engine, TEST_READER_SECRET).scope(collection, private=True)
    finally:
        await engine.dispose()
    return private_reader_url(reader_url_for(admin_url), collection, TEST_READER_SECRET)


def _with_userinfo(url: str, user: str, password: str) -> str:
    """Replace the `user:pass@` segment of a SQLAlchemy URL, keeping the scheme, host and database."""
    scheme, _, rest = url.partition("://")
    _old_userinfo, _, host_and_db = rest.rpartition("@")
    return f"{scheme}://{user}:{password}@{host_and_db}"


def _asyncpg_dsn(url: str) -> str:
    """Turn a SQLAlchemy `postgresql+asyncpg://` URL into the plain `postgresql://` form asyncpg wants."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _with_database(url: str, db_name: str) -> str:
    """Swap the database name in a SQLAlchemy URL, preserving the `+asyncpg` driver tag."""
    base, _, _old = url.rpartition("/")
    return f"{base}/{db_name}"
