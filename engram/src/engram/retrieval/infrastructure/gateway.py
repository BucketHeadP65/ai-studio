r"""The read-only SQL gateway, the production `SqlGateway`.

`ReadOnlySqlGateway` runs reads over one injected `AsyncEngine`. Every read announces the reading
knowledge base, which routes an omitted collection argument to it. A private knowledge base is served
only to a session logged in as its own reader role, so a gateway given `PrivateReaders` reads a private
collection through that role. A gateway with no collection reads anonymously and sees only the shared
knowledge bases.

`rows` runs SQL with bound parameters and returns at most `cap` rows, with a truncation message when
more existed. `api_objects` describes the `api` schema's views, tables and functions with their
COMMENTs. The engine must already have the pgvector codec registered so a `halfvec` parameter
round-trips.

Inside `read_snapshot()` every read of one engine and collection runs in one `REPEATABLE READ` transaction, so a
tool call that reads several times sees one state of the knowledge base, even while an index commits. Outside it each
read is its own transaction.
"""

from __future__ import annotations

import asyncio

from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import copy
from typing import TYPE_CHECKING, Self

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from engram.retrieval.domain import ColumnInfo, ObjectInfo, SchemaDescription, SqlResult
from engram.retrieval.exceptions import ReaderRoleOverprivilegedError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping, Sequence

    from sqlalchemy.engine import Row as SaRow
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

    from .reader_roles import PrivateReaders

STATEMENT_ERRORS: tuple[type[SQLAlchemyError], ...] = (SQLAlchemyError,)
"""The errors a statement that did not run raises through this gateway."""

# Makes the transaction a snapshot read: every statement in it sees the database as the first one did.
_REPEATABLE_READ_SQL = text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")

# Announces the reading session's own collection for the surrounding transaction only. It routes an omitted
# collection argument and grants no visibility.
_SET_COLLECTION_SQL = text("SELECT set_config('app.collection', :collection, true)")

# Views and tables in `api` with their COMMENTs. `:names` is a list of bare names without the `api.`
# prefix, and NULL describes every object.
_OBJECTS_SQL = """
SELECT c.relname AS name,
       CASE c.relkind WHEN 'v' THEN 'view' ELSE 'table' END AS kind,
       obj_description(c.oid, 'pg_class') AS comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'api' AND c.relkind IN ('v', 'r')
  AND (CAST(:names AS text[]) IS NULL OR c.relname = ANY(CAST(:names AS text[])))
ORDER BY c.relname
"""

_COLUMNS_SQL = """
SELECT a.attname AS name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       col_description(c.oid, a.attnum) AS comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
WHERE n.nspname = 'api' AND c.relname = :relname AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""

# Functions in `api`: argument list rendered into the data_type slot, COMMENT via pg_proc. Same
# `:names` allowlist as `_OBJECTS_SQL`, matching `p.proname`.
_FUNCTIONS_SQL = """
SELECT p.proname AS name,
       pg_get_function_arguments(p.oid) AS args,
       obj_description(p.oid, 'pg_proc') AS comment
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'api'
  AND (CAST(:names AS text[]) IS NULL OR p.proname = ANY(CAST(:names AS text[])))
