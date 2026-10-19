r"""The acceptance gate: an incremental index is bit-for-bit identical to a full rebuild.

Each edit scenario drives TWO freshly created local Postgres databases through the public
index path (the `IndexKnowledgeBaseHandler`, the same write path the CLI invokes):

* **DB A** is built to a baseline by a full index, then the edit is applied to its knowledge base
  and an **incremental** `index` runs (the default path);
* **DB B** is the oracle: a *separate* knowledge base tree with the same edit already applied,
  indexed **once, full, from scratch** into its own fresh database.

`assert_db_equal(A, B)` dumps every serving-facing `memory.*` data table plus
`kb.validation_store`, each sorted deterministically, and asserts byte-equality — so a
discrepancy between the targeted incremental load and the whole knowledge base rebuild surfaces
as a row diff, not as a silent serving drift. The whole knowledge base `ValidationReport` (the CI
`n_errors` gate) is compared too, and the no-op reindex additionally asserts the
incremental run skipped every page and re-embedded nothing.

The shrinking edits (a removed section, a renamed heading, a removed claim anchor, a page stripped
of every child, removed introduction prose) also declare the exact
keys that must be gone, so equality cannot be reached by both sides keeping a row neither should
have. Disclosure summaries, which the index reconciles but never authors, are seeded straight into
the database (see `_seed_derived_rows`), and a
separate test indexes a second collection with colliding ids beside the one being reindexed to
prove no reconcile `DELETE` reaches across.

Two fresh databases per scenario give A and B their own AGE `kb_graph` graph, so the
comparison is never polluted by cross-collection bleed; each is migrated under the bed's
grammar via the `tests/conftest.py` `run_upgrade_head` seam. Both sides embed through the
same hash-seeded :class:`FakeEmbedding` (same `card_text` → same vector), so the embed path
runs for real and the no-op's `embedded == 0` assertion stays meaningful; the `embedding`
column is still excluded from the dump (see :data:`_VOLATILE_COLUMNS`) because the `halfvec`
binary round-trip is not bit-stable across two independent inserts. The module skips when
Docker is unavailable (via the `pg` fixture).
"""

from __future__ import annotations

import uuid

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import asyncpg
import pytest

from engram.indexing.contracts import FilesystemKnowledgeBaseReader, IndexKnowledgeBase, IndexKnowledgeBaseHandler
from engram.indexing.infrastructure import orm
from engram.kb.contracts import load_knowledge_base_schema
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.infrastructure.knowledge_base_db import run_upgrade_head
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from sqlalchemy import NullPool, func, insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from studio.knowledge_base.indexing.infrastructure.validation_report_sink import FileValidationReportSink
from studio.knowledge_base.wiring.indexing import KnowledgeBaseUnitOfWorkFactory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence
    from pathlib import Path

    from engram.indexing.contracts import IndexReport, ValidationReport
    from sqlalchemy.ext.asyncio import AsyncEngine

_COLLECTION = "research"

# A second knowledge base indexed into the same database under its own collection, from a tree that
# produces the very same page and section ids: the neighbour every collection-scoped DELETE must
# leave alone.
_SHADOW_COLLECTION = "shadow"

# The serving-facing `memory.*` data tables the indexer writes, in FK-safe order, plus the
# index-private `validation_store`. Each is dumped sorted by its key columns and compared
# column-by-column. `embed_meta` / `query_log` are excluded (the model contract row carries a
# volatile `embedded_at`, query_log is never indexed); the manifest / fingerprint carry a
# volatile `indexed_at` and are index-private detection state, not served data — not part
# of the incremental==full *output* contract.
_DATA_TABLES: tuple[str, ...] = (
    "sources_raw",
    "raw_chunks",
    "pages",
    "sections",
    "claims",
    "claim_qualifiers",
    "edges",
    "unit_summaries",
    "vocab",
    "validation_store",
)

# Columns excluded from the byte-equality comparison, with the reason each is non-comparable:
# `embedding` — the `halfvec` binary round-trip is not guaranteed bit-stable across two
#   independent inserts (the embed hash-gate is proven by the e2e test instead);
# `synced_at` — a `sources_raw` timestamp the indexer never sets (always NULL here, but
#   excluded defensively as a non-deterministic clock column).
_VOLATILE_COLUMNS: frozenset[str] = frozenset({"embedding", "synced_at"})

# `id` is dropped only on the two `bigserial`-keyed tables: there the surrogate sequence value is
# insertion-order-dependent and carries no meaning, so rows are compared on their business columns.
# Everywhere else `id` IS the business key, and a stale child row is exactly an id one side holds
# and the other does not.
_SURROGATE_ID_TABLES: frozenset[str] = frozenset({"edges", "validation_store"})


# -- baseline knowledge base content --------------------------------------------------
#
# A paper that (1) cites a source mirror, (2) proposes an entity `[[lightrag]]` that exists,
# and (3) carries a claim that links to `[[graphrag]]` (an entity that exists).
# Enough structure to exercise pages, sections, claims, edges, sources, raw_chunks and vocab
# on both the full and the incremental path.

