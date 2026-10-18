"""Index statements over sets larger than asyncpg's bind parameter limit, over a real migrated database.

asyncpg refuses a statement that binds more than 32,767 parameters. Each repository test hands one method a set past
that limit, a few real values among tens of thousands of absent ones, and checks the rows it reads or removes. The
last test runs an incremental index whose new pages bring more link surfaces than the limit. The tests skip when
Docker is unavailable (the `migrated` fixture raises `pytest.skip`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.db import MAX_BIND_PARAMETERS
from sqlalchemy import NullPool, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engram.indexing.contracts import (
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    SqlAlchemyKnowledgeBaseUnitOfWork,
)
from engram.indexing.infrastructure import orm
from engram.indexing.infrastructure.repository import (
    ClaimQualifiers,
    Claims,
    Edges,
    LinkRefs,
    ManifestEntries,
    Pages,
    Sections,
    SourceRepository,
    UnitSummaries,
    ValidationIssues,
    VocabEntries,
)
from engram.init.contracts import scaffold
from engram.kb.contracts import CanonicalSection, ClaimGrammarSpec, KnowledgeBaseSchema, PageTypeSpec, VocabEntry
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from engram.indexing.contracts import IndexReport

_COLLECTION = "bind-limit"
_NEIGHBOUR = "neighbour"

# Values no row carries, more of them than one statement may bind.
_ABSENT = frozenset(f"absent.{n}" for n in range(40_000))
_ABSENT_PAIRS = frozenset(("absent", f"value.{n}") for n in range(40_000))


def test_the_absent_sets_are_past_the_limit() -> None:
    assert len(_ABSENT) > MAX_BIND_PARAMETERS
    assert len(_ABSENT_PAIRS) > MAX_BIND_PARAMETERS


@pytest.fixture
async def session(migrated: str) -> AsyncIterator[AsyncSession]:
    """An `AsyncSession` bound to the freshly migrated database."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            yield db
    finally:
        await engine.dispose()


def _page(page_id: str, *, collection: str = _COLLECTION) -> dict[str, object]:
    return {
        "id": page_id,
        "collection": collection,
        "page_type": "note",
        "slug": page_id.split(".", 1)[-1],
        "title": page_id.title(),
        "status": "draft",
        "card_text": page_id,
        "body_hash": "h",
    }


def _section(section_id: str, *, collection: str = _COLLECTION) -> dict[str, object]:
    page_id, _, anchor = section_id.partition("#")
    return {
        "id": section_id,
        "collection": collection,
        "page_id": page_id,
        "heading": anchor.title(),
        "path": anchor,
        "ord": 0,
        "body": "b",
        "body_hash": "h",
    }


def _claim(claim_id: str, *, subject_id: str | None = None) -> dict[str, object]:
    page_id, _, block_id = claim_id.partition("#")
    return {
        "id": claim_id,
        "collection": _COLLECTION,
        "page_id": page_id,
        "block_id": block_id,
        "text": f"Claim {claim_id}",
        "kind": "observation",
        "subject_id": subject_id,
        "subject_collection": _COLLECTION if subject_id else None,
    }


def _link_ref(src_page_id: str, raw_target: str, *, dst_id: str | None = None) -> dict[str, object]:
    return {
        "collection": _COLLECTION,
        "src_page_id": src_page_id,
        "src_kind": "page",
        "rel": "related",
        "raw_target": raw_target,
        "dst_id": dst_id,
        "dst_collection": _COLLECTION if dst_id else None,
        "provenance_claim": None,
    }


def _edge(src_id: str, dst_id: str) -> dict[str, object]:
    return {
        "src_collection": _COLLECTION,
        "src_id": src_id,
        "src_kind": "claim" if "#" in src_id else "page",
        "rel": "related",
        "dst_collection": _COLLECTION,
        "dst_id": dst_id,
        "dst_kind": "page",
    }


def _source(source_id: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "provider": "files",
        "provider_key": source_id,
        "collection": _COLLECTION,
        "title": source_id,
        "source_hash": "h",
    }


