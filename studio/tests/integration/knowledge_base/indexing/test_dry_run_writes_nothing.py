r"""`studio index --dry-run` previews the dirty set and writes NOTHING.

The dry-run guarantee, proven against a real local Postgres: after a baseline full index,
editing one note and running `studio index --dry-run` must

* print the dirty-set preview — the changed file (and any inbound closure) listed under the right
  partition (added / removed / content-changed / inbound-closure / mirror-citers), plus the
  `pages_to_reembed` / `edges_to_rebuild` / `graph_refresh` signals; and
* leave EVERY `memory.*` table byte-identical to the pre-dry-run snapshot — **including** the
  index-private detection tables (`index_manifest`, `link_refs`, `validation_store`,
  `index_fingerprint`), so the proof covers the manifest the change detector reads, not just the
  served rows; and
* exit 0 (a preview never runs validation, so a non-clean knowledge base still exits 0).

The byte-equality oracle reuses `test_incremental_equals_full.py`'s `_normalize` row renderer,
wrapped in a local dump whose table map is widened to the four index-state tables a dry run must
also not touch. The whole module skips when Docker is unavailable (via the `pg` fixture).
"""

from __future__ import annotations

import asyncio
import uuid

from collections import Counter
from typing import TYPE_CHECKING

import asyncpg
import pytest

from core.cqrs.contracts import JobQueries, MemoryJobs, MessageBus
from engram.indexing.contracts import FilesystemKnowledgeBaseReader, IndexKnowledgeBase, IndexKnowledgeBaseHandler, KnowledgeBaseReader
from engram.indexing.infrastructure import orm
from engram.kb.contracts import load_knowledge_base_schema
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.infrastructure.knowledge_base_db import run_upgrade_head
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from sqlalchemy import NullPool, select
from sqlalchemy.ext.asyncio import create_async_engine
from typer.testing import CliRunner

from studio.knowledge_base.cli import app
from studio.knowledge_base.indexing.infrastructure.validation_report_sink import FileValidationReportSink
from studio.knowledge_base.wiring.contracts import AdminSqlGateway
from studio.knowledge_base.wiring.indexing import KnowledgeBaseUnitOfWorkFactory
from tests.integration.knowledge_base.indexing.test_incremental_equals_full import _build_baseline, _normalize

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

runner = CliRunner()


def _run[T](coro: Coroutine[object, object, T]) -> T:
    """Drive a coroutine to completion from a synchronous test body (no loop is running).

    The test body must be synchronous: `runner.invoke` drives the Typer `index` command, whose
    handler calls `asyncio.run` — so an `async def` test (running inside pytest-asyncio's loop) would
    nest `asyncio.run` and raise. DB snapshot reads therefore wrap their own `asyncio.run` here,
    legal precisely because the sync test body has no running loop (the e2e suite's pattern).
    """
    return asyncio.run(coro)


# Every `memory.*` table a dry run must leave untouched: the serving-facing data tables PLUS the four
# index-private detection tables (`index_manifest` / `link_refs` / `validation_store` /
# `index_fingerprint`). Mapped to its ORM model so the dump selects typed columns (no raw SQL),
# mirroring `test_incremental_equals_full.py::_TABLES` but widened to the state tables this test owns.
_ALL_TABLES: dict[str, type[orm.KbBase]] = {
    "sources_raw": orm.SourceRawORM,
    "raw_chunks": orm.RawChunkORM,
    "pages": orm.PageORM,
    "sections": orm.SectionORM,
    "claims": orm.ClaimORM,
    "edges": orm.EdgeORM,
    "vocab": orm.VocabORM,
    "validation_store": orm.ValidationStoreORM,
    "index_manifest": orm.IndexManifestORM,
    "link_refs": orm.LinkRefORM,
    "index_fingerprint": orm.IndexFingerprintORM,
}

# Columns dropped from the byte-equality dump: the `halfvec` `embedding` (its binary round-trip is
# not bit-stable across inserts), the `bigserial` surrogate `id` (insertion-order-dependent,
# carries no meaning), and the non-deterministic clock columns (`synced_at`, `indexed_at`).
_VOLATILE_COLUMNS: frozenset[str] = frozenset({"embedding", "id", "synced_at", "indexed_at"})


# -- fresh-DB-per-test fixture (mirrors test_incremental_equals_full.py::bed) ------------------