_PAPER = """\
---
id: paper.lightrag-2025
type: paper
title: LightRAG
summary: Graph-indexed RAG.
status: synthesized
zotero_key: 8QXFXYMI
year: 2025
paper_type: method
source_hash: bd72
tasks: [long-doc-qa]
stages: [retrieval]
proposes: ["[[lightrag]]"]
---

## TL;DR

LightRAG indexes a knowledge base as a graph and retrieves at two levels.

## Findings

- LightRAG wins on the large UltraDomain subsets. ^c1
    - loc:: §4.2, Table 1
    - quote:: "LightRAG consistently outperforms GraphRAG, particularly in larger datasets with complex language contexts"
    - kind:: benchmark-result
    - outperforms:: [[graphrag]]
    - on_dataset:: [[ultradomain]]
    - metric:: [[llm-judged-win-rate]]
- LightRAG builds on an as-yet-unwritten technique. ^c2
    - loc:: §3
    - kind:: observation
"""

_ENTITY_LIGHTRAG = """\
---
id: ent.lightrag
type: entity
title: LightRAG
status: draft
entity_type: technique
---

## Definition

A graph-indexed retrieval technique.
"""

_ENTITY_GRAPHRAG = """\
---
id: ent.graphrag
type: entity
title: GraphRAG
status: draft
entity_type: technique
---

## Definition

A graph-based retrieval-augmented generation technique.
"""

_MIRROR = """\
---
source_key: 8QXFXYMI
provider: example-library
source_hash: bd72
---
# LightRAG: Simple and Fast Retrieval-Augmented Generation

## 3 The LightRAG Architecture

LightRAG consistently outperforms GraphRAG, particularly in larger datasets with complex language contexts.
"""

# An entity with introduction prose before its first heading (the reserved preamble section), an
# H2 and a nested H3: the page whose section set the removal scenarios shrink.
_ENTITY_SECTIONS = """\
---
id: ent.sections-demo
type: entity
title: Sections Demo
status: draft
entity_type: technique
---

Introduction prose ahead of every heading.

## Definition

A technique described in sections.

## Mechanism

How it works.

### Graph Indexing

The nested detail.
"""

# Two link-free comparison pages. Nothing references them and they reference nothing, so neither is
# ever pulled into an inbound closure: each is dirty only when its own file changes. The seeded
# comparison cells hang off them, so one owner can be rebuilt while the other stays untouched.
_COMPARISON_EDITED = """\
---
id: cmp.rag-approaches
type: comparison
title: RAG Approaches
status: draft
dimensions: [latency]
---

## TL;DR

Two indexing approaches, one matrix.

## Matrix

The matrix rows are indexed as comparison cells.
"""

_COMPARISON_UNTOUCHED = """\
---
id: cmp.retrieval-modes
type: comparison
title: Retrieval Modes
status: draft
dimensions: [latency]
---

## TL;DR

Retrieval modes, one matrix.

## Matrix

The matrix rows are indexed as comparison cells.
"""

# Scenario E1.5's added note: a new entity that no other page links to.
_ENTITY_NEWTECH = """\
---
id: ent.newtech
type: entity
title: NewTech
status: draft
entity_type: technique
---

## Definition

The previously-unwritten technique the paper builds on.
"""


def _build_baseline(builder: KnowledgeBaseBuilder) -> None:
    """Lay down the shared baseline knowledge base: paper + two entities + mirror + registries + meta."""
    builder.add_unit_document("Wiki/Papers", "lightrag-2025", _PAPER)
    builder.add_unit_document("Wiki/Entities", "lightrag", _ENTITY_LIGHTRAG)
    builder.add_unit_document("Wiki/Entities", "graphrag", _ENTITY_GRAPHRAG)
    builder.add_unit_document("Wiki/Entities", "sections-demo", _ENTITY_SECTIONS)
    builder.add_unit_document("Wiki/Comparisons", "rag-approaches", _COMPARISON_EDITED)
    builder.add_unit_document("Wiki/Comparisons", "retrieval-modes", _COMPARISON_UNTOUCHED)
    builder.add_source_mirror("lightrag-2025", _MIRROR)
    builder.add_vocabulary()
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema()


# -- rows the index reconciles but never authors ------------------------------
#
# Disclosure summaries are written by the summary stage and promoted by a human, so they are seeded
# straight into the database after a baseline index: A gets the whole set before its edit, B gets the whole set minus the keys
# the scenario declares stale. A reconcile that deletes too much or too little shows up as a row
# diff between the two.

# `(unit_id, unit_kind)` for each seeded `active` summary. Every one describes a unit the baseline
# knowledge base has; the scenarios that take that unit away declare it stale.
_SEEDED_SUMMARIES: tuple[tuple[str, str], ...] = (
    ("paper.lightrag-2025", "page"),
    ("ent.graphrag", "page"),
    ("ent.sections-demo#__preamble__", "section"),
    ("ent.sections-demo#mechanism.graph_indexing", "section"),
)

# A fixed stamp on every seeded summary, so the two databases' rows are comparable byte for byte
# (the column defaults to `now()`, which two independent inserts never agree on).
_SEEDED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _summary_row(collection: str, unit_id: str, unit_kind: str) -> dict[str, object]:
    """One `kb.unit_summaries` insert row: an active summary of `unit_id`, stamped deterministically."""
    return {
        "collection": collection,
        "unit_id": unit_id,
        "unit_kind": unit_kind,
        "summary": f"A summary of {unit_id}.",
        "status": "active",
        "model": "seeded-model",
        "source_hash": "seeded-hash",
        "indexed_at": _SEEDED_AT,
    }