def _chunk(chunk_id: str) -> dict[str, object]:
    source_id, _, seq = chunk_id.partition(":")
    return {"id": chunk_id, "collection": _COLLECTION, "source_id": source_id, "seq": int(seq), "body": "b"}


async def _ids(session: AsyncSession, table: str, column: str = "id") -> set[str]:
    """The `column` values of `table`'s rows in the collection."""
    result = await session.execute(text(f"SELECT {column} FROM kb.{table} WHERE collection = :collection"), {"collection": _COLLECTION})  # noqa: S608 (the table and column are this module's own names)
    return {str(value) for value in result.scalars().all()}


class TestReadsPastTheLimit:
    async def test_the_pages_linking_at_any_of_the_names_are_found(self, session: AsyncSession) -> None:
        await Pages(session).upsert_many([_page("note.one"), _page("note.two"), _page("note.three")])
        await LinkRefs(session).replace_for_sources(
            [_link_ref("note.one", "alpha"), _link_ref("note.two", "beta"), _link_ref("note.three", "gamma")], set(), collection=_COLLECTION
        )

        found = await LinkRefs(session).src_pages_referencing({"alpha", "beta"} | _ABSENT, collection=_COLLECTION)

        assert found == {"note.one", "note.two"}

    async def test_the_pages_whose_link_subject_or_qualifier_points_at_any_of_the_ids_are_found(self, session: AsyncSession) -> None:
        await Pages(session).upsert_many(
            [_page(page_id) for page_id in ("note.one", "note.two", "note.three", "note.four", "topic.a", "topic.b", "topic.c")]
        )
        await LinkRefs(session).replace_for_sources([_link_ref("note.one", "a", dst_id="topic.a")], set(), collection=_COLLECTION)
        await Claims(session).upsert_many([_claim("note.two#c1", subject_id="topic.b"), _claim("note.three#c1"), _claim("note.four#c1")])
        await ClaimQualifiers(session).replace_for_pages(
            [
                {
                    "collection": _COLLECTION,
                    "claim_id": "note.three#c1",
                    "qualifier": "applies_to",
                    "ord": 0,
                    "target_collection": _COLLECTION,
                    "target_id": "topic.c",
                }
            ],
            set(),
            collection=_COLLECTION,
        )

        found = await LinkRefs(session).src_pages_pointing_at({"topic.a", "topic.b", "topic.c"} | _ABSENT, collection=_COLLECTION)

        assert found == {"note.one", "note.two", "note.three"}


