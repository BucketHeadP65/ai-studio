"""Removing pages that claims cite, through the full and the incremental index, over a real migrated database.

A claim's `subject` and each claim qualifier point at a page through foreign keys with no cascade. One note's claim
names a topic page as its subject and in a link field. Removing the note's file and the topic's file together, then
indexing, must leave no page, claim or qualifier of either. Removing only the topic's file must leave the citing
note's claim in place with its subject unset and no qualifier row for the gone topic. The database tests skip when
Docker is unavailable (the `migrated` fixture raises `pytest.skip`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine

from engram.indexing.contracts import (
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    SqlAlchemyKnowledgeBaseUnitOfWork,
)
from engram.indexing.infrastructure import orm
from engram.init.contracts import scaffold
from engram.kb.contracts import CanonicalSection, ClaimGrammarSpec, KnowledgeBaseSchema, PageTypeSpec, VocabEntry
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder

if TYPE_CHECKING:
    from pathlib import Path

    from engram.indexing.contracts import IndexReport

_COLLECTION = "cited-pages"


def _note(stem: str) -> str:
    """A note whose one claim names `topic.alpha` as its subject and links `topic.alpha` and `topic.beta` in `applies_to`."""
    return f"""---
id: note.{stem}
type: note
title: {stem.title()}
status: draft
---

## Findings

- Alpha shapes the outcome. ^c1
    - loc:: §1
    - kind:: observation
    - subject:: [[alpha]]
    - applies_to:: [[alpha]], [[beta]]
"""


def _topic(stem: str) -> str:
    """A claimless topic page a claim cites."""
    return f"---\nid: topic.{stem}\ntype: topic\ntitle: {stem.title()}\nstatus: draft\n---\n"


def _spec() -> KnowledgeBaseSchema:
    """Notes whose claims carry a subject and an `applies_to` link field, and topic pages they cite."""
    findings = (CanonicalSection(slug="findings"),)
    return KnowledgeBaseSchema(
        slug=_COLLECTION,
        page_types=(
            PageTypeSpec(page_type="note", id_prefix="note.", canonical_sections=findings, claims_enabled=True),
            PageTypeSpec(page_type="topic", id_prefix="topic.", canonical_sections=findings),
        ),
        claim_types=(VocabEntry(value="observation", definition="Something observed"),),
        claims=ClaimGrammarSpec(section="findings", multilink_fields=("applies_to",)),
    )


def _knowledge_base(root: Path, spec: KnowledgeBaseSchema, *, notes: tuple[str, ...]) -> dict[str, Path]:
    """Write the topics `alpha` and `beta`, the given notes, the scaffolded vocabulary and the orientation stubs, answering each page's file by id."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema(spec)
    for relative, body in scaffold(spec).items():
        if relative.startswith("_meta/vocabulary/"):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(body, encoding="utf-8")
    files = {f"topic.{stem}": builder.add_unit_document("topic", stem, _topic(stem)) for stem in ("alpha", "beta")}
    files.update({f"note.{stem}": builder.add_unit_document("note", stem, _note(stem)) for stem in notes})
    return files


async def _index(knowledge_base: Path, migrated: str, spec: KnowledgeBaseSchema, *, full: bool) -> IndexReport:
    """Run one index of `knowledge_base` into the migrated database, a full rebuild when `full`."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        handler = IndexKnowledgeBaseHandler(
            FilesystemKnowledgeBaseReader(knowledge_base),
            FakeEmbedding(),
            lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine),
            FakeValidationReportSink(),
            spec=spec,
        )
        return await handler.handle(IndexKnowledgeBase(collection=_COLLECTION, embed=False, full=full))
    finally:
        await engine.dispose()


async def _rows(migrated: str, sql: str) -> list[tuple[str, ...]]:
    """The rows `sql` answers for the collection, each as a tuple of strings, sorted."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(sql), {"collection": _COLLECTION})
            return sorted(tuple(str(value) for value in row) for row in result.all())
    finally:
        await engine.dispose()


async def _page_ids(migrated: str) -> list[tuple[str, ...]]:
    return await _rows(migrated, "SELECT id FROM kb.pages WHERE collection = :collection AND (id LIKE 'note.%' OR id LIKE 'topic.%')")


async def _claims(migrated: str) -> list[tuple[str, ...]]:
    return await _rows(migrated, "SELECT id, coalesce(subject_id, '') FROM kb.claims WHERE collection = :collection")


async def _qualifiers(migrated: str) -> list[tuple[str, ...]]:
    return await _rows(migrated, "SELECT claim_id, target_id FROM kb.claim_qualifiers WHERE collection = :collection")


class TestRemovingACitingNoteAndTheTopicItCites:
    @pytest.mark.parametrize("full", [True, False], ids=["full", "incremental"])
    async def test_the_index_removes_both_pages_their_claims_and_their_qualifiers(self, tmp_path: Path, migrated: str, full: bool) -> None:
        spec = _spec()
        files = _knowledge_base(tmp_path, spec, notes=("citing",))
        await _index(tmp_path, migrated, spec, full=True)
        assert await _claims(migrated) == [("note.citing#c1", "topic.alpha")]
        assert await _qualifiers(migrated) == [("note.citing#c1", "topic.alpha"), ("note.citing#c1", "topic.beta")]

        files["note.citing"].unlink()
        files["topic.alpha"].unlink()
        report = await _index(tmp_path, migrated, spec, full=full)

        assert report.mode == ("full" if full else "incremental")
        assert await _page_ids(migrated) == [("topic.beta",)]
        assert await _claims(migrated) == []
        assert await _qualifiers(migrated) == []


class TestRemovingOnlyTheCitedTopic:
    @pytest.mark.parametrize("full", [True, False], ids=["full", "incremental"])
    async def test_the_citing_claim_stays_with_its_subject_unset_and_no_qualifier_for_the_gone_topic(
        self, tmp_path: Path, migrated: str, full: bool
    ) -> None:
        spec = _spec()
        files = _knowledge_base(tmp_path, spec, notes=("keeper",))
        await _index(tmp_path, migrated, spec, full=True)

        files["topic.alpha"].unlink()
        report = await _index(tmp_path, migrated, spec, full=full)

        assert report.mode == ("full" if full else "incremental")
        assert await _page_ids(migrated) == [("note.keeper",), ("topic.beta",)]
        assert await _claims(migrated) == [("note.keeper#c1", "")]
        assert await _qualifiers(migrated) == [("note.keeper#c1", "topic.beta")]