def _summary_rows(collection: str, unit_ids: Sequence[str]) -> list[dict[str, object]]:
    """`kb.unit_summaries` insert rows for `unit_ids`, in the seeded set's declared order."""
    kinds = dict(_SEEDED_SUMMARIES)
    return [_summary_row(collection, unit_id, kinds[unit_id]) for unit_id in unit_ids]


async def _insert(engine: AsyncEngine, model: type[orm.KbBase], rows: Sequence[Mapping[str, object]]) -> None:
    """Insert `rows` into `model`'s table in one transaction (a no-op on an empty batch)."""
    if not rows:
        return
    async with engine.begin() as conn:
        await conn.execute(insert(model), [dict(row) for row in rows])


async def _seed_derived_rows(engine: AsyncEngine, *, scenario: _Scenario, final_state: bool) -> None:
    """Seed the summaries the index reconciles but never writes.

    `final_state` seeds the oracle: the whole set minus the keys `scenario` declares stale, which is
    exactly what a knowledge base in the post-edit state should still hold. Without it the whole set
    is seeded, which is what the pre-edit knowledge base held.
    """
    stale_units = {unit_id for table, unit_id in scenario.stale_ids if table == "unit_summaries"}
    units = [unit_id for unit_id, _kind in _SEEDED_SUMMARIES if not (final_state and unit_id in stale_units)]
    await _insert(engine, orm.UnitSummaryORM, _summary_rows(_COLLECTION, units))


# -- the edit each scenario applies to an already-baselined knowledge base ------------


def _edit_body_only(knowledge_base: Path) -> None:
    """E1.1 — a body-only edit: reword the paper's TL;DR (no resolution-key change)."""
    paper = knowledge_base / "Wiki" / "Papers" / "lightrag-2025.md"
    paper.write_text(_PAPER.replace("retrieves at two levels.", "retrieves at two distinct levels of granularity."), encoding="utf-8")


def _edit_add_preamble(knowledge_base: Path) -> None:
    """Add introduction prose, creating the reserved section row."""
    paper = knowledge_base / "Wiki" / "Papers" / "lightrag-2025.md"
    paper.write_text(
        _PAPER.replace("\n## TL;DR", "\nPreamble-only retrieval phrase.\n\n## TL;DR"),
        encoding="utf-8",
    )


def _edit_title_change(knowledge_base: Path) -> None:
    """E1.2 — a title change on an inbound-linked entity (the `graphrag` claim target)."""
    entity = knowledge_base / "Wiki" / "Entities" / "graphrag.md"
    entity.write_text(_ENTITY_GRAPHRAG.replace("title: GraphRAG", "title: Graph RAG (renamed)"), encoding="utf-8")


def _edit_file_rename(knowledge_base: Path) -> None:
    """E1.3 — a file rename (stem change) of the `lightrag` entity the paper proposes."""
    entities = knowledge_base / "Wiki" / "Entities"
    (entities / "lightrag.md").unlink()
    (entities / "lightrag-technique.md").write_text(_ENTITY_LIGHTRAG, encoding="utf-8")


def _edit_id_rename(knowledge_base: Path) -> None:
    """E1.4 — an `id` rename on the `graphrag` entity (old id deleted, new id added)."""
    entity = knowledge_base / "Wiki" / "Entities" / "graphrag.md"
    entity.write_text(_ENTITY_GRAPHRAG.replace("id: ent.graphrag", "id: ent.graphrag-v2"), encoding="utf-8")


def _edit_add_satisfying_note(knowledge_base: Path) -> None:
    """E1.5: add a new entity note that no other page links to."""
    (knowledge_base / "Wiki" / "Entities" / "newtech.md").write_text(_ENTITY_NEWTECH, encoding="utf-8")


def _edit_delete_link_target(knowledge_base: Path) -> None:
    """E1.6 — delete a link target (the `graphrag` entity the claim resolves to)."""
    (knowledge_base / "Wiki" / "Entities" / "graphrag.md").unlink()


def _edit_change_source_mirror(knowledge_base: Path) -> None:
    """E1.7 — change a source mirror (the `mirror_citers` re-verification path)."""
    mirror = knowledge_base / "Sources" / "lightrag-2025.md"
    mirror.write_text(_MIRROR.replace("## 3 The LightRAG Architecture", "## 3 The LightRAG Architecture (revised)"), encoding="utf-8")


def _edit_noop(knowledge_base: Path) -> None:
    """E1.8 — the no-op reindex: leave the baselined knowledge base untouched."""
    del knowledge_base


def _sections_demo(knowledge_base: Path) -> Path:
    """The entity note whose section set the removal scenarios shrink."""
    return knowledge_base / "Wiki" / "Entities" / "sections-demo.md"


def _edit_remove_h3(knowledge_base: Path) -> None:
    """Remove the nested H3, leaving its owning H2 in place."""
    _sections_demo(knowledge_base).write_text(
        _ENTITY_SECTIONS.replace("\n### Graph Indexing\n\nThe nested detail.\n", ""), encoding="utf-8"
    )


def _edit_rename_h2(knowledge_base: Path) -> None:
    """Rename an H2, which re-keys it and the H3 nested under it."""
    _sections_demo(knowledge_base).write_text(_ENTITY_SECTIONS.replace("## Mechanism", "## Variants"), encoding="utf-8")