class TestWholeCollectionPrunesPastTheLimit:
    async def test_the_page_prune_removes_only_the_pages_outside_the_keep_set(self, session: AsyncSession) -> None:
        await Pages(session).upsert_many([_page("note.kept"), _page("note.gone")])
        await Pages(session).upsert_many([_page("note.gone", collection=_NEIGHBOUR)])
        await Claims(session).upsert_many([_claim("note.kept#c1"), _claim("note.gone#c1")])

        removed = await Pages(session).prune_missing({"note.kept"} | _ABSENT, collection=_COLLECTION)

        assert removed == 1
        assert await _ids(session, "pages") == {"note.kept"}
        assert await _ids(session, "claims") == {"note.kept#c1"}
        assert await session.get(orm.PageORM, (_NEIGHBOUR, "note.gone")) is not None

    async def test_the_section_prune_removes_only_the_sections_outside_the_keep_set(self, session: AsyncSession) -> None:
        await Pages(session).upsert_many([_page("note.one")])
        await Sections(session).upsert_many([_section("note.one#kept"), _section("note.one#gone")])

        removed = await Sections(session).prune_missing({"note.one#kept"} | _ABSENT, collection=_COLLECTION)

        assert removed == 1
        assert await _ids(session, "sections") == {"note.one#kept"}

    async def test_the_summary_prune_removes_only_the_summaries_outside_the_keep_set(self, session: AsyncSession) -> None:
        await session.execute(
            insert(orm.UnitSummaryORM).values(
                [
                    {"collection": _COLLECTION, "unit_id": unit_id, "unit_kind": "page", "summary": "s", "model": "m", "source_hash": "h"}
                    for unit_id in ("note.kept", "note.gone")
                ]
            )
        )

        removed = await UnitSummaries(session).prune_missing({"note.kept"} | _ABSENT, collection=_COLLECTION)

        assert removed == 1
        assert await _ids(session, "unit_summaries", "unit_id") == {"note.kept"}

    async def test_the_vocabulary_prune_removes_only_the_pairs_outside_the_keep_set(self, session: AsyncSession) -> None:
        await VocabEntries(session).upsert_many(
            [{"collection": _COLLECTION, "vocab": "topic", "value": value, "definition": "d"} for value in ("kept", "gone")]
        )

        removed = await VocabEntries(session).prune_missing({("topic", "kept")} | _ABSENT_PAIRS, collection=_COLLECTION)

        assert removed == 1
        assert await _ids(session, "vocab", "value") == {"kept"}

    async def test_the_source_and_chunk_prunes_remove_only_the_rows_outside_the_keep_sets(self, session: AsyncSession) -> None:
        sources = SourceRepository(session)
        await sources.upsert_sources([_source("kept"), _source("gone")])
        await sources.upsert_chunks([_chunk("kept:0"), _chunk("kept:1")])

        removed_chunks = await sources.prune_missing_chunks({"kept:0"} | _ABSENT, collection=_COLLECTION)
        removed_sources = await sources.prune_missing_sources({"kept"} | _ABSENT, collection=_COLLECTION)

        assert (removed_chunks, removed_sources) == (1, 1)
        assert await _ids(session, "raw_chunks") == {"kept:0"}
        assert await _ids(session, "sources_raw", "source_id") == {"kept"}


