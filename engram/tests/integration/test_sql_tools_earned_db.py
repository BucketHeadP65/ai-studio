"""Integration: `embedded_knowledge_base` seeds `Earned` from the knowledge base's real stored `sql` switch.

Reads go through the `reader_url` fixture (the `kb_reader` role, `migrated` underneath). An
auto-mode collection with no fingerprint row yet (the state every collection starts in, before any
index has run) starts dark -- `api.retrieval_switches` answers no row, `switch_source` reads
that as every switch off. A collection whose stored `sql` switch is already on at open time seeds
`Earned` earned from its very first resolve, no flip needed. The flip-then-refresh path itself is
covered at the unit tier (`tests/unit/engram/retrieval/test_earned.py::TestSwitchCache`) with a
controllable fake read, deterministically, rather than raced here against a background task.

Skips the whole module when Docker is unavailable (via the `migrated` fixture).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engram.indexing.contracts import RetrievalSwitches
from engram.indexing.infrastructure.repository import IndexFingerprints
from engram.kb.domain.schema import RetrievalSpec
from engram.retrieval.earned import Earned
from engram.retrieval.infrastructure.embedded import embedded_knowledge_base
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from engram.kb.contracts import KnowledgeBaseSchema

UNDECIDED = "earnedtest-undecided"
"""A collection no knowledge base indexes, with no fingerprint row ever written for it."""

ALREADY_EARNED = "earnedtest-already-on"
"""A collection whose stored `sql` switch is flipped on before the knowledge base is ever opened."""


def _auto_spec(collection: str) -> KnowledgeBaseSchema:
    return research_spec().model_copy(update={"retrieval": RetrievalSpec(mode="auto", collection=collection)})


def _ctx() -> RunContext[object]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


async def _seed_switches(migrated: str, *, collection: str, switches: RetrievalSwitches) -> None:
    """Seed a fingerprint row then set its stored switches."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await IndexFingerprints(db).set(
                collection=collection,
                indexer_version="0",
                spec_hash="seed",
                vocabulary_hash="seed",
                embed_model="seed",
                knowledge_base_root="",
            )
            await IndexFingerprints(db).set_switches(collection=collection, switches=switches)
            await db.commit()
    finally:
        await engine.dispose()


class TestEmbeddedKnowledgeBaseSeedsEarnedFromTheStoredSwitch:
    async def test_an_undecided_collection_opens_dark(self, reader_url: str) -> None:
        async with embedded_knowledge_base(reader_url, domain="a test knowledge_base", spec=_auto_spec(UNDECIDED)) as capability:
            assert isinstance(capability, Earned)
            resolved = capability.capability_func(_ctx())
            assert resolved is None

    async def test_a_collection_already_earning_sql_opens_earned(self, migrated: str, reader_url: str) -> None:
        await _seed_switches(migrated, collection=ALREADY_EARNED, switches=RetrievalSwitches(sql=True))

        async with embedded_knowledge_base(reader_url, domain="a test knowledge_base", spec=_auto_spec(ALREADY_EARNED)) as capability:
            assert isinstance(capability, Earned)
            resolved = capability.capability_func(_ctx())
            assert resolved is not None
            assert resolved is capability.wrapped