def _edit_remove_preamble(knowledge_base: Path) -> None:
    """Remove the introduction prose, which removes the reserved preamble section."""
    _sections_demo(knowledge_base).write_text(
        _ENTITY_SECTIONS.replace("Introduction prose ahead of every heading.\n\n", ""), encoding="utf-8"
    )


def _edit_remove_claim_anchor(knowledge_base: Path) -> None:
    """Remove one `^cN` claim block from the paper, leaving the other."""
    paper = knowledge_base / "Wiki" / "Papers" / "lightrag-2025.md"
    paper.write_text(_PAPER.split("- LightRAG builds on an as-yet-unwritten technique. ^c2", maxsplit=1)[0], encoding="utf-8")


def _edit_strip_page_children(knowledge_base: Path) -> None:
    """Strip the paper's whole body, so the page keeps no section and no claim at all."""
    paper = knowledge_base / "Wiki" / "Papers" / "lightrag-2025.md"
    frontmatter, _, _body = _PAPER.partition("---\n\n")
    paper.write_text(frontmatter + "---\n", encoding="utf-8")


@dataclass(frozen=True, slots=True)
class _Scenario:
    """One edit scenario: a stable id, the in-place knowledge base mutation, and the rows it makes stale.

    `stale_ids` names, per table, the exact business keys the incremental database must no longer
    hold after the edit. They are the positive half of the oracle: `assert_db_equal` proves A and B agree, these prove the
    agreement is reached by *removing* the right rows rather than by both sides keeping them.
    """

    id: str
    apply_edit: Callable[[Path], None]
    stale_ids: tuple[tuple[str, str], ...] = ()


_SCENARIOS: tuple[_Scenario, ...] = (
    _Scenario("body-only", _edit_body_only),
    _Scenario("add-preamble", _edit_add_preamble),
    _Scenario("title-change", _edit_title_change),
    _Scenario("file-rename", _edit_file_rename),
    _Scenario("id-rename", _edit_id_rename, stale_ids=(("pages", "ent.graphrag"), ("unit_summaries", "ent.graphrag"))),
    _Scenario("add-satisfies-dangling", _edit_add_satisfying_note),
    _Scenario(
        "delete-link-target", _edit_delete_link_target, stale_ids=(("pages", "ent.graphrag"), ("unit_summaries", "ent.graphrag"))
    ),
    _Scenario("change-source-mirror", _edit_change_source_mirror),
    _Scenario("no-op", _edit_noop),
    _Scenario(
        "remove-h3",
        _edit_remove_h3,
        stale_ids=(
            ("sections", "ent.sections-demo#mechanism.graph_indexing"),
            ("unit_summaries", "ent.sections-demo#mechanism.graph_indexing"),
        ),
    ),
    _Scenario(
        "rename-h2",
        _edit_rename_h2,
        stale_ids=(
            ("sections", "ent.sections-demo#mechanism"),
            ("sections", "ent.sections-demo#mechanism.graph_indexing"),
            ("unit_summaries", "ent.sections-demo#mechanism.graph_indexing"),
        ),
    ),
    _Scenario(
        "remove-preamble",
        _edit_remove_preamble,
        stale_ids=(("sections", "ent.sections-demo#__preamble__"), ("unit_summaries", "ent.sections-demo#__preamble__")),
    ),
    _Scenario("remove-claim-anchor", _edit_remove_claim_anchor, stale_ids=(("claims", "paper.lightrag-2025#c2"),)),
    _Scenario(
        "strip-page-children",
        _edit_strip_page_children,
        stale_ids=(
            ("sections", "paper.lightrag-2025#tl_dr"),
            ("sections", "paper.lightrag-2025#findings"),
            ("claims", "paper.lightrag-2025#c1"),
            ("claims", "paper.lightrag-2025#c2"),
        ),
    ),
)


# -- fresh-DB-per-side fixture (mirrors test_migrations.py::_migrated_graph_age) ----