ORDER BY p.proname
"""


class _ReadSnapshot:
    """The open snapshot transactions of one block, one per engine and collection, each used by one read at a time."""

    def __init__(self) -> None:
        self._connections: dict[tuple[AsyncEngine, str | None], AsyncConnection] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def reading(self, engine: AsyncEngine, collection: str | None) -> AsyncGenerator[AsyncConnection]:
        """The snapshot connection for `engine` and `collection`, opened on first use and dropped when a read fails."""
        async with self._lock:
            key = (engine, collection)
            connection = self._connections.get(key)
            if connection is None:
                connection = await engine.connect()
                await connection.execute(_REPEATABLE_READ_SQL)
                self._connections[key] = connection
            try:
                yield connection
            except BaseException:
                del self._connections[key]
                await connection.close()
                raise

    async def release(self) -> None:
        """End every open transaction. A later read in the same block opens a new snapshot."""
        async with self._lock:
            connections, self._connections = list(self._connections.values()), {}
            for connection in connections:
                await connection.close()


_SNAPSHOT: ContextVar[_ReadSnapshot | None] = ContextVar("engram_read_snapshot", default=None)


@asynccontextmanager
async def read_snapshot() -> AsyncGenerator[None]:
    """Make every gateway read inside the block see one snapshot of its engine and collection.

    A block inside another joins the outer one. Reads of different engines or collections see their own snapshots,
    each taken at its first read. Every transaction ends when the outermost block exits.

    Yields:
        Nothing. The reads inside the block share the snapshot.
    """
    if _SNAPSHOT.get() is not None:
        yield
        return
    snapshot = _ReadSnapshot()
    token = _SNAPSHOT.set(snapshot)
    try:
        yield
    finally:
        _SNAPSHOT.reset(token)
        await snapshot.release()


async def release_snapshot() -> None:
    """End the current block's transactions before a slow step, such as a model call, so none stays open through it.

    A read after it opens a new snapshot, which may see a later state. Outside a block it does nothing.
    """
    snapshot = _SNAPSHOT.get()
    if snapshot is not None:
        await snapshot.release()


READER_SETTING = "infrastructure.database.reader_url"
"""The setting that names the reader connection, which an exported DSN may override."""

# What the reader role holds beyond reading `api`, one array per kind of problem. Every array is empty for a
# role that keeps read-only transactions, holds no role attribute, belongs to no role but `kb_reader`, uses no
# schema but `api` and `public`, creates in none and holds no privilege on a relation outside `api`. A privilege
# PUBLIC holds, such as one an extension grants on its own schema, is every role's and is not counted.
_READER_ROLE_SQL = """
SELECT current_user AS role,
       current_setting('default_transaction_read_only') = 'on' AS read_only,
       ARRAY(SELECT attribute FROM (VALUES ('superuser', r.rolsuper), ('createrole', r.rolcreaterole),
                                           ('createdb', r.rolcreatedb), ('bypassrls', r.rolbypassrls),
                                           ('replication', r.rolreplication)) AS a(attribute, held)
             WHERE held) AS attributes,
       ARRAY(SELECT g.rolname::text FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
             WHERE m.member = r.oid AND g.rolname <> 'kb_reader') AS member_of,
       ARRAY(SELECT n.nspname::text FROM pg_namespace n
             WHERE has_schema_privilege(n.oid, 'USAGE') AND NOT has_schema_privilege('public', n.oid, 'USAGE')
               AND n.nspname NOT IN ('api', 'public', 'pg_catalog', 'information_schema')
               AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp%') AS schemas_used,
       ARRAY(SELECT n.nspname::text FROM pg_namespace n
             WHERE has_schema_privilege(n.oid, 'CREATE') AND NOT has_schema_privilege('public', n.oid, 'CREATE')
               AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp%') AS schemas_created_in,
       ARRAY(SELECT n.nspname || '.' || c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relkind IN ('r', 'v', 'm', 'p', 'f')
               AND n.nspname NOT IN ('api', 'pg_catalog', 'information_schema')
               AND has_table_privilege(c.oid, 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
               AND NOT has_table_privilege('public', c.oid, 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
             ORDER BY 1 LIMIT 5) AS relations_outside_api
FROM pg_roles r
WHERE r.rolname = current_user
"""


async def verify_reader_role(engine: AsyncEngine) -> None:
    """Refuse a reader connection that can do more than read the `api` schema.

    The role must keep `default_transaction_read_only` on, hold no role attribute, belong to no role but `kb_reader`,
    use no schema but `api` and `public`, create in none and hold no privilege on a relation outside `api`, beyond
    what PUBLIC holds. Call it once, where the reader engine is built.

    Args:
        engine: The reader engine, before any gateway reads through it.

    Raises:
        ReaderRoleOverprivilegedError: When the role fails any of these, naming the role, each problem and `READER_SETTING`.
    """
    async with engine.connect() as conn:
        row = (await conn.execute(text(_READER_ROLE_SQL))).one()
    problems = [
        *([] if row.read_only else ["its transactions are not read-only by default"]),
        *([f"it holds {', '.join(row.attributes)}"] if row.attributes else []),
        *([f"it belongs to {', '.join(row.member_of)}"] if row.member_of else []),
        *([f"it uses the schemas {', '.join(row.schemas_used)}"] if row.schemas_used else []),
        *([f"it may create in {', '.join(row.schemas_created_in)}"] if row.schemas_created_in else []),
        *([f"it can reach {', '.join(row.relations_outside_api)} outside api"] if row.relations_outside_api else []),
    ]
    if problems:
        msg = (
            f"The reader connection logs in as {row.role!r}, which can do more than read the api schema: {'. '.join(problems)}. "
            f"Point {READER_SETTING}, or the export that overrides it, at the read-only kb_reader role."
        )
        raise ReaderRoleOverprivilegedError(msg)


class ReadOnlySqlGateway:
    """Runs capped reads + api-surface introspection over one `AsyncEngine`, under one session identity.

    Every read announces `collection` to the database as `app.collection`. A private knowledge base is
    visible only through its own reader role, which `private_readers` opens. With no collection, or no
    `private_readers`, the gateway sees only the shared knowledge bases.
    """

    def __init__(self, engine: AsyncEngine, *, collection: str | None = None, private_readers: PrivateReaders | None = None) -> None:
        """Wrap `engine`, reading as `collection`.

        Args:
            engine: The engine every anonymous read runs on. It must already have the pgvector codec registered.
            collection: The knowledge base the reads announce themselves as, or None to read anonymously.
            private_readers: Opens a private collection's own reader role, or None to read every collection through
                `engine`.
        """
        self._engine = engine
        self.collection = collection
        self._private_readers = private_readers

    def for_collection(self, collection: str) -> Self:
        """A copy of this gateway that reads as `collection`'s own session.

        Args:
            collection: The knowledge base partition the reads announce themselves as.

        Returns:
            A gateway over the same engine, carrying that identity.
        """
        bound = copy(self)
        bound.collection = collection
        return bound

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        """Run `sql` with bound `params` and return at most `cap` rows.

        Args:
            sql: The SQL text to run.
            params: The bound parameters, or None for none.
            cap: The most rows to return.

        Returns:
            The rows and columns, with a truncation message when the query had more than `cap` rows.
        """
        statement = text(sql)
        async with self._connected() as conn:
            result = await conn.execute(statement, dict(params or {}))
            columns = tuple(result.keys())
            fetched = result.fetchmany(cap + 1)
        capped, message = _apply_cap(fetched, cap)
        return SqlResult(rows=tuple(_mapping(row) for row in capped), columns=columns, truncated_message=message)

    @asynccontextmanager
    async def _connected(self) -> AsyncGenerator[AsyncConnection]:
        """A connection announcing this gateway's collection: the block's snapshot inside `read_snapshot()`, else a fresh one."""
        engine = await self._reading_engine()
        snapshot = _SNAPSHOT.get()
        if snapshot is None:
            async with engine.connect() as conn:
                await self._announce(conn)
                yield conn
            return
        async with snapshot.reading(engine, self.collection) as conn:
            await self._announce(conn)
            yield conn

    async def _reading_engine(self) -> AsyncEngine:
        """The engine this gateway's collection is read through."""
        if self.collection is None or self._private_readers is None:
            return self._engine
        return await self._private_readers.engine_for(self.collection, self._engine)

    async def _announce(self, conn: AsyncConnection) -> None:
        """Announce this gateway's collection for the connection's open transaction only."""
        if self.collection is not None:
            await conn.execute(_SET_COLLECTION_SQL, {"collection": self.collection})

    async def api_objects(self, names: tuple[str, ...] = ()) -> SchemaDescription:
        """Describe every `api` object (views/tables with columns, then functions), or only `names`.

        Args:
            names: The bare object names to describe, without the `api.` prefix, or `()` for every object.

        Returns:
            Every matching object (views/tables first, then functions), each with its columns.
        """
        bound = {"names": list(names) or None}
        async with self._connected() as conn:
            objects_rows = (await conn.execute(text(_OBJECTS_SQL), bound)).all()
            relations = [await _describe_relation(conn, row) for row in objects_rows]
            function_rows = (await conn.execute(text(_FUNCTIONS_SQL), bound)).all()
            functions = [_describe_function(row) for row in function_rows]
        return SchemaDescription(objects=tuple(relations + functions))


async def _describe_relation(conn: AsyncConnection, relation: SaRow[tuple[str, str, str | None]]) -> ObjectInfo:
    """Build an `ObjectInfo` for one view/table, fetching its ordered columns."""
    column_rows = (await conn.execute(text(_COLUMNS_SQL).bindparams(bindparam("relname", relation.name)))).all()
    columns = tuple(ColumnInfo(name=c.name, data_type=c.data_type, comment=c.comment) for c in column_rows)
    kind = "view" if relation.kind == "view" else "table"
    return ObjectInfo(name=f"api.{relation.name}", kind=kind, comment=relation.comment, columns=columns)


def _describe_function(function: SaRow[tuple[str, str, str | None]]) -> ObjectInfo:
    """Build an `ObjectInfo` for one function, its argument list as the single column line."""
    signature = ColumnInfo(name=f"({function.args})", data_type="", comment=None)
    return ObjectInfo(name=f"api.{function.name}", kind="function", comment=function.comment, columns=(signature,))


def _apply_cap(fetched: Sequence[SaRow[tuple[object, ...]]], cap: int) -> tuple[Sequence[SaRow[tuple[object, ...]]], str | None]:
    """Trim to `cap` rows, return the contract's truncation message when more existed."""
    if len(fetched) > cap:
        return fetched[:cap], f"{cap}+ more rows: narrow your filters"
    return fetched, None


def _mapping(row: SaRow[tuple[object, ...]]) -> dict[str, object]:
    """One result row as a plain dict (the `Row` shape)."""
    return dict(row._mapping)  # noqa: SLF001 (Row._mapping is the public-by-convention accessor)
