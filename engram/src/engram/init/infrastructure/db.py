"""Migrate a knowledge base database to the code's alembic head and seed its vocabulary rows.

No function here constructs `ApplicationSettings`, so each is safe to call in a knowledge base
directory that has no `_meta/` tree yet. `alembic.ini` and `config/application_settings.json` are
resolved relative to the engram checkout, so a host embedding the wheel supplies its own.
"""

from __future__ import annotations

import asyncio
import json
import os

from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from core.db import async_dsn, build_engine

from engram.indexing.contracts import SqlAlchemyKnowledgeBaseUnitOfWork, load_vocab, register_pgvector
from engram.init.domain.revision import revision_standing
from engram.init.exceptions import SchemaAheadOfCodeError, SchemaBehindHeadError

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Connection

    from engram.kb.contracts import VocabularyIndex


# The engram project root, which holds alembic.ini. The 4 counts the folders above this file, so a move changes it.
_ENGRAM_ROOT: Path = Path(__file__).resolve().parents[4]

# The application_settings.json that holds the DB URL when no env override is set.
# Same path the alembic env.py uses: <engram>/config/application_settings.json.
_CONFIG_JSON: Path = _ENGRAM_ROOT / "config" / "application_settings.json"


def resolve_db_url(override: str | None) -> str:
    """Resolve the database URL from the one shared precedence ladder.

    The answer is a credentialed DSN, so pass it on and never log or echo it.
    Precedence (highest first):

    1. `override`, the caller's explicit URL (or `None` if absent).
    2. `APP_INFRASTRUCTURE__DATABASE__URL` environment variable.
    3. `infrastructure.database.url` from `config/application_settings.json`.

    Args:
        override: The caller's explicit URL (the `--db-url` flag value or the
            migration test seam), or `None` when absent.

    Returns:
        The resolved database URL string.

    Raises:
        ValueError: When no URL can be resolved from any source.
    """
    if override:
        return async_dsn(override)
    env_url = os.environ.get("APP_INFRASTRUCTURE__DATABASE__URL")
    if env_url:
        return async_dsn(env_url)
    json_url = _config_value("infrastructure", "database", "url")
    if json_url:
        return async_dsn(json_url)
    msg = (
        "No database URL: pass --db-url, set APP_INFRASTRUCTURE__DATABASE__URL, "
        "or set infrastructure.database.url in config/application_settings.json"
    )
    raise ValueError(msg)


def _config_value(*keys: str) -> str | None:
    """Read the string at a key path in `config/application_settings.json`, or `None` when absent."""
    if not _CONFIG_JSON.is_file():
        return None
    node: object = json.loads(_CONFIG_JSON.read_text(encoding="utf-8"))
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node if isinstance(node, str) else None


def upgrade_schema(db_url: str) -> None:
    """Run `alembic upgrade head` against `db_url` without constructing ApplicationSettings.

    Neither the config JSON nor any environment variable is read for the URL.

    Args:
        db_url: The asyncpg-dialect database URL to migrate
            (e.g. `postgresql+asyncpg://kb:kb@localhost:5433/kb`).
    """
    alembic_config = AlembicConfig(str(_ENGRAM_ROOT / "alembic.ini"))
    alembic_config.attributes["sqlalchemy_url"] = db_url
    alembic_command.upgrade(alembic_config, "head")


def _require_at_head(db_rev: str | None, head: str | None, *, known: Callable[[str], bool]) -> None:
    """Raise the error naming the fix for the mismatch's direction, never a downgrade, unless `db_rev` is `head`."""
    standing = revision_standing(db_rev, head, known=known)
    if standing == "at_head":
        return
    if standing == "behind":
        msg = (
            f"knowledge_base DB is at revision {db_rev or 'none'}, but the code expects head {head}. "
            "Run `uv run alembic upgrade head` (or `studio init db`) before indexing."
        )
        raise SchemaBehindHeadError(msg)
    msg = (
        f"knowledge_base DB is at revision {db_rev}, which this code does not know (its head is {head}). "
        "The code is older than the database. Update this checkout (or the plugin) to one that knows "
        f"revision {db_rev}. Do not touch the database, it is the newer side."
    )
    raise SchemaAheadOfCodeError(msg)


def code_head_revision() -> str | None:
    """The alembic head the installed code declares, read from its own migration scripts.

    Returns:
        The head revision id, or `None` when the scripts declare no revision.
    """
    alembic_config = AlembicConfig(str(_ENGRAM_ROOT / "alembic.ini"))
    return ScriptDirectory.from_config(alembic_config).get_current_head()


def _known_revisions() -> frozenset[str]:
    """Every revision id the installed code's migration scripts contain."""
    alembic_config = AlembicConfig(str(_ENGRAM_ROOT / "alembic.ini"))
    return frozenset(script.revision for script in ScriptDirectory.from_config(alembic_config).walk_revisions())


def _read_current_revision(connection: Connection) -> str | None:
    """Read the current alembic revision off a live sync connection, `None` if unmigrated."""
    return MigrationContext.configure(connection).get_current_revision()


async def _current_db_revision(db_url: str) -> str | None:
    """Read the database's current alembic revision over a short-lived engine, `None` if never migrated."""
    engine = build_engine(db_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_read_current_revision)
    finally:
        await engine.dispose()


def assert_schema_at_head(db_url: str) -> None:
    """Raise a typed error unless the database at `db_url` is migrated to the code's head.

    Run it before touching any knowledge base row.

    Args:
        db_url: The asyncpg-dialect database URL to check (the resolved `infrastructure.database.url`).

    Raises:
        SchemaBehindHeadError: When the database is behind the code's head, naming both revisions
            and the fix (`alembic upgrade head` / `studio init db`).
        SchemaAheadOfCodeError: When the database carries a revision this code does not know,
            naming both revisions and the newer checkout the code needs, never a downgrade.
    """
    head = code_head_revision()
    db_rev = asyncio.run(_current_db_revision(db_url))
    _require_at_head(db_rev, head, known=_known_revisions().__contains__)


async def load_vocabulary(db_url: str, vocabulary: VocabularyIndex, collection: str) -> int:
    """Upsert the vocabulary rows of `vocabulary` into the database at `db_url`.

    It builds a short-lived engine and unit of work from `db_url` and builds no dependency-injection container.

    Args:
        db_url: The asyncpg-dialect database URL.
        vocabulary: The vocabulary index whose rows are upserted.
        collection: The knowledge base the rows belong to.

    Returns:
        The number of rows upserted.
    """
    engine = build_engine(db_url)
    await register_pgvector(engine)
    try:
        return await load_vocab(partial(SqlAlchemyKnowledgeBaseUnitOfWork, engine), vocabulary, collection)
    finally:
        await engine.dispose()


def load_vocabulary_sync(db_url: str, vocabulary: VocabularyIndex, collection: str) -> int:
    """Upsert the vocabulary rows of `vocabulary` into the database at `db_url`.

    It blocks until the upsert finishes and must not be called from inside a running event loop.

    Args:
        db_url: The asyncpg-dialect database URL.
        vocabulary: The vocabulary index whose rows are upserted.
        collection: The knowledge base the rows belong to.

    Returns:
        The number of rows upserted.
    """
    return asyncio.run(load_vocabulary(db_url, vocabulary, collection))
