r"""End-to-end authoring round-trip: payload JSON → built note → indexed rows — Docker-gated.

The other side of the vertical slice from `test_index_and_search` (which seeds from
pre-baked golden markdown): here the note is *authored through the CLI* from an extractor
payload, then ingested. The whole path runs through the public `studio` commands:

1. an authoring payload (modelled on the golden LightRAG benchmark claim, carrying a quote
   that appears verbatim in the golden mirror excerpt) →
2. `studio note build --payload … --out <knowledge_base>/Wiki/Papers/built.md --verify-quotes` against a
   :class:`~engram.testing.knowledge_base_builder.KnowledgeBaseBuilder` knowledge base whose ``Sources/`` holds that mirror
   excerpt → the build verifies every quote against the mirror *before* writing, exits 0, and the
   file exists.
3. `studio note validate` over the built note passes (parse + registry vocabulary clean).
4. `studio index` ingests that same knowledge base into the freshly-migrated ParadeDB (real PG
   Unit-of-Work, :class:`FakeEmbedding` the only fake) with **no validation errors**, and the built
   note's page row + its claim row land (their ids are present in `kb.pages` / `kb.claims`).

The module skips when Docker is unavailable (the `migrated` fixture, via `pg`, raises
`pytest.skip`), so `uv run pytest` stays green on a Docker-less machine. The build/validate
steps are pure (no DB), but the assertion that the authored note *indexes and lands* needs the
real database — so the whole round-trip is gated together.
"""

from __future__ import annotations

import asyncio
import json

from typing import TYPE_CHECKING

import pytest

from core.cqrs.contracts import JobQueries, MemoryJobs, MessageBus
from engram.indexing.contracts import FilesystemKnowledgeBaseReader, IndexKnowledgeBase, IndexKnowledgeBaseHandler, KnowledgeBaseReader
from engram.indexing.infrastructure import orm
from engram.kb.contracts import load_knowledge_base_schema
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.knowledge_base_builder import GOLDEN, KnowledgeBaseBuilder
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine
from typer.testing import CliRunner

from studio.knowledge_base.cli import app
from studio.knowledge_base.indexing.infrastructure.validation_report_sink import FileValidationReportSink
from studio.knowledge_base.settings import KnowledgeBaseSettings
from studio.knowledge_base.wiring.contracts import AdminSqlGateway
from studio.knowledge_base.wiring.indexing import KnowledgeBaseUnitOfWorkFactory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine
    from pathlib import Path

    from pydantic import JsonValue
    from sqlalchemy.ext.asyncio import AsyncEngine

runner = CliRunner()

# The golden mirror excerpt carries this as its `source_key`; the built paper note's own
# `zotero_key` shares the value so quote verification (build + index) resolves the mirror, and
# the FK `pages.zotero_key` is satisfied.
# The golden knowledge base fixtures ship with the library as package data.
_GOLDEN = GOLDEN
_MIRROR_EXCERPT = (_GOLDEN / "mirror-excerpt.md").read_text(encoding="utf-8")
_ZOTERO_KEY = "8QXFXYMI"
# The `source_hash` the golden mirror declares (pages.source_hash mirrors it).
_SOURCE_HASH = "bd72eebf611df1366b251c16ce4eac7aac699e519783c586feb2c5c43899347a"

# A passage present verbatim in the golden mirror excerpt — the authored claim's `quote::`.
_MIRROR_QUOTE = "LightRAG consistently outperforms GraphRAG, particularly in larger datasets with complex language contexts"

# The authored note's ids (the page id from the payload; the claim id is `<page>#<block>` per
# the resolver, with the block filled `c1` by `assign_anchors` since the payload omits anchors).
_PAGE_ID = "paper.built-roundtrip-2025"
_CLAIM_ID = f"{_PAGE_ID}#c1"

# The entity the paper proposes (so the page's `proposes` edge resolves, mirroring the
# index-and-search knowledge base) — keeps the indexed knowledge base validation-clean and self-contained.
_ENTITY = """\
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


def _payload() -> dict[str, JsonValue]:
    """An extractor authoring payload: a `benchmark-result` claim quoting the golden mirror.

    A `benchmark-result` kind requires a `quote` (validator rule 3) — supplied from the
    mirror — but carries no comparative edge, so it needs no dataset/metric qualifiers and the
    indexed note is validation-clean. `proposes` links the in-knowledge_base entity so that edge resolves.
    """
    frontmatter: dict[str, JsonValue] = {
        "id": _PAGE_ID,
        "type": "paper",
        "title": "Built Round-Trip",
        "summary": "An authored note built from a payload and ingested end to end.",
        "status": "synthesized",
        "paper_type": "method",
        "year": 2025,
        "zotero_key": _ZOTERO_KEY,
        "source_hash": _SOURCE_HASH,
        "tasks": ["long-doc-qa"],
        "stages": ["retrieval"],
        "proposes": ["[[lightrag]]"],
    }
    claim: dict[str, JsonValue] = {
        "text": "LightRAG wins on the large UltraDomain subsets.",
        "loc": "§3",
        "quote": _MIRROR_QUOTE,
        "kind": "benchmark-result",
    }
    return {
        "frontmatter": frontmatter,
        "sections": [{"heading": "TL;DR", "body": "An authored note ingested end to end."}],
        "claims": [claim],
    }


class _KnowledgeBaseContainer:
    """Resolves :class:`KnowledgeBaseSettings` for the note commands' ``ctx.obj`` seam (build/verify path)."""

    def __init__(self, knowledge_base: KnowledgeBaseSettings) -> None:
        self._knowledge_base = knowledge_base

    async def get(self, key: type) -> object:
        if key is KnowledgeBaseSettings:
            return self._knowledge_base
        msg = f"no registration for {key!r}"
        raise KeyError(msg)


