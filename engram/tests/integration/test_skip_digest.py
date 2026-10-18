"""`skip_digest` over a real, migrated Postgres, fed by a real index: the sibling lookup finds every page of one source.

Two talk pages digest `yt-abc` and one unrelated talk digests `yt-other`. Skipping through one page of `yt-abc`
must set both of its pages to skipped with the reason and leave the other source's page active.

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

_REASON = "Skipped: off purpose."

_MIRROR = """\
---
source_key: {key}
source_hash: h-{key}
provider: youtube
item_type: video
title: A talk
---

# A talk

Some transcript text about planning agents that keep a task list.
"""


def _talk(page_id: str, source_id: str) -> dict[str, object]:
    return {
        "frontmatter": {"id": page_id, "type": "talk", "title": "Agents that plan", "summary": "A talk on planning agents.", "status": "active", "digest_of": source_id},
        "sections": [{"heading": "TL;DR", "body": "Planning agents keep a task list."}, {"heading": "Findings", "body": ""}, {"heading": "Entities", "body": ""}],
    }


def _build_knowledge_base(tmp_path: Path) -> Path:
    builder = KnowledgeBaseBuilder(tmp_path)
    builder.build_minimal()
    builder.add_source_mirror("abc", _MIRROR.format(key="yt-abc"))
    builder.add_source_mirror("other", _MIRROR.format(key="yt-other"))
    writer = NoteWriter(AuthoringKnowledgeBase(name="research", collection=_COLLECTION, path=tmp_path, schema=research_spec()))
    writer.write(_talk("talk.agents-that-plan", "yt-abc"))
    writer.write(_talk("talk.agents-that-plan.methods", "yt-abc"))
    writer.write(_talk("talk.other-talk", "yt-other"))
    return tmp_path


async def _compile(knowledge_base: Path, db_url: str) -> None:
    spec = research_spec()
    engine = create_async_engine(db_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        reader = FilesystemKnowledgeBaseReader(knowledge_base, "Sources", spec.note_folders, note_files=spec.note_files)
        handler = IndexKnowledgeBaseHandler(reader, FakeEmbedding(), lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine), FakeValidationReportSink(), spec=spec)
        await handler.handle(IndexKnowledgeBase(collection=_COLLECTION, embed=True))
    finally:
        await engine.dispose()


async def _skip(db_url: str, target_path: Path, page_id: str) -> str:
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
        return await doors.skip_digest("research", page_id, _REASON)
    finally:
        await engine.dispose()


class TestSkipDigestAgainstARealCompile:
    async def test_every_page_of_the_source_is_skipped_and_another_source_is_left_alone(self, migrated: str, tmp_path: Path) -> None:
        knowledge_base = _build_knowledge_base(tmp_path)

        await _compile(knowledge_base, migrated)
        said = await _skip(migrated, knowledge_base, "talk.agents-that-plan.methods")

        assert said.startswith("2 digest pages of yt-abc are now skipped in research: talk.agents-that-plan, talk.agents-that-plan.methods")
        talks = knowledge_base / "Wiki" / "Talks"
        for stem in ("agents-that-plan", "agents-that-plan.methods"):
            text = (talks / f"{stem}.md").read_text(encoding="utf-8")
            assert "status: skipped" in text
            assert f"decision_reason: '{_REASON}'" in text
        assert "status: active" in (talks / "other-talk.md").read_text(encoding="utf-8")
