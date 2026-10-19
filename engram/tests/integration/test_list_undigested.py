r"""`list_undigested` over a real, migrated Postgres, fed by a real index.

`digest_of` is a frontmatter field only, backed by no typed column: `kb.pages.source_id` is
populated from the page's value under the spec's declared `sources.key_field`, never from
`digest_of`. So `list_undigested`'s join through `p.frontmatter->>'digest_of'` depends entirely on
how the indexer's own long-tail frontmatter projection stores a scalar field. It does so
unchanged: a plain `str`/`int`/`float`/`bool` value passes straight through
`_jsonable_frontmatter_value` into `_frontmatter_payload`, the dict `add_page` stores as
`pages.frontmatter` -- only a `UnitLink`-shaped ("link"-typed) field is dropped there instead,
because it already lives in `kb.edges`. `digest_of` is declared `"scalar"` for every page_type in
the research template's schema, never `"link"`, so it survives that projection.

This test proves the join against the real thing rather than a hand-seeded row: a fixture knowledge base
with two `Sources/` mirrors is indexed through the real five-stage `IndexKnowledgeBaseHandler`
(real `FilesystemKnowledgeBaseReader`, real `SqlAlchemyKnowledgeBaseUnitOfWork` over the `migrated` admin URL,
`FakeEmbedding` standing in only for the network call -- embeddings stay on, `IndexKnowledgeBase(embed=True)`)
after one of the two mirrors is linked to a `talk` digest note written through the real `NoteWriter`.
`list_undigested`, read through the production `ReadOnlySqlGateway`, must then return the other
mirror and not the digested one, carrying the `item_type` its own mirror frontmatter declared:
`add_source` folds `item_type` into `sources_raw.meta` the same way as
`doi` / `arxiv_id` / `venue` / `year`, so the value a digest workflow step routes on (video, article,
post, paper) survives from the mirror all the way to `list_undigested`'s answer.

Skips when Docker is unavailable (the `migrated` fixture raises `pytest.skip`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import create_async_engine

from engram.authoring.contracts import AuthoringKnowledgeBase, AuthoringTools, NoteWriter
from engram.indexing.contracts import (
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    SqlAlchemyKnowledgeBaseUnitOfWork,
)
from engram.indexing.infrastructure import orm
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.sources.contracts import SourceQueue, SourceType, claimants_for
from engram.testing.fakes.authoring import FakeReindex
from engram.testing.fakes.connector import FakeConnector
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from pathlib import Path

_COLLECTION = "research"

_PROVIDERS = {"youtube": FakeConnector(name="youtube", source_type_from=SourceType)}
"""A fake standing in for the real `youtube` provider: engram's own `SourceType`
values fold to themselves, the same identity every real connector that writes its own page type directly
uses, never a real connector's own vocabulary. Neither this test's own venv nor engram's carries the
`providers` package, so `source_type_of`/`claimants_for` need this in place of the real vocabulary."""

_UNDIGESTED_SOURCE_ID = "yt-undigested"
_DIGESTED_SOURCE_ID = "yt-digested"
_SECOND_UNDIGESTED_SOURCE_ID = "yt-second-undigested"

_MIRROR_SECOND_UNDIGESTED = """\
---
source_key: yt-second-undigested
source_hash: h-second-undigested
provider: youtube
item_type: video
title: A second talk with no digest yet
---

# A second talk with no digest yet

More transcript text with no digest note pointing at it.
"""

_MIRROR_UNDIGESTED = """\
---
source_key: yt-undigested
source_hash: h-undigested
provider: youtube
item_type: video
title: A talk with no digest yet
---

# A talk with no digest yet

Some transcript text with no digest note pointing at it.
"""

_MIRROR_DIGESTED = """\
---
source_key: yt-digested
source_hash: h-digested
provider: youtube
item_type: video
title: Agents that plan
---

# Agents that plan