def _knowledge_base_factory(knowledge_base: KnowledgeBaseSettings):
    """A zero-arg `ctx.obj` factory returning a knowledge base-resolving container."""

    def build() -> _KnowledgeBaseContainer:
        return _KnowledgeBaseContainer(knowledge_base)

    return build


class _IndexFactory:
    """A `ctx.obj` factory whose bus runs a real-PG, fake-embed index handler (e2e shape)."""

    def __init__(self, engine: AsyncEngine, knowledge_base: Path) -> None:
        self._reader = FilesystemKnowledgeBaseReader(knowledge_base, note_folders=load_knowledge_base_schema(knowledge_base).note_folders)
        handler = IndexKnowledgeBaseHandler(
            self._reader, FakeEmbedding(), KnowledgeBaseUnitOfWorkFactory(engine), FileValidationReportSink(knowledge_base), spec=load_knowledge_base_schema(knowledge_base)
        )
        self._bus = MessageBus({IndexKnowledgeBase: handler}, {})
        self._gateway = AdminSqlGateway(engine)  # the identity announce's own read, over the same admin engine

    def __call__(self) -> _IndexContainer:
        return _IndexContainer(self._bus, self._gateway, self._reader)


class _IndexContainer:
    """Minimal async container exposing the message bus and identity-announce reads to the `index` command."""

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


def _build_knowledge_base(root: Path) -> tuple[Path, KnowledgeBaseSettings]:
    """Lay down the knowledge base (entity + the golden mirror + registries + meta stubs); return root + settings.

    The `Wiki/Papers/` directory is created so `studio note build --out` (which writes
    atomically into an existing directory) can drop the authored paper note where the
    filesystem knowledge base reader will later index it.
    """
    builder = KnowledgeBaseBuilder(root)
    builder.add_unit_document("Wiki/Entities", "lightrag", _ENTITY)
    builder.add_source_mirror("lightrag-mirror", _MIRROR_EXCERPT)
    builder.add_vocabulary()
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema()
    (root / "Wiki" / "Papers").mkdir(parents=True, exist_ok=True)
    return root, KnowledgeBaseSettings.model_validate({"path": str(root)})


@pytest.fixture
async def engine(migrated: str) -> AsyncIterator[AsyncEngine]:
    """An engine over the freshly-migrated throwaway PG with the pgvector codec registered."""
    eng = create_async_engine(migrated, poolclass=NullPool)
    await orm.register_pgvector(eng)
    try:
        yield eng
    finally:
        await eng.dispose()


def _run[T](coro: Coroutine[object, object, T]) -> T:
    """Drive a coroutine to completion from a synchronous test body (no loop is running)."""
    return asyncio.run(coro)


def _scalar(engine: AsyncEngine, sql: str, params: dict[str, object]) -> object:
    """Run a single-value parametrised read from a synchronous test body via `asyncio.run`."""

    async def _go() -> object:
        async with engine.connect() as conn:
            return await conn.scalar(text(sql), params)

    return _run(_go())


class TestNoteBuildRoundtrip:
    # Synchronous, like the index e2e: `runner.invoke` drives Typer commands that call
    # `asyncio.run` internally, so the body must hold no running loop; DB readbacks wrap their
    # own `asyncio.run` via `_scalar` (legal — no loop is running in a sync test body).
    def test_authored_note_builds_validates_and_indexes(self, engine: AsyncEngine, tmp_path: Path):
        knowledge_base_root, knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        payload_path = tmp_path / "payload.json"
        payload_path.write_text(json.dumps(_payload()), encoding="utf-8")
        out = knowledge_base_root / "Wiki" / "Papers" / "built.md"

        # 1. Build the note from the payload, verifying quotes against the mirror before writing.
        built = runner.invoke(
            app, ["note", "build", "--payload", str(payload_path), "--out", str(out), "--verify-quotes", "--knowledge-base", str(knowledge_base_root)], obj=_knowledge_base_factory(knowledge_base)
        )
        assert built.exit_code == 0, built.stdout
        assert out.exists()

        # 2. The authored note validates clean (parse + registry vocabulary).
        validated = runner.invoke(app, ["note", "validate", str(out), "--knowledge-base", str(knowledge_base_root)], obj=_knowledge_base_factory(knowledge_base))
        assert validated.exit_code == 0, validated.stdout

        # 3. Index the knowledge base into the migrated DB — validation-clean (exit 0).
        indexed = runner.invoke(app, ["index", "--knowledge-base", str(knowledge_base_root)], obj=_IndexFactory(engine, knowledge_base_root))
        assert indexed.exit_code == 0, indexed.stdout

        # 4. The built note's page row + its claim row landed (ids present).
        assert _scalar(engine, "SELECT count(*) FROM kb.pages WHERE id = :id", {"id": _PAGE_ID}) == 1
        assert _scalar(engine, "SELECT count(*) FROM kb.claims WHERE id = :id", {"id": _CLAIM_ID}) == 1
        assert _scalar(engine, "SELECT quote FROM kb.claims WHERE id = :id", {"id": _CLAIM_ID}) == _MIRROR_QUOTE