def _asyncpg_dsn(url: str) -> str:
    """SQLAlchemy `postgresql+asyncpg://` → the plain `postgresql://` raw asyncpg wants."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _with_database(url: str, db_name: str) -> str:
    """Swap the database name in a SQLAlchemy URL, preserving the `+asyncpg` driver tag."""
    base, _, _old = url.rpartition("/")
    return f"{base}/{db_name}"


async def _create_migrated_db(base_url: str) -> str:
    """Create a fresh `test_<uuid>` database inside the bed container and migrate it to head.

    Mirrors the `migrated` fixture / `test_migrations.py`'s fresh-DB creation: `CREATE
    DATABASE` cannot run in a transaction block (asyncpg autocommits a simple exec), then
    `run_upgrade_head` (the `tests/conftest.py` seam) migrates under the bed's grammar.
    Returns the new database's admin SQLAlchemy URL.
    """
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


@dataclass(slots=True)
class _Bed:
    """A pair of fresh, migrated analog databases (A and B) with engines + the base URL.

    `a` receives the incremental treatment, `b` the full-rebuild-from-scratch treatment;
    each is its own `test_<uuid>` database (its own AGE `kb_graph`), so the comparison never
    bleeds across collections. The engines carry the pgvector codec so the `halfvec`
    columns round-trip on read.
    """

    base_url: str
    a_url: str
    b_url: str
    a_engine: AsyncEngine
    b_engine: AsyncEngine


@pytest.fixture
async def bed(pg: str) -> AsyncIterator[_Bed]:
    """Two freshly-migrated analog databases (A + B) inside the shared bed container.

    Each scenario gets its own pair so A's incremental history and B's full rebuild never
    share a database (or an AGE graph). The databases are dropped and the engines disposed
    on teardown (the container itself is session-scoped and reused).
    """
    a_url = await _create_migrated_db(pg)
    b_url = await _create_migrated_db(pg)
    a_engine = create_async_engine(a_url, poolclass=NullPool)
    b_engine = create_async_engine(b_url, poolclass=NullPool)
    await orm.register_pgvector(a_engine)
    await orm.register_pgvector(b_engine)
    try:
        yield _Bed(base_url=pg, a_url=a_url, b_url=b_url, a_engine=a_engine, b_engine=b_engine)
    finally:
        await a_engine.dispose()
        await b_engine.dispose()
        await _drop_db(pg, a_url)
        await _drop_db(pg, b_url)


# -- index driver (the public write path, fake-embed, real PG) -------------


async def _index(engine: AsyncEngine, knowledge_base: Path, *, full: bool, embed: bool = True, collection: str = _COLLECTION) -> IndexReport:
    """Run one index against `engine`'s database through the production handler.

    The only seam is the deterministic :class:`FakeEmbedding` (real knowledge base reader, real
    `SqlAlchemyKnowledgeBaseUnitOfWork` over the throwaway PG, real dashboard writer) — the same
    wiring `test_multi_collection_index.py` uses. `full=True` forces the whole knowledge base
    rebuild oracle; `full=False` lets `handle` route to the incremental path when the
    manifest + fingerprint allow it. `embed=False` skips the embed stage (`--no-embed`),
    leaving every row's vector NULL.
    """
    handler = IndexKnowledgeBaseHandler(
        FilesystemKnowledgeBaseReader(knowledge_base, note_folders=load_knowledge_base_schema(knowledge_base).note_folders),
        FakeEmbedding(),
        KnowledgeBaseUnitOfWorkFactory(engine),
        FileValidationReportSink(knowledge_base),
        spec=load_knowledge_base_schema(knowledge_base),
    )
    return await handler.handle(IndexKnowledgeBase(collection=collection, full=full, embed=embed))


async def _whole_validation_report(engine: AsyncEngine) -> ValidationReport:
    """The reassembled whole knowledge base `ValidationReport` persisted in `engine`'s `kb.validation_store`.

    Both paths persist the whole knowledge base validation: the full path writes every page's issues, the
    incremental path keeps clean pages' stored issues and overwrites the dirty ones. So the
    reassembled report read back through the production `ValidationIssues.whole_report` is the
    whole knowledge base view on either side — the CI `n_errors` gate, which must be identical. Built via
    a short read UoW (the same query the serving / status path uses).
    """
    async with KnowledgeBaseUnitOfWorkFactory(engine)() as uow:
        return await uow.validation_store.whole_report(_COLLECTION)


# -- the byte-equality oracle ------------------------------------------------

# The mapped ORM table for each dumped name, so the dump selects typed columns (no raw SQL /
# `information_schema`) and tracks the schema through the models the loader already writes.
_TABLES: dict[str, type[orm.KbBase]] = {
    "sources_raw": orm.SourceRawORM,
    "raw_chunks": orm.RawChunkORM,
    "pages": orm.PageORM,
    "sections": orm.SectionORM,
    "claims": orm.ClaimORM,
    "claim_qualifiers": orm.ClaimQualifierORM,
    "edges": orm.EdgeORM,
    "unit_summaries": orm.UnitSummaryORM,
    "vocab": orm.VocabORM,
    "validation_store": orm.ValidationStoreORM,
}

# The column each reconciled table keys its rows by, for the stale-row assertion.
_STALE_KEY_COLUMNS: dict[str, str] = {"pages": "id", "sections": "id", "claims": "id", "unit_summaries": "unit_id"}

type _CellValue = str | int | float | bool | tuple[object, ...] | None
type _DumpedRow = tuple[tuple[str, _CellValue], ...]


async def _dump_table(engine: AsyncEngine, table: str) -> list[_DumpedRow]:
    """Dump every row of `memory.<table>` as hashable `(column, value)` tuples, volatile columns dropped.

    Selects the mapped table's non-volatile columns (`_VOLATILE_COLUMNS` removed) in name order and
    renders each row as a tuple of `(column_name, normalized_value)` pairs. The rows are hashable
    (every cell is normalized to a scalar or nested tuple), so `assert_db_equal` compares them as a
    `Counter` multiset — order-independent and multiplicity-exact. Reads through a plain
    `Connection` (no ORM identity map) so the comparison sees exactly what is persisted.
    """
    model = _TABLES[table]
    dropped = _VOLATILE_COLUMNS | ({"id"} if table in _SURROGATE_ID_TABLES else frozenset())
    columns = sorted(column.name for column in model.__table__.columns if column.name not in dropped)
    statement = select(*(model.__table__.c[name] for name in columns))
    async with engine.connect() as conn:
        result = await conn.execute(statement)
        return [tuple((name, _normalize(value)) for name, value in zip(columns, row, strict=True)) for row in result.all()]


def _normalize(value: object) -> _CellValue:
    """Render a dumped cell deterministically + hashably: arrays/lists → tuples, JSON dicts → sorted items.

    Postgres `text[]` arrays decode to `list` and `jsonb` to `dict`; both are unhashable (and a
    dict's iteration order is not guaranteed), so a list becomes a `tuple` (order-preserving — array
    column order is deterministic from the loader) and a dict its sorted-key items tuple. Scalars
    (`str` / `int` / `float` / `bool` / `None`) pass through unchanged. Equality of two normalized
    rows is then exactly equality of the underlying served data.
    """
    if isinstance(value, (list, tuple)):
        return tuple(_normalize(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((str(key), _normalize(item)) for key, item in value.items()))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


async def assert_db_equal(engine_a: AsyncEngine, engine_b: AsyncEngine) -> None:
    """Assert databases A and B hold byte-identical served data across every `memory.*` data table.

    Dumps each table in `_DATA_TABLES` from both databases (sorted, volatile columns dropped) and
    asserts row-for-row equality. A failure names the offending table and shows the rows present
    on only one side, so an incremental-vs-full discrepancy is diagnosable from the assertion
    message alone.
    """
    for table in _DATA_TABLES:
        count_a = Counter(await _dump_table(engine_a, table))
        count_b = Counter(await _dump_table(engine_b, table))
        if count_a != count_b:
            only_a = list((count_a - count_b).elements())
            only_b = list((count_b - count_a).elements())
            msg = (
                f"memory.{table} differs between incremental (A) and full (B):\n"
                f"  rows in A not matched by B ({len(only_a)}): {only_a[:5]}\n"
                f"  rows in B not matched by A ({len(only_b)}): {only_b[:5]}"
            )
            raise AssertionError(msg)


# -- the scenario matrix -----------------------------------------------------


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=[scenario.id for scenario in _SCENARIOS])
async def test_incremental_equals_full(scenario: _Scenario, bed: _Bed, tmp_path: Path) -> None:
    """An incremental index of an edited knowledge base equals a full rebuild from scratch (the oracle).

    For `scenario`: A is built to a baseline by a full index, the edit is applied to A's knowledge base,
    and an **incremental** index runs (A holds the incremental result). B is the oracle — its
    knowledge base is the baseline with the same edit *already applied*, indexed **once, full, from
    scratch** into its own fresh database (B holds the from-scratch full rebuild of the final knowledge base
    state). The two databases must then be byte-identical and agree on the whole knowledge base
    `ValidationReport`. The no-op scenario additionally asserts the incremental run skipped every tracked
    file and re-embedded / pruned nothing.

    B is deliberately a *single* full index of the post-edit knowledge base, not a baseline index
    followed by a second full one: a second full index would conflate the incremental==full
    property with full-path idempotency, a separate concern.
    """
    knowledge_base_a = _baselined_knowledge_base(tmp_path / "a")
    await _index(bed.a_engine, knowledge_base_a, full=True)
    await _seed_derived_rows(bed.a_engine, scenario=scenario, final_state=False)
    scenario.apply_edit(knowledge_base_a)
    incremental = await _index(bed.a_engine, knowledge_base_a, full=False)

    knowledge_base_b = _baselined_knowledge_base(tmp_path / "b")
    scenario.apply_edit(knowledge_base_b)
    full = await _index(bed.b_engine, knowledge_base_b, full=True)
    await _seed_derived_rows(bed.b_engine, scenario=scenario, final_state=True)

    assert incremental.mode == "incremental", f"scenario {scenario.id!r} did not take the incremental path"
    assert full.mode == "full"

    await _assert_rows_gone(bed.a_engine, scenario)
    await assert_db_equal(bed.a_engine, bed.b_engine)
    await _assert_validation_equal(scenario, bed)
    await _assert_standing_validation_reported(scenario, incremental, bed)

    if scenario.id == "no-op":
        await _assert_noop(incremental, bed)


def _baselined_knowledge_base(root: Path) -> Path:
    """Create `root` and lay down the shared baseline knowledge base under it."""
    root.mkdir(parents=True, exist_ok=True)
    _build_baseline(KnowledgeBaseBuilder(root))
    return root


async def _assert_validation_equal(scenario: _Scenario, bed: _Bed) -> None:
    """Assert A and B agree on the whole knowledge base `ValidationReport` (the CI `n_errors` gate is identical).

    Read back through the production `ValidationIssues.whole_report` from each post-index
    database: the incremental path (A) reassembles the whole knowledge base report from stored-clean ∪
    recomputed-dirty issues, the full path (B) rewrote every page's issues. Their error/warning
    issue sets must match exactly — the validation half of the incremental==full contract.
    """
    report_a = await _whole_validation_report(bed.a_engine)
    report_b = await _whole_validation_report(bed.b_engine)
    assert _validation_key(report_a) == _validation_key(report_b), f"scenario {scenario.id!r}: whole knowledge base ValidationReport differs (A vs B)"


async def _assert_standing_validation_reported(scenario: _Scenario, incremental: IndexReport, bed: _Bed) -> None:
    """Assert the incremental report carries every issue stored for the collection once its transaction committed."""
    assert incremental.standing_validation is not None, f"scenario {scenario.id!r}: the incremental report read back no standing validation"
    stored = await _whole_validation_report(bed.a_engine)
    assert _validation_key(incremental.standing_validation) == _validation_key(stored), (
        f"scenario {scenario.id!r}: the standing validation differs from the stored issues"
    )


def _validation_key(report: ValidationReport) -> tuple[frozenset[tuple[str, ...]], frozenset[tuple[str, ...]]]:
    """A set-equality key for a `ValidationReport`: its error and warning issues as content tuples.

    Issue identity is content, not list order (the two paths can assemble issues in different
    orders), so each issue collapses to a sorted-insensitive `(page_id, rule, severity, message,
    anchor)` tuple and the two severities become frozensets — equal iff the same findings exist.
    """
    return (_issue_set(report.errors), _issue_set(report.warnings))


def _issue_set(issues: Sequence[object]) -> frozenset[tuple[str, ...]]:
    """Render validation issues to a frozenset of their `(page_id, rule, severity, message, anchor)` tuples."""
    return frozenset(
        (
            str(getattr(issue, "page_id", "")),
            str(getattr(issue, "rule", "")),
            str(getattr(issue, "severity", "")),
            str(getattr(issue, "message", "")),
            str(getattr(issue, "anchor", "") or ""),
        )
        for issue in issues
    )


async def _assert_noop(incremental: IndexReport, bed: _Bed) -> None:
    """Assert the no-op reindex skipped every tracked file and pruned / re-embedded nothing.

    A reindex with no knowledge base change must touch nothing: `skipped` equals the file count the
    manifest tracks (every note + source mirror — the dirty set is empty, so `skipped =
    manifest_size`), and both `pruned` and `embedded` are zero. The tracked-file count is read
    from `kb.index_manifest` on the incremental database.
    """
    tracked = await _manifest_count(bed.a_engine)
    assert incremental.embedded == 0, f"no-op re-embedded {incremental.embedded} units"
    assert incremental.pruned == 0, f"no-op pruned {incremental.pruned} rows"
    assert incremental.skipped == tracked, f"no-op skipped {incremental.skipped}, expected all {tracked} tracked files"


async def _assert_rows_gone(engine: AsyncEngine, scenario: _Scenario) -> None:
    """Assert the incremental database no longer holds the exact keys `scenario` declares stale."""
    for table, row_id in scenario.stale_ids:
        model = _TABLES[table]
        key = model.__table__.c[_STALE_KEY_COLUMNS[table]]
        statement = select(func.count()).select_from(model).where(model.__table__.c.collection == _COLLECTION, key == row_id)
        async with engine.connect() as conn:
            remaining = await conn.scalar(statement) or 0
        assert remaining == 0, f"scenario {scenario.id!r}: memory.{table} still holds {row_id!r} after the incremental index"


async def _dump_collection(engine: AsyncEngine, collection: str) -> dict[str, Counter[_DumpedRow]]:
    """Dump every data table of `engine`, keeping only the rows belonging to `collection`."""
    dumps: dict[str, Counter[_DumpedRow]] = {}
    for table in _DATA_TABLES:
        column = "src_collection" if table == "edges" else "collection"
        rows = await _dump_table(engine, table)
        dumps[table] = Counter(row for row in rows if (column, collection) in row)
    return dumps


async def _summary_unit_ids(engine: AsyncEngine, collection: str = _COLLECTION) -> set[str]:
    """Every `kb.unit_summaries.unit_id` stored for `collection`, read outside the ORM identity map."""
    summaries = orm.UnitSummaryORM.__table__.c
    statement = select(summaries.unit_id).where(summaries.collection == collection)
    async with engine.connect() as conn:
        result = await conn.execute(statement)
    return {str(unit_id) for (unit_id,) in result.all()}


async def test_a_full_index_prunes_only_the_summaries_whose_unit_is_gone(bed: _Bed, tmp_path: Path) -> None:
    """The full path's summary prune, met by a summary table that is NOT empty when the index starts.

    The scenario matrix seeds its oracle's derived rows after the full index has run, so this is the
    one case where the full path's own `prune_missing` has any stored summary to weigh. A summary
    has no foreign key to the unit it describes, so nothing but this prune removes one.
    """
    knowledge_base = _baselined_knowledge_base(tmp_path / "full")
    kept_page = "paper.lightrag-2025"
    kept_section = "ent.sections-demo#mechanism.graph_indexing"
    gone = "paper.withdrawn-2024"
    await _insert(
        bed.a_engine,
        orm.UnitSummaryORM,
        [
            _summary_row(_COLLECTION, kept_page, "page"),
            _summary_row(_COLLECTION, kept_section, "section"),
            _summary_row(_COLLECTION, gone, "page"),
        ],
    )

    report = await _index(bed.a_engine, knowledge_base, full=True)

    assert report.mode == "full"
    assert await _summary_unit_ids(bed.a_engine) == {kept_page, kept_section}


async def test_an_incremental_index_leaves_a_second_collection_with_colliding_ids_untouched(bed: _Bed, tmp_path: Path) -> None:
    """A neighbour collection holding the very same ids is not a candidate for any reconcile DELETE.

    The shadow knowledge base is a copy of the baseline, so every page, section and claim id it
    produces collides with the collection being reindexed. Removing a section and a claim from the
    indexed knowledge base must leave the shadow's identically-keyed rows exactly as they were.
    """
    knowledge_base_a = _baselined_knowledge_base(tmp_path / "a")
    await _index(bed.a_engine, knowledge_base_a, full=True)
    shadow = _baselined_knowledge_base(tmp_path / "shadow")
    await _index(bed.a_engine, shadow, full=True, collection=_SHADOW_COLLECTION)
    await _insert(bed.a_engine, orm.UnitSummaryORM, _summary_rows(_SHADOW_COLLECTION, [unit for unit, _kind in _SEEDED_SUMMARIES]))
    before = await _dump_collection(bed.a_engine, _SHADOW_COLLECTION)

    _edit_remove_h3(knowledge_base_a)
    _edit_remove_claim_anchor(knowledge_base_a)
    incremental = await _index(bed.a_engine, knowledge_base_a, full=False)

    assert incremental.mode == "incremental"
    assert incremental.pruned > 0, "the edit must have reconciled at least one child row away"
    assert await _dump_collection(bed.a_engine, _SHADOW_COLLECTION) == before


async def _manifest_count(engine: AsyncEngine) -> int:
    """The number of files `kb.index_manifest` tracks for the test collection (the no-op skip target)."""
    statement = select(func.count()).select_from(orm.IndexManifestORM).where(orm.IndexManifestORM.collection == _COLLECTION)
    async with engine.connect() as conn:
        return await conn.scalar(statement) or 0


# -- the --no-embed-leftover backfill oracle ---------------------------------
#
# `assert_db_equal` deliberately excludes the `embedding` column (halfvec is not bit-stable across
# two inserts), so these helpers assert the vector *presence* the byte-equality oracle cannot: after
# the widening backfill every embedded row must carry a vector; after a `--no-embed` run none may.

# The four embedded aggregates (each carries a nullable `embedding` halfvec column).
_EMBEDDED_TABLES: dict[str, type[orm.KbBase]] = {
    "pages": orm.PageORM,
    "sections": orm.SectionORM,
    "claims": orm.ClaimORM,
    "raw_chunks": orm.RawChunkORM,
}


async def _embedding_null_counts(engine: AsyncEngine) -> dict[str, tuple[int, int]]:
    """Per embedded table, `(rows_with_null_embedding, rows_with_a_vector)` (the backfill oracle)."""
    counts: dict[str, tuple[int, int]] = {}
    async with engine.connect() as conn:
        for name, model in _EMBEDDED_TABLES.items():
            column = model.__table__.c["embedding"]
            null_n = await conn.scalar(select(func.count()).select_from(model).where(column.is_(None)))
            set_n = await conn.scalar(select(func.count()).select_from(model).where(column.is_not(None)))
            counts[name] = (null_n or 0, set_n or 0)
    return counts


async def test_no_embed_then_incremental_backfills_every_embedding(bed: _Bed, tmp_path: Path) -> None:
    """A `--no-embed` index then a plain incremental index equals a full-with-embeddings rebuild.

    A is indexed full with `--no-embed` (every row lands with a NULL embedding), then a plain
    incremental index runs. Its widening pre-pass folds
    every file owning a leftover NULL row into the dirty set, so the same load path as a full index
    rebuilds every embed input and the embed stage backfills every vector — no row is left NULL and
    no empty string ever reaches the embedder (:class:`FakeEmbedding` asserts non-empty inputs). B is
    the oracle: a single full index with embeddings. A and B must be byte-identical on every served
    table, and A must carry a non-NULL embedding on every embedded row (including the manifest-less
    `meta.about` page, which the widening re-reads directly).
    """
    knowledge_base_a = _baselined_knowledge_base(tmp_path / "a")
    await _index(bed.a_engine, knowledge_base_a, full=True, embed=False)
    incremental = await _index(bed.a_engine, knowledge_base_a, full=False)

    knowledge_base_b = _baselined_knowledge_base(tmp_path / "b")
    full = await _index(bed.b_engine, knowledge_base_b, full=True)

    assert incremental.mode == "incremental", "the backfill run must take the incremental path"
    assert full.mode == "full"

    await assert_db_equal(bed.a_engine, bed.b_engine)
    for table, (null_n, set_n) in (await _embedding_null_counts(bed.a_engine)).items():
        assert null_n == 0, f"memory.{table} still has {null_n} NULL embeddings after the incremental backfill"
        assert set_n > 0, f"memory.{table} has no embedded rows after the backfill (expected the widening to load some)"


async def test_no_embed_incremental_leaves_nulls_and_skips_widening(bed: _Bed, tmp_path: Path) -> None:
    """A `--no-embed` incremental index embeds nothing and leaves every NULL embedding untouched.

    The embed flag is honored on the incremental path: with `--no-embed` the widening
    pre-pass and the embed stage are both skipped. An index that follows a `--no-embed` full build
    with a second `--no-embed` run therefore reports zero embedded units and backfills nothing —
    every embedded row keeps its NULL embedding.
    """
    knowledge_base_a = _baselined_knowledge_base(tmp_path / "a")
    await _index(bed.a_engine, knowledge_base_a, full=True, embed=False)
    incremental = await _index(bed.a_engine, knowledge_base_a, full=False, embed=False)

    assert incremental.mode == "incremental", "a matching fingerprint + manifest must route incremental"
    assert incremental.embedded == 0
    for table, (_null_n, set_n) in (await _embedding_null_counts(bed.a_engine)).items():
        assert set_n == 0, f"memory.{table} has {set_n} embedded rows after a --no-embed index (widening should have been skipped)"