Some transcript text about planning agents that keep a task list.
"""

_TALK_DIGEST = {
    "frontmatter": {
        "id": "talk.agents-that-plan",
        "type": "talk",
        "title": "Agents that plan",
        "summary": "A talk on planning agents whose task list survives compaction.",
        "status": "active",
        "digest_of": _DIGESTED_SOURCE_ID,
    },
    "sections": [
        {"heading": "TL;DR", "body": "Planning agents keep a task list the model owns."},
        {"heading": "Findings", "body": ""},
        {"heading": "Entities", "body": ""},
    ],
}


def _build_knowledge_base(tmp_path: Path) -> Path:
    """A fixture knowledge_base: two source mirrors, and a `talk` digest note (via `NoteWriter`) linking one."""
    builder = KnowledgeBaseBuilder(tmp_path)
    builder.build_minimal()
    builder.add_source_mirror("undigested-talk", _MIRROR_UNDIGESTED)
    builder.add_source_mirror("digested-talk", _MIRROR_DIGESTED)
    knowledge_base = AuthoringKnowledgeBase(name="research", collection=_COLLECTION, path=tmp_path, schema=research_spec())
    NoteWriter(knowledge_base).write(_TALK_DIGEST)
    return tmp_path


async def _compile(knowledge_base: Path, db_url: str) -> None:
    """Run the real `IndexKnowledgeBaseHandler` (real reader, real UoW, fake embedder) over `knowledge_base`."""
    spec = research_spec()
    engine = create_async_engine(db_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        reader = FilesystemKnowledgeBaseReader(knowledge_base, "Sources", note_folders=spec.note_folders, note_files=spec.note_files)
        handler = IndexKnowledgeBaseHandler(reader, FakeEmbedding(), lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine), FakeValidationReportSink(), spec=spec)
        await handler.handle(IndexKnowledgeBase(collection=_COLLECTION, embed=True))
    finally:
        await engine.dispose()


async def _undigested(db_url: str, target_path: Path, *, limit: int = 40, source_ids: list[str] | None = None) -> list[dict[str, str]]:
    """Run `AuthoringTools.list_undigested` through the production admin gateway over `db_url`."""
    engine = create_async_engine(db_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        knowledge_base = AuthoringKnowledgeBase(name="research", collection=_COLLECTION, path=target_path, schema=research_spec())
        doors = AuthoringTools(
            knowledge_bases={"research": knowledge_base},
            reindex=FakeReindex(),
            admin=ReadOnlySqlGateway(engine),
            queue_for=lambda t: SourceQueue(t.path / "_meta" / "source-queue.yaml", connectors=claimants_for(t.schema.sources.connectors, source=_PROVIDERS)),
            connectors=_PROVIDERS,
        )
        return await doors.list_undigested("research", limit=limit, source_ids=source_ids)
    finally:
        await engine.dispose()


class TestListUndigestedAgainstARealCompile:
    async def test_the_digested_mirror_is_excluded_and_the_other_one_is_returned(self, migrated: str, tmp_path: Path) -> None:
        knowledge_base = _build_knowledge_base(tmp_path)

        await _compile(knowledge_base, migrated)
        rows = await _undigested(migrated, knowledge_base)

        assert [row["source_id"] for row in rows] == [_UNDIGESTED_SOURCE_ID]
        assert rows[0]["provider"] == "youtube"
        # `add_source` folds the mirror's own `item_type` into `sources_raw.meta`, so the value a
        # digest workflow step routes on survives a real index, not an invented one.
        assert rows[0]["item_type"] == "video"

    async def test_picked_ids_answer_every_undigested_one_among_them_past_the_limit(self, migrated: str, tmp_path: Path) -> None:
        knowledge_base = _build_knowledge_base(tmp_path)
        KnowledgeBaseBuilder(knowledge_base).add_source_mirror("second-undigested-talk", _MIRROR_SECOND_UNDIGESTED)

        await _compile(knowledge_base, migrated)
        picked = [_SECOND_UNDIGESTED_SOURCE_ID, _DIGESTED_SOURCE_ID, _UNDIGESTED_SOURCE_ID, "not-a-mirror"]
        rows = await _undigested(migrated, knowledge_base, limit=1, source_ids=picked)

        assert sorted(row["source_id"] for row in rows) == sorted([_UNDIGESTED_SOURCE_ID, _SECOND_UNDIGESTED_SOURCE_ID])
