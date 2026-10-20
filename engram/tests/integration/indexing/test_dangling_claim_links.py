"""A claim link naming a page not yet written, resolved by the incremental index that adds the page, over a real migrated database.

One note's claim names a missing topic as its subject or in its `applies_to` link field. A full index stores the
claim with that link unset and reports it as dangling. Adding the topic's file and running an incremental index must
resolve the claim to the new topic and clear the dangling-link finding, leaving exactly the rows a full index of the
same files writes. The database tests skip when Docker is unavailable (the `migrated` fixture raises `pytest.skip`).
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

_COLLECTION = "dangling-claim-links"


def _note(*, subject: str, applies_to: tuple[str, ...]) -> str:
    """A note whose one claim names `subject` as its subject and `applies_to` in its link field."""
    links = ", ".join(f"[[{target}]]" for target in applies_to)
    return f"""---
id: note.citing
type: note
title: Citing
status: draft
---

## Findings

- Gamma shapes the outcome. ^c1
    - loc:: §1
    - kind:: observation
    - subject:: [[{subject}]]
    - applies_to:: {links}
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
            PageTypeSpec(page_type="note", id_prefix="note.", folder="Wiki/Notes", canonical_sections=findings, claims_enabled=True),
            PageTypeSpec(page_type="topic", id_prefix="topic.", folder="Wiki/Topics", canonical_sections=findings),
        ),
        claim_types=(VocabEntry(value="observation", definition="Something observed"),),
        claims=ClaimGrammarSpec(section="findings", multilink_fields=("applies_to",)),
    )


def _knowledge_base(root: Path, spec: KnowledgeBaseSchema, note: str) -> KnowledgeBaseBuilder:
    """Write the topic `beta`, the citing note, the scaffolded vocabulary and the orientation stubs."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema(spec)
    for relative, body in scaffold(spec).items():
        if relative.startswith("_meta/vocabulary/"):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(body, encoding="utf-8")
    builder.add_unit_document("Wiki/Topics", "beta", _topic("beta"))
    builder.add_unit_document("Wiki/Notes", "citing", note)
    return builder


async def _index(knowledge_base: Path, migrated: str, spec: KnowledgeBaseSchema, *, full: bool) -> IndexReport:
    """Run one index of `knowledge_base` into the migrated database, a full rebuild when `full`."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        handler = IndexKnowledgeBaseHandler(
            FilesystemKnowledgeBaseReader(knowledge_base, note_folders=spec.note_folders),
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


async def _claims(migrated: str) -> list[tuple[str, ...]]:
    return await _rows(migrated, "SELECT id, coalesce(subject_id, '') FROM kb.claims WHERE collection = :collection")


async def _qualifiers(migrated: str) -> list[tuple[str, ...]]:
    return await _rows(migrated, "SELECT claim_id, qualifier, ord, target_id FROM kb.claim_qualifiers WHERE collection = :collection")


async def _dangling(migrated: str) -> list[tuple[str, ...]]:
    return await _rows(
        migrated, "SELECT page_id, message FROM kb.validation_store WHERE collection = :collection AND rule = 'dangling-link'"
    )


async def _state(migrated: str) -> dict[str, list[tuple[str, ...]]]:
    """The claims, claim qualifiers, link references and dangling-link findings the collection holds."""
    link_refs = await _rows(
        migrated,
        "SELECT src_page_id, src_kind, rel, raw_target, coalesce(dst_id, ''), coalesce(provenance_claim, '')"
        " FROM kb.link_refs WHERE collection = :collection",
    )
    return {
        "claims": await _claims(migrated),
        "qualifiers": await _qualifiers(migrated),
        "link_refs": link_refs,
        "dangling": await _dangling(migrated),
    }


_CASES = {
    "subject": (
        _note(subject="gamma", applies_to=("beta",)),
        [("note.citing#c1", "")],
        [("note.citing#c1", "applies_to", "0", "topic.beta")],
        [("note.citing#c1", "topic.gamma")],
        [("note.citing#c1", "applies_to", "0", "topic.beta")],
    ),
    "link-field": (
        _note(subject="beta", applies_to=("gamma", "beta")),
        [("note.citing#c1", "topic.beta")],
        [("note.citing#c1", "applies_to", "0", "topic.beta")],
        [("note.citing#c1", "topic.beta")],
        [("note.citing#c1", "applies_to", "0", "topic.gamma"), ("note.citing#c1", "applies_to", "1", "topic.beta")],
    ),
}


class TestAClaimLinkToATopicNotYetWritten:
    @pytest.mark.parametrize("case", list(_CASES), ids=list(_CASES))
    async def test_the_incremental_index_adding_the_topic_resolves_the_claim_as_a_full_index_would(
        self, tmp_path: Path, migrated: str, case: str
    ) -> None:
        note, claims_before, qualifiers_before, claims_after, qualifiers_after = _CASES[case]
        spec = _spec()
        builder = _knowledge_base(tmp_path, spec, note)
        await _index(tmp_path, migrated, spec, full=True)
        assert await _claims(migrated) == claims_before
        assert await _qualifiers(migrated) == qualifiers_before
        assert [page_id for page_id, _message in await _dangling(migrated)] == ["note.citing"]

        builder.add_unit_document("Wiki/Topics", "gamma", _topic("gamma"))
        report = await _index(tmp_path, migrated, spec, full=False)

        assert report.mode == "incremental"
        assert await _claims(migrated) == claims_after
        assert await _qualifiers(migrated) == qualifiers_after
        assert await _dangling(migrated) == []
        incremental = await _state(migrated)
        await _index(tmp_path, migrated, spec, full=True)
        assert incremental == await _state(migrated)