class TestPerPageWritesPastTheLimit:
    async def test_deleting_pages_by_id_removes_the_named_pages_and_their_claims(self, session: AsyncSession) -> None:
        await Pages(session).upsert_many([_page("note.kept"), _page("note.gone")])
        await Claims(session).upsert_many([_claim("note.kept#c1"), _claim("note.gone#c1")])

        await Pages(session).delete_by_ids({"note.gone"} | _ABSENT, collection=_COLLECTION)

        assert await _ids(session, "pages") == {"note.kept"}
        assert await _ids(session, "claims") == {"note.kept#c1"}

    async def test_the_page_child_reconcile_removes_only_the_named_pages_rows_outside_the_keep_set(self, session: AsyncSession) -> None:
        await Pages(session).upsert_many([_page("note.one"), _page("note.two")])
        await Sections(session).upsert_many([_section("note.one#kept"), _section("note.one#gone"), _section("note.two#other")])
        await Claims(session).upsert_many([_claim("note.one#c1"), _claim("note.one#c2"), _claim("note.two#c1")])

        removed_sections = await Sections(session).prune_for_pages(
            {"note.one#kept"} | _ABSENT, {"note.one"} | _ABSENT, collection=_COLLECTION
        )
        removed_claims = await Claims(session).prune_for_pages({"note.one#c1"} | _ABSENT, {"note.one"} | _ABSENT, collection=_COLLECTION)

        assert (removed_sections, removed_claims) == (1, 1)
        assert await _ids(session, "sections") == {"note.one#kept", "note.two#other"}
        assert await _ids(session, "claims") == {"note.one#c1", "note.two#c1"}

    async def test_the_summary_reconcile_removes_the_summaries_of_gone_sections_and_deleted_pages(self, session: AsyncSession) -> None:
        await Pages(session).upsert_many([_page("note.one")])
        await Sections(session).upsert_many([_section("note.one#kept"), _section("note.one#gone")])
        await session.execute(
            insert(orm.UnitSummaryORM).values(
                [
                    {
                        "collection": _COLLECTION,
                        "unit_id": unit_id,
                        "unit_kind": "section",
                        "summary": "s",
                        "model": "m",
                        "source_hash": "h",
                    }
                    for unit_id in ("note.one#kept", "note.one#gone", "note.deleted", "note.other")
                ]
            )
        )

        removed = await UnitSummaries(session).prune_for_pages(
            {"note.one#kept"} | _ABSENT, {"note.one"} | _ABSENT, {"note.deleted"} | _ABSENT, collection=_COLLECTION
        )

        assert removed == 2
        assert await _ids(session, "unit_summaries", "unit_id") == {"note.one#kept", "note.other"}

    async def test_replacing_edges_for_sources_removes_the_named_pages_page_and_claim_edges(self, session: AsyncSession) -> None:
        edges = Edges(session)
        await edges.replace_all(
            [_edge("note.one", "topic.a"), _edge("note.one#c1", "topic.a"), _edge("note.two", "topic.a")], collection=_COLLECTION
        )

        changed = await edges.replace_for_sources([], {"note.one"} | _ABSENT, collection=_COLLECTION)

        assert changed == 2
        assert {edge.src_id for edge in await edges.all()} == {"note.two"}

    async def test_replacing_link_refs_for_sources_removes_only_the_named_pages_links(self, session: AsyncSession) -> None:
        await Pages(session).upsert_many([_page("note.one"), _page("note.two")])
        links = LinkRefs(session)
        await links.replace_for_sources([_link_ref("note.one", "alpha"), _link_ref("note.two", "beta")], set(), collection=_COLLECTION)

        await links.replace_for_sources([], {"note.one"} | _ABSENT, collection=_COLLECTION)

        assert await _ids(session, "link_refs", "src_page_id") == {"note.two"}

    async def test_replacing_claim_qualifiers_for_pages_removes_only_the_named_pages_rows(self, session: AsyncSession) -> None:
        await Pages(session).upsert_many([_page("note.one"), _page("note.two"), _page("topic.a")])
        await Claims(session).upsert_many([_claim("note.one#c1"), _claim("note.two#c1")])
        qualifiers = ClaimQualifiers(session)
        await qualifiers.replace_for_pages(
            [
                {
                    "collection": _COLLECTION,
                    "claim_id": claim_id,
                    "qualifier": "applies_to",
                    "ord": 0,
                    "target_collection": _COLLECTION,
                    "target_id": "topic.a",
                }
                for claim_id in ("note.one#c1", "note.two#c1")
            ],
            set(),
            collection=_COLLECTION,
        )

        await qualifiers.replace_for_pages([], {"note.one"} | _ABSENT, collection=_COLLECTION)

        assert await _ids(session, "claim_qualifiers", "claim_id") == {"note.two#c1"}

    async def test_replacing_validation_issues_for_pages_removes_only_the_named_pages_issues(self, session: AsyncSession) -> None:
        issues = ValidationIssues(session)
        await issues.replace_all(
            [
                {"collection": _COLLECTION, "page_id": page_id, "severity": "warning", "rule": "r", "message": "m", "anchor": None}
                for page_id in ("note.one", "note.two")
            ],
            collection=_COLLECTION,
        )

        await issues.replace_for_pages([], {"note.one"} | _ABSENT, collection=_COLLECTION)

        assert await _ids(session, "validation_store", "page_id") == {"note.two"}

    async def test_deleting_sources_by_key_removes_the_named_sources_and_their_chunks(self, session: AsyncSession) -> None:
        sources = SourceRepository(session)
        await sources.upsert_sources([_source("kept"), _source("gone")])
        await sources.upsert_chunks([_chunk("kept:0"), _chunk("gone:0")])

        await sources.delete_sources_by_keys({"gone"} | _ABSENT, collection=_COLLECTION)

        assert await _ids(session, "sources_raw", "source_id") == {"kept"}
        assert await _ids(session, "raw_chunks") == {"kept:0"}

    async def test_deleting_manifest_paths_removes_only_the_named_paths(self, session: AsyncSession) -> None:
        manifest = ManifestEntries(session)
        await manifest.upsert_many(
            [
                {
                    "collection": _COLLECTION,
                    "path": path,
                    "kind": "note",
                    "content_hash": "c",
                    "identity_hash": "i",
                    "page_id": None,
                    "source_key": None,
                }
                for path in ("Notes/kept.md", "Notes/gone.md")
            ],
            collection=_COLLECTION,
        )

        await manifest.delete_paths({"Notes/gone.md"} | _ABSENT, collection=_COLLECTION)

        assert await _ids(session, "index_manifest", "path") == {"Notes/kept.md"}