def _asyncpg_dsn(url: str) -> str:
    """SQLAlchemy `postgresql+asyncpg://` → the plain `postgresql://` raw asyncpg wants."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _with_database(url: str, db_name: str) -> str:
    """Swap the database name in a SQLAlchemy URL, preserving the `+asyncpg` driver tag."""
    base, _, _old = url.rpartition("/")
    return f"{base}/{db_name}"


async def _create_migrated_db(base_url: str) -> str:
    """Create a fresh `test_<uuid>` database inside the bed container and migrate it to head."""
    db_name = f"test_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(_asyncpg_dsn(base_url))
    try:
        await admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin.close()
    target_url = _with_database(base_url, db_name)
    run_upgrade_head(target_url)
    return target_url


async def _drop_db(base_url: str, target_url: str) -> None:
    """Drop the throwaway database `target_url` created inside the `base_url` container."""
    _base, _, db_name = target_url.rpartition("/")
    admin = await asyncpg.connect(_asyncpg_dsn(base_url))
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    finally:
        await admin.close()


@pytest.fixture
async def engine(pg: str) -> AsyncIterator[AsyncEngine]:
    """A freshly-migrated analog database with the pgvector codec registered, dropped on teardown."""
    url = await _create_migrated_db(pg)
    eng = create_async_engine(url, poolclass=NullPool)
    await orm.register_pgvector(eng)
    try:
        yield eng
    finally:
        await eng.dispose()
        await _drop_db(pg, url)


# -- the dump snapshot (reuses the property test's `_normalize`, widened to the 4 state tables) --


async def _dump_table(engine: AsyncEngine, table: str) -> Counter[object]:
    """Dump `memory.<table>` as a multiset of hashable `(column, value)` rows, volatile columns dropped.

    Selects the mapped model's non-volatile columns in name order and renders each row through the
    property test's `_normalize` (arrays → tuples, jsonb → sorted items, scalars verbatim), so a
    dry-run-vs-snapshot diff is order-independent and multiplicity-exact. Reuses the property test's
    cell normalization; only the table set (the four index-state tables) is local to this test.
    """
    model = _ALL_TABLES[table]
    columns = sorted(column.name for column in model.__table__.columns if column.name not in _VOLATILE_COLUMNS)
    statement = select(*(model.__table__.c[name] for name in columns))
    async with engine.connect() as conn:
        result = await conn.execute(statement)
        return Counter(tuple((name, _normalize(value)) for name, value in zip(columns, row, strict=True)) for row in result.all())


async def _snapshot(engine: AsyncEngine) -> dict[str, Counter[object]]:
    """Dump every `memory.*` table (data + index-state) as a name → row-multiset map."""
    return {table: await _dump_table(engine, table) for table in _ALL_TABLES}


# -- the public-CLI index driver (real PG, fake embed) --------------------------------------


class _Factory:
    """A `ctx.obj` container factory whose `MessageBus` runs a real-PG, fake-embed index handler."""

    def __init__(self, engine: AsyncEngine, knowledge_base: Path) -> None:
        self._reader = FilesystemKnowledgeBaseReader(knowledge_base, note_folders=load_knowledge_base_schema(knowledge_base).note_folders)
        handler = IndexKnowledgeBaseHandler(
            self._reader,
            FakeEmbedding(),  # the only seam — real reader + real PG UoW + real dashboard writer
            KnowledgeBaseUnitOfWorkFactory(engine),
            FileValidationReportSink(knowledge_base),
            spec=load_knowledge_base_schema(knowledge_base),
        )
        self._bus = MessageBus({IndexKnowledgeBase: handler}, {})
        self._gateway = AdminSqlGateway(engine)  # the identity announce's own read, over the same admin engine

    def __call__(self) -> _Container:
        return _Container(self._bus, self._gateway, self._reader)


class _Container:
    """Minimal async container exposing the message bus and identity-announce reads to the CLI command."""

    def __init__(self, bus: MessageBus, gateway: AdminSqlGateway, reader: KnowledgeBaseReader) -> None:
        self._bus = bus
        self._gateway = gateway
        self._reader = reader
        self._jobs = MemoryJobs()

    async def get(self, key: type) -> object:
        if key is MessageBus:
            return self._bus
        if key is AdminSqlGateway:
            return self._gateway
        if key is JobQueries:
            return self._jobs
        if key is KnowledgeBaseReader:
            return self._reader
        msg = f"no registration for {key!r}"
        raise KeyError(msg)


def _baselined_knowledge_base(root: Path) -> Path:
    """Create `root` and lay down the shared baseline knowledge base (paper + entities + mirror + meta)."""
    root.mkdir(parents=True, exist_ok=True)
    _build_baseline(KnowledgeBaseBuilder(root))
    return root


_PARTITION_LABELS: frozenset[str] = frozenset({"added", "removed", "content-changed", "inbound-closure", "mirror-citers"})


def _parse_partitions(stdout: str) -> dict[str, set[str]]:
    """Parse the printed `--dry-run` preview into `partition label → set of sample paths`.

    Walks the `_print_dry_run_preview` output: a `  <label>: <count>` line opens a partition and the
    following `    - <path>` lines are its sample paths. Lets the test assert a path appears under the
    *right* partition header (not merely somewhere in the output). Unknown headers reset the cursor.
    """
    partitions: dict[str, set[str]] = {label: set() for label in _PARTITION_LABELS}
    current: str | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            if current is not None:
                partitions[current].add(stripped.removeprefix("- "))
            continue
        label = stripped.split(":", 1)[0]
        current = label if label in _PARTITION_LABELS else None
    return partitions


# -- the test ---------------------------------------------------------------------------------


class TestDryRun:
    # Synchronous like the e2e `TestIndex`: `runner.invoke` drives the Typer `index` command,
    # whose handler calls `asyncio.run` — so the test body must not run inside an event loop (an
    # `async def` test would, raising "asyncio.run() cannot be called from a running event loop").
    # DB snapshot reads go through `_run`, wrapping their own `asyncio.run` (legal — no loop is
    # running here). The async `engine` fixture still resolves for a sync test under
    # `asyncio_mode=auto` (pytest-asyncio runs it in its own loop).
    def test_dry_run_previews_dirty_set_and_writes_nothing(self, engine: AsyncEngine, tmp_path: Path) -> None:
        """`index --dry-run` after an edit lists the dirty file + closure + signals, leaves every table untouched, exits 0.

        A baseline **full** index seeds the analog; the `lightrag` entity's FILE is renamed (a
        structural edit — the paper `proposes: ["[[lightrag]]"]`, so the rename pulls the paper into
        the inbound closure, the unchanged file whose link must re-resolve). The pre-dry-run snapshot
        of every `memory.*` table (incl. the four index-state tables) is taken, then a bare
        `studio index --dry-run` runs through the public CLI. The printed report must list the old
        path under removed, the new path under added, the paper under inbound-closure, and carry the
        re-embed / edges / graph-refresh signals; every table must be byte-identical to the snapshot
        (nothing was written — no manifest / link_refs / validation / fingerprint write, no commit, no embed,
        no graph refresh); and the exit code must be 0.
        """
        knowledge_base = _baselined_knowledge_base(tmp_path / "knowledge_base")
        baseline = runner.invoke(app, ["index", "--full", "--knowledge-base", str(knowledge_base)], obj=_Factory(engine, knowledge_base))
        assert baseline.exit_code == 0, baseline.stdout

        # Rename the `lightrag` entity's file (stem change). The entity keeps its `id`/`title`, so the
        # paper's `[[lightrag]]` still resolves (via the title key) — but the old stem disappears and a
        # new one appears, so the paper (the inbound linker) must re-resolve: removed = {old path},
        # added = {new path}, inbound-closure = {the paper}.
        entities = knowledge_base / "Wiki" / "Entities"
        renamed_text = (entities / "lightrag.md").read_text(encoding="utf-8")
        (entities / "lightrag.md").unlink()
        (entities / "lightrag-technique.md").write_text(renamed_text, encoding="utf-8")

        before = _run(_snapshot(engine))

        result = runner.invoke(app, ["index", "--dry-run", "--knowledge-base", str(knowledge_base)], obj=_Factory(engine, knowledge_base))

        assert result.exit_code == 0, result.stdout
        # (a) the preview lists the renamed file's old/new paths under removed/added and the inbound
        # closure (the paper) under inbound-closure, each under the right partition header.
        partitions = _parse_partitions(result.stdout)
        assert "Wiki/Entities/lightrag.md" in partitions["removed"]
        assert "Wiki/Entities/lightrag-technique.md" in partitions["added"]
        assert "Wiki/Papers/lightrag-2025.md" in partitions["inbound-closure"]
        # the re-embed / edges / graph-refresh signals are present (the dirty sources' edges rebuild →
        # the AGE graph is re-projected on a real run).
        stdout = result.stdout.lower()
        assert "pages_to_reembed" in stdout
        assert "edges_to_rebuild" in stdout
        assert "graph_refresh: yes" in stdout

        # (b) every memory.* table is byte-identical to the pre-dry-run snapshot (nothing was written).
        after = _run(_snapshot(engine))
        for table in _ALL_TABLES:
            assert after[table] == before[table], f"memory.{table} changed during --dry-run (a dry run must write nothing)"
