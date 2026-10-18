"""The private reader roles: giving a private knowledge base its own login role, and reading through it.

`SqlReaderScopes` runs as the owner. It creates a private knowledge base's role with the reader's own settings, makes it
a member of `kb_reader` so it holds every `api` grant, and records which collection the role may read in
`kb.reader_scopes`, the table `api.visible_collection` consults. `PrivateReaders` runs in the reader. It asks the
anonymous reader whether it can see a collection, and when it cannot, opens an engine logged in as that collection's
role.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from core.db import build_reader_engine
from core.observability import get_logger
from sqlalchemy import text
from sqlalchemy.engine import make_url

from engram.indexing.contracts import register_pgvector
from engram.retrieval.reader_roles import READER_ROLE, private_reader_password, private_reader_role

from .gateway import verify_reader_role

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

_logger = get_logger(__name__)

_ROLE_EXISTS_SQL = text("SELECT 1 FROM pg_roles WHERE rolname = :role")
_READER_SETTINGS_SQL = text("SELECT coalesce(rolconfig, ARRAY[]::text[]) FROM pg_roles WHERE rolname = :role")
_IS_MEMBER_SQL = text(
    "SELECT 1 FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid JOIN pg_roles r ON r.oid = m.member"
    " WHERE g.rolname = :group AND r.rolname = :role"
)
_ADD_SCOPE_SQL = text("INSERT INTO kb.reader_scopes (role_name, collection) VALUES (:role, :collection) ON CONFLICT DO NOTHING")
_REMOVE_SCOPES_SQL = text("DELETE FROM kb.reader_scopes WHERE collection = :collection")
_VISIBLE_SQL = text("SELECT api.visible_collection(:collection)")


def private_reader_url(reader_url: str, collection: str, secret: str) -> str:
    """`reader_url` with its user and password swapped for the private reader role of `collection`.

    Args:
        reader_url: The anonymous reader's URL, whose host, port, database and options are kept.
        collection: The private knowledge base's collection.
        secret: The deployment's reader secret. It must not be empty.

    Returns:
        The URL to connect as that collection's reader role.

    Raises:
        ValueError: When `secret` is empty.
    """
    role = private_reader_role(collection)
    return make_url(reader_url).set(username=role, password=private_reader_password(role, secret)).render_as_string(hide_password=False)


def _literal(value: str) -> str:
    """`value` as a single-quoted SQL literal."""
    return "'" + value.replace("'", "''") + "'"


class SqlReaderScopes:
    """Gives a private knowledge base its own reader role, and takes the scope away once it is shared.

    You must build it over the owner engine. Every call is idempotent and sets the role's password again, so a changed
    secret takes effect at the next index.
    """

    def __init__(self, engine: AsyncEngine, secret: str) -> None:
        """Wrap the owner engine and the deployment's reader secret.

        Args:
            engine: An engine whose role may create roles and write `kb.reader_scopes`.
            secret: The reader secret. Empty, a private knowledge base gets no role and stays unreadable to every reader.
        """
        self._engine = engine
        self._secret = secret

    async def scope(self, collection: str, *, private: bool) -> None:
        """Give `collection` its reader role when it is private, or remove its scope when it is shared.

        Args:
            collection: The knowledge base's collection.
            private: Whether the knowledge base declares itself private.
        """
        if not private:
            async with self._engine.begin() as conn:
                await conn.execute(_REMOVE_SCOPES_SQL, {"collection": collection})
            return
        if not self._secret:
            _logger.warning("{collection} is private but no reader secret is set, so no reader can read it", collection=collection)
            return
        role = private_reader_role(collection)
        async with self._engine.begin() as conn:
            if (await conn.execute(_ROLE_EXISTS_SQL, {"role": role})).first() is None:
                await conn.execute(text(f"CREATE ROLE {role} LOGIN"))
            await conn.execute(text(f"ALTER ROLE {role} PASSWORD {_literal(private_reader_password(role, self._secret))}"))
            settings = (await conn.execute(_READER_SETTINGS_SQL, {"role": READER_ROLE})).scalar_one()
            for setting in settings:
                name, _, value = setting.partition("=")
                await conn.execute(text(f"ALTER ROLE {role} SET {name} = {_literal(value)}"))
            if (await conn.execute(_IS_MEMBER_SQL, {"group": READER_ROLE, "role": role})).first() is None:
                await conn.execute(text(f"GRANT {READER_ROLE} TO {role}"))
            await conn.execute(_ADD_SCOPE_SQL, {"role": role, "collection": collection})


class PrivateReaders:
    """Opens the engine a reader reads one collection through: the anonymous reader's, or the collection's own role's.

    A collection the anonymous reader can see is read anonymously. A collection it cannot see is private, and is read
    through its own role when the reader secret is set. With no secret it stays anonymous and reads nothing. Each answer
    and engine is kept for the life of this object, so `dispose` it when the reader closes.
    """

    def __init__(self, reader_url: str, secret: str) -> None:
        """Remember how to reach a private collection's role.

        Args:
            reader_url: The anonymous reader's URL, whose host, database and options every role shares.
            secret: The reader secret, or empty to read every collection anonymously.
        """
        self._reader_url = reader_url
        self._secret = secret
        self._engines: dict[str, AsyncEngine | None] = {}
        self._opening = asyncio.Lock()

    async def engine_for(self, collection: str, anonymous: AsyncEngine) -> AsyncEngine:
        """The engine to read `collection` through.

        Args:
            collection: The collection a read announces.
            anonymous: The anonymous reader's engine, which answers whether `collection` is visible to it.

        Returns:
            `anonymous` for a visible collection, or the collection's own role's engine for a private one.

        Raises:
            ReaderRoleOverprivilegedError: When the collection's role can do more than read the api schema.
        """
        async with self._opening:
            if collection not in self._engines:
                self._engines[collection] = await self._open(collection, anonymous)
        return self._engines[collection] or anonymous

    async def _open(self, collection: str, anonymous: AsyncEngine) -> AsyncEngine | None:
        """The private role's engine for an invisible collection, or None to read anonymously."""
        async with anonymous.connect() as conn:
            visible = bool((await conn.execute(_VISIBLE_SQL, {"collection": collection})).scalar_one())
        if visible:
            return None
        if not self._secret:
            _logger.warning("{collection} is private but no reader secret is set, so it reads as empty", collection=collection)
            return None
        engine = build_reader_engine(private_reader_url(self._reader_url, collection, self._secret))
        await verify_reader_role(engine)
        await register_pgvector(engine)
        return engine

    async def dispose(self) -> None:
        """Dispose every private engine this object opened."""
        for engine in self._engines.values():
            if engine is not None:
                await engine.dispose()
        self._engines.clear()