# -- an incremental index whose run adds more link surfaces than the limit ----

_TOPICS = 120
_ALIASES_PER_TOPIC = 280
_CITED_ALIAS = "Name 16000"


def _spec() -> KnowledgeBaseSchema:
    """Notes whose claims link a topic through the `concerns` relation, and the topic pages they link."""
    findings = (CanonicalSection(slug="findings"),)
    return KnowledgeBaseSchema(
        slug=_COLLECTION,
        page_types=(
            PageTypeSpec(page_type="note", id_prefix="note.", canonical_sections=findings, claims_enabled=True),
            PageTypeSpec(page_type="topic", id_prefix="topic.", canonical_sections=findings),
        ),
        claim_types=(VocabEntry(value="observation", definition="Something observed"),),
        claims=ClaimGrammarSpec(section="findings", edge_relations=("concerns",)),
    )


def _citing_note() -> str:
    """A note whose one claim links a name no page carries yet."""
    return f"""---
id: note.citing
type: note
title: Citing
status: draft
---

## Findings

- The named topic shapes the outcome. ^c1
    - loc:: §1
    - kind:: observation
    - concerns:: [[{_CITED_ALIAS}]]
"""


def _topic(number: int) -> str:
    """A topic page carrying its own run of aliases, `Name <n>` for each `n` in its run."""
    first = number * _ALIASES_PER_TOPIC
    aliases = "\n".join(f"  - Name {n}" for n in range(first, first + _ALIASES_PER_TOPIC))
    return f"---\nid: topic.t{number}\ntype: topic\ntitle: Topic {number}\nstatus: draft\naliases:\n{aliases}\n---\n"


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


async def _targets_of_the_citing_claim(migrated: str) -> list[str]:
    """The pages the citing note's claim has a resolved edge to."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            statement = select(orm.EdgeORM.dst_id).where(orm.EdgeORM.src_collection == _COLLECTION, orm.EdgeORM.src_id == "note.citing#c1")
            return list((await conn.execute(statement)).scalars().all())
    finally:
        await engine.dispose()


async def _topic_count(migrated: str) -> int:
    engine = create_async_engine(migrated, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT count(*) FROM kb.pages WHERE collection = :collection AND page_type = 'topic'"), {"collection": _COLLECTION}
            )
            return int(result.scalar_one())
    finally:
        await engine.dispose()


class TestAnIncrementalIndexAddingMoreLinkSurfacesThanTheLimit:
    async def test_it_indexes_the_new_pages_and_resolves_the_link_one_of_them_now_answers(self, tmp_path: Path, migrated: str) -> None:
        assert _TOPICS * _ALIASES_PER_TOPIC > MAX_BIND_PARAMETERS
        spec = _spec()
        builder = KnowledgeBaseBuilder(tmp_path)
        builder.add_meta_stubs()
        builder.add_knowledge_base_schema(spec)
        for relative, body in scaffold(spec).items():
            if relative.startswith("_meta/vocabulary/"):
                (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
                (tmp_path / relative).write_text(body, encoding="utf-8")
        builder.add_unit_document("note", "citing", _citing_note())
        await _index(tmp_path, migrated, spec, full=True)
        assert await _targets_of_the_citing_claim(migrated) == []

        for number in range(_TOPICS):
            builder.add_unit_document("topic", f"t{number}", _topic(number))
        report = await _index(tmp_path, migrated, spec, full=False)

        assert report.mode == "incremental"
        assert await _topic_count(migrated) == _TOPICS
        assert await _targets_of_the_citing_claim(migrated) == ["topic.t57"]
