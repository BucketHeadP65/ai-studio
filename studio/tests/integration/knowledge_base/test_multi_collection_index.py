r"""Two-collection index safety test — the multi-knowledge_base guard.

Indexing knowledge base B (collection="beta") into a DB that already holds knowledge base A
(collection="alpha") must NOT delete A's rows: every prune is scoped by collection.
Three phases:

1. Index "alpha" → assert alpha pages/sections/edges present.
2. Index "beta" into the SAME DB → assert beta rows present AND alpha rows STILL
   present (an unscoped prune would delete or FK-error on alpha's rows).
3. Re-index alpha-minus-one-page → assert that page (+ cascaded children) is
   pruned from alpha, and beta rows are untouched.

Skips when Docker is unavailable (via the `migrated` fixture's `pg` guard).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from engram.indexing.contracts import FilesystemKnowledgeBaseReader, IndexKnowledgeBase, IndexKnowledgeBaseHandler
from engram.indexing.infrastructure import orm
from engram.kb.contracts import load_knowledge_base_schema
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine

from studio.knowledge_base.indexing.infrastructure.validation_report_sink import FileValidationReportSink
from studio.knowledge_base.wiring.indexing import KnowledgeBaseUnitOfWorkFactory

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

# ── minimal knowledge base content — two independent collections ─────────────────────
#
# Alpha has two pages (paper + entity); beta has one page (paper only) so each
# collection is distinguishable in row counts and both are tiny enough to index
# in the unit-test budget.

_ALPHA_PAPER = """\
---
id: paper.alpha-method
type: paper
title: Alpha Method
summary: Alpha approach.
status: synthesized
zotero_key: ALPHA_KEY
year: 2024
paper_type: method
source_hash: a1b2
tasks: [classification]
stages: [training]
---

## TL;DR

The alpha method for classification.

## Findings

- Alpha outperforms baselines. ^c1
    - loc:: §4, Table 1
    - kind:: observation
    - on_task:: [[ent.alpha-task]]
"""

_ALPHA_ENTITY = """\
---
id: ent.alpha-entity
type: entity
title: Alpha Entity
status: draft
entity_type: technique
---

## Definition

A technique in the alpha collection.
"""

# The claim's qualifier target: a SEPARATE page from `ent.alpha-entity` (phase 3 prunes the latter),
# so pruning it never orphans a still-live `claim_qualifiers` row (the citation FK has no cascade).
_ALPHA_TASK_ENTITY = """\
---
id: ent.alpha-task
type: entity
title: Alpha Task
status: draft
entity_type: task
---

## Definition

The task alpha's claim qualifies its result against.
"""

_ALPHA_MIRROR = """\
---
source_key: ALPHA_KEY
provider: example-library
source_hash: a1b2
---
# Alpha Method Paper

## Introduction

Alpha is a method for classification tasks.
"""

_BETA_PAPER = """\
---
id: paper.beta-approach
type: paper
title: Beta Approach
summary: Beta approach.
status: synthesized
zotero_key: BETA_KEY
year: 2023
paper_type: survey
source_hash: b3c4
tasks: [retrieval]
stages: [evaluation]
---

## TL;DR

The beta approach for retrieval.

## Findings

- Beta improves retrieval recall. ^c1
    - loc:: §3
    - kind:: observation
    - on_task:: [[ent.beta-entity]]
"""

_BETA_ENTITY = """\
---
id: ent.beta-entity
type: entity
title: Beta Entity
status: draft
entity_type: task
---

## Definition

A technique in the beta collection.
"""

_BETA_MIRROR = """\
---
source_key: BETA_KEY
provider: example-library
source_hash: b3c4
---
# Beta Approach Paper

## Methodology

Beta takes a retrieval-focused angle.
"""


def _alpha_knowledge_base(root: Path) -> Path:
    """Build a three-page alpha knowledge base under `root` (a paper with a qualified claim, its prunable entity, and its qualifier target)."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_unit_document("Papers", "alpha-method", _ALPHA_PAPER)
    builder.add_unit_document("Entities", "alpha-entity", _ALPHA_ENTITY)
    builder.add_unit_document("Entities", "alpha-task", _ALPHA_TASK_ENTITY)
    builder.add_source_mirror("alpha-method", _ALPHA_MIRROR)
    builder.add_vocabulary()
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema()
    return root


def _alpha_knowledge_base_minus_entity(root: Path) -> Path:
    """Build the alpha knowledge base with the entity page removed (for phase 3); the qualifier's target entity stays present."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_unit_document("Papers", "alpha-method", _ALPHA_PAPER)
    # ent.alpha-entity intentionally omitted — simulates a deleted note
    builder.add_unit_document("Entities", "alpha-task", _ALPHA_TASK_ENTITY)
    builder.add_source_mirror("alpha-method", _ALPHA_MIRROR)
    builder.add_vocabulary()
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema()
    return root


def _beta_knowledge_base(root: Path) -> Path:
    """Build a two-page beta knowledge base under `root` (a paper with a qualified claim, plus its target entity)."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_unit_document("Papers", "beta-approach", _BETA_PAPER)
    builder.add_unit_document("Entities", "beta-entity", _BETA_ENTITY)
    builder.add_source_mirror("beta-approach", _BETA_MIRROR)
    builder.add_vocabulary()
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema()
    return root


# ── shared page-id knowledge bases — a bare id collision replayed on an ordinary content page ────────
#
# Two independent knowledge bases declare the SAME page id (`paper.shared-note`) and the SAME helper entity id
# (`ent.shared-helper`), each with its own distinct title/body/claim/qualifier-target/about text, so a
# cross-knowledge_base mix-up would surface as a wrong VALUE at read time, not merely a missing or extra row.

_SHARED_NOTE_A = """\
---
id: paper.shared-note
type: paper
title: Shared Note — Knowledge base A's Version
summary: Knowledge base A's own account of the shared note.
status: synthesized
zotero_key: SHARED_A_KEY
year: 2021
paper_type: method
source_hash: sa01
---

## TL;DR

Knowledge base A's exclusive content for the shared note.

## Findings

- Knowledge base A observes an A-only result. ^c1
    - loc:: §1
    - kind:: observation
    - on_task:: [[ent.shared-helper]]
"""

_SHARED_HELPER_A = """\
---
id: ent.shared-helper
type: entity
title: Shared Helper — Knowledge base A
status: draft
entity_type: task
---

## Definition

Knowledge base A's own helper entity.
"""

_SHARED_MIRROR_A = """\
---
source_key: SHARED_A_KEY
provider: example-library
source_hash: sa01
---
# Knowledge base A Shared Source

Knowledge base A's mirror text for the shared note.
"""

_SHARED_NOTE_B = """\
---
id: paper.shared-note
type: paper
title: Shared Note — Knowledge base B's Version
summary: Knowledge base B's own account of the shared note.
status: synthesized
zotero_key: SHARED_B_KEY
year: 2022
paper_type: survey
source_hash: sb01
---

## TL;DR

Knowledge base B's exclusive content for the shared note.

## Findings

- Knowledge base B observes a B-only result. ^c1
    - loc:: §2
    - kind:: observation
    - on_task:: [[ent.shared-helper]]
"""

_SHARED_HELPER_B = """\
---
id: ent.shared-helper
type: entity
title: Shared Helper — Knowledge base B
status: draft
entity_type: task
---

## Definition

Knowledge base B's own helper entity.
"""

_SHARED_MIRROR_B = """\
---
source_key: SHARED_B_KEY
provider: example-library
source_hash: sb01
---
# Knowledge base B Shared Source

Knowledge base B's mirror text for the shared note.
"""

# `add_meta_stubs()` writes the SAME three orientation stubs for every knowledge base (they are not
# assertion-relevant to the other multi-collection tests), so the about-page check below overwrites
# `_meta/purpose.md` with its own distinct text per knowledge base after laying the stubs down.
_SHARED_PURPOSE_A = "# Purpose\n\nKnowledge base A's own distinct purpose text for the about page.\n"
_SHARED_PURPOSE_B = "# Purpose\n\nKnowledge base B's own distinct purpose text for the about page.\n"


def _shared_knowledge_base_a(root: Path) -> Path:
    """Build knowledge base A: declares `paper.shared-note` + `ent.shared-helper`, with knowledge base A's own distinct content."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_unit_document("Papers", "shared-note", _SHARED_NOTE_A)
    builder.add_unit_document("Entities", "shared-helper", _SHARED_HELPER_A)
    builder.add_source_mirror("shared-source", _SHARED_MIRROR_A)
    builder.add_vocabulary()
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema()
    (root / "_meta" / "purpose.md").write_text(_SHARED_PURPOSE_A, encoding="utf-8")
    return root


def _shared_knowledge_base_b(root: Path) -> Path:
    """Build knowledge base B: declares the SAME `paper.shared-note` + `ent.shared-helper` ids, with knowledge base B's own distinct content."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_unit_document("Papers", "shared-note", _SHARED_NOTE_B)
    builder.add_unit_document("Entities", "shared-helper", _SHARED_HELPER_B)
    builder.add_source_mirror("shared-source", _SHARED_MIRROR_B)
    builder.add_vocabulary()
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema()
    (root / "_meta" / "purpose.md").write_text(_SHARED_PURPOSE_B, encoding="utf-8")
    return root


# ── vocab-isolation knowledge bases — the same term, two different meanings, one shared vocab table ──

_VOCAB_ENTITY_TYPES_A = """\
---
id: meta.registry-entity-types
type: meta
title: "Registry: entity types"
summary: Controlled vocabulary for `entity_type` on entity pages.
status: verified
---

# Registry: entity types

| entity_type | Definition | Examples |
|---|---|---|
| `widget` | Knowledge base A's meaning: a graphical UI control. | `button` |
"""

_VOCAB_ENTITY_TYPES_B = """\
---
id: meta.registry-entity-types
type: meta
title: "Registry: entity types"
summary: Controlled vocabulary for `entity_type` on entity pages.
status: verified
---

# Registry: entity types

| entity_type | Definition | Examples |
|---|---|---|
| `widget` | Knowledge base B's meaning: a mechanical fastening part. | `bolt` |
"""

_VOCAB_ENTITY_A = """\
---
id: ent.vocab-a-widget
type: entity
title: Vocab Widget (A)
status: draft
entity_type: widget
---

## Definition

Knowledge base A's widget entity.
"""

_VOCAB_ENTITY_B = """\
---
id: ent.vocab-b-widget
type: entity
title: Vocab Widget (B)
status: draft
entity_type: widget
---

## Definition

Knowledge base B's widget entity.
"""


def _vocab_knowledge_base_a(root: Path) -> Path:
    """Build a knowledge base whose entity-types registry defines `widget` with knowledge base A's own definition."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_unit_document("Entities", "vocab-a-widget", _VOCAB_ENTITY_A)
    builder.add_vocabulary()
    (root / "_meta" / "vocabulary" / "entity-types.md").write_text(_VOCAB_ENTITY_TYPES_A, encoding="utf-8")
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema()
    return root


def _vocab_knowledge_base_b(root: Path) -> Path:
    """Build a knowledge base whose entity-types registry defines the SAME `widget` term with knowledge base B's own, different definition."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_unit_document("Entities", "vocab-b-widget", _VOCAB_ENTITY_B)
    builder.add_vocabulary()
    (root / "_meta" / "vocabulary" / "entity-types.md").write_text(_VOCAB_ENTITY_TYPES_B, encoding="utf-8")
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema()
    return root


async def _index(engine: AsyncEngine, knowledge_base: Path, collection: str) -> None:
    """Drive IndexKnowledgeBaseHandler directly (fake embedder, real PG) for `collection`.

    `collection_named=True`: every call in this module deliberately picks its own collection (the
    whole point of a multi-collection isolation test) — including phase 3's re-index of "alpha"
    from a second, trimmed-content root, a legitimate take-over of a collection this handler's own
    knowledge base root did not most recently index, never an accidental foreign write.
    """
    handler = IndexKnowledgeBaseHandler(
        FilesystemKnowledgeBaseReader(knowledge_base),
        FakeEmbedding(),
        KnowledgeBaseUnitOfWorkFactory(engine),
        FileValidationReportSink(knowledge_base),
        spec=load_knowledge_base_schema(knowledge_base),
    )
    await handler.handle(IndexKnowledgeBase(collection=collection, collection_named=True))


async def _page_ids_for_collection(engine: AsyncEngine, collection: str) -> set[str]:
    """Return the set of non-meta page ids belonging to `collection`.

    `meta.about` keeps its bare literal id only for the legacy `"research"` collection
    (`engram.indexing.service.orientation.orientation_page_id`); every other collection's copy is
    suffixed (`meta.about@{collection}`) and never collides with another non-research
    collection's copy. Exclude it anyway so the query focuses on actual knowledge base content
    pages, not the synthetic orientation page.
    """
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT id FROM kb.pages WHERE collection = :c AND id NOT LIKE 'meta.%'"), {"c": collection})
        return {row[0] for row in result}


async def _section_ids_for_collection(engine: AsyncEngine, collection: str) -> set[str]:
    """Return the section ids for `collection`'s pages."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT s.id FROM kb.sections s JOIN kb.pages p ON s.page_id = p.id WHERE p.collection = :c"), {"c": collection}
        )
        return {row[0] for row in result}


async def _edge_src_ids_for_collection(engine: AsyncEngine, collection: str) -> set[str]:
    """Return `src_id` values for edges whose source page belongs to `collection`."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT e.src_id FROM kb.edges e JOIN kb.pages p ON e.src_id = p.id WHERE p.collection = :c"), {"c": collection}
        )
        return {row[0] for row in result}


async def _claim_ids_for_collection(engine: AsyncEngine, collection: str) -> set[str]:
    """Return the claim ids belonging to `collection`."""
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT id FROM kb.claims WHERE collection = :c"), {"c": collection})
        return {row[0] for row in result}


async def _qualifier_keys_for_collection(engine: AsyncEngine, collection: str) -> set[tuple[str, str, int]]:
    """Return `(claim_id, qualifier, ord)` triples for `collection`'s claim_qualifiers rows."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT claim_id, qualifier, ord FROM kb.claim_qualifiers WHERE collection = :c"), {"c": collection}
        )
        return {(row[0], row[1], row[2]) for row in result}


async def _vocab_pairs_for_collection(engine: AsyncEngine, collection: str) -> set[tuple[str, str]]:
    """Return `(vocab, value)` pairs for `collection`'s kb.vocab rows."""
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT vocab, value FROM kb.vocab WHERE collection = :c"), {"c": collection})
        return {(row[0], row[1]) for row in result}


async def _source_ids_for_collection(engine: AsyncEngine, collection: str) -> set[str]:
    """Return `source_id` values for `collection`'s kb.sources_raw rows."""
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT source_id FROM kb.sources_raw WHERE collection = :c"), {"c": collection})
        return {row[0] for row in result}


async def _insert_unit_summary(engine: AsyncEngine, *, collection: str, unit_id: str, unit_kind: str, summary: str) -> None:
    """Insert one `active` `kb.unit_summaries` row directly.

    `SummaryStage` (the governed, LLM-backed summarizer) is wired separately from
    `IndexKnowledgeBaseHandler` in production and is not exercised by `_index` here, so a
    survival assertion over `unit_summaries` needs its own row, seeded the same direct-SQL way
    the other tables' fixture rows (registries, mirrors) reach the database.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO kb.unit_summaries (collection, unit_id, unit_kind, summary, status, model, source_hash)"
                " VALUES (:collection, :unit_id, :unit_kind, :summary, 'active', 'test-fixture', 'fixturehash')"
            ),
            {"collection": collection, "unit_id": unit_id, "unit_kind": unit_kind, "summary": summary},
        )


async def _unit_summary_text(engine: AsyncEngine, *, collection: str, unit_id: str) -> str | None:
    """Return the `summary` text of `collection`'s unit_summaries row for `unit_id`, or `None` if absent."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT summary FROM kb.unit_summaries WHERE collection = :collection AND unit_id = :unit_id"),
            {"collection": collection, "unit_id": unit_id},
        )
        row = result.first()
        return row[0] if row is not None else None


async def _page_title(engine: AsyncEngine, *, collection: str, page_id: str) -> str | None:
    """Return the `title` of `collection`'s page row for `page_id`, or `None` if absent."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT title FROM kb.pages WHERE collection = :collection AND id = :id"), {"collection": collection, "id": page_id}
        )
        row = result.first()
        return row[0] if row is not None else None


async def _section_bodies_for_page(engine: AsyncEngine, *, collection: str, page_id: str) -> set[str]:
    """Return the section bodies under `collection`'s `page_id`."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT body FROM kb.sections WHERE collection = :collection AND page_id = :page_id"),
            {"collection": collection, "page_id": page_id},
        )
        return {row[0] for row in result}


async def _claim_texts_for_page(engine: AsyncEngine, *, collection: str, page_id: str) -> set[str]:
    """Return the claim texts under `collection`'s `page_id`."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT text FROM kb.claims WHERE collection = :collection AND page_id = :page_id"),
            {"collection": collection, "page_id": page_id},
        )
        return {row[0] for row in result}


async def _qualifier_targets_for_claim(engine: AsyncEngine, *, collection: str, claim_id: str) -> set[tuple[str, str, str]]:
    """Return `(qualifier, target_collection, target_id)` triples for `collection`'s `claim_id`'s qualifier rows."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT qualifier, target_collection, target_id FROM kb.claim_qualifiers"
                " WHERE collection = :collection AND claim_id = :claim_id"
            ),
            {"collection": collection, "claim_id": claim_id},
        )
        return {(row[0], row[1], row[2]) for row in result}


async def _collection_about(engine: AsyncEngine, collection: str) -> str | None:
    """Return `api.collection_about(collection)`'s body text, or `None` when it answers zero rows."""
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT body FROM api.collection_about(:c)"), {"c": collection})
        row = result.first()
        return row[0] if row is not None else None


@pytest.fixture
async def engine(migrated: str) -> AsyncEngine:  # type: ignore[return]
    """An engine over the freshly-migrated throwaway PG with the pgvector codec registered."""
    eng = create_async_engine(migrated, poolclass=NullPool)
    await orm.register_pgvector(eng)
    try:
        yield eng  # type: ignore[misc]
    finally:
        await eng.dispose()


class TestMultiCollectionIndex:
    """Two collections coexist in one DB without cross-pruning.

    The three-phase contract: index alpha; index beta into the same DB → alpha
    survives; re-index alpha-minus-entity → only alpha's entity prunes, beta untouched.
    """

    async def test_alpha_rows_survive_beta_index(self, engine: AsyncEngine, tmp_path: Path) -> None:  # type: ignore[override]
        """Phase 1 + 2: index alpha, then beta; alpha's rows must STILL be present at EVERY table.

        `meta.about` keeps its bare literal id only for the legacy `"research"` collection; alpha
        and beta each get their own suffixed copy and never collide — it is excluded here anyway,
        since it is a synthetic orientation page, not knowledge base content. `unit_summaries` is seeded
        directly (`SummaryStage` is wired separately from `IndexKnowledgeBaseHandler` and is not
        exercised here).
        """
        alpha_root = tmp_path / "alpha"
        alpha_root.mkdir()
        beta_root = tmp_path / "beta"
        beta_root.mkdir()

        # Phase 1: index alpha into the fresh DB.
        await _index(engine, _alpha_knowledge_base(alpha_root), collection="alpha")
        await _insert_unit_summary(
            engine, collection="alpha", unit_id="paper.alpha-method", unit_kind="page", summary="Alpha's own indexed summary."
        )

        # Record alpha's non-meta content page ids and sections (the survival baseline).
        alpha_content_pages_after_alpha = await _page_ids_for_collection(engine, "alpha")
        alpha_sections_after_alpha = await _section_ids_for_collection(engine, "alpha")
        alpha_claims_after_alpha = await _claim_ids_for_collection(engine, "alpha")
        alpha_qualifiers_after_alpha = await _qualifier_keys_for_collection(engine, "alpha")
        alpha_vocab_after_alpha = await _vocab_pairs_for_collection(engine, "alpha")
        alpha_sources_after_alpha = await _source_ids_for_collection(engine, "alpha")
        assert alpha_content_pages_after_alpha >= {"paper.alpha-method", "ent.alpha-entity"}, (
            f"alpha should have content pages after its own index; got {alpha_content_pages_after_alpha}"
        )
        assert len(alpha_sections_after_alpha) >= 1, "alpha should have sections after its own index"
        assert alpha_claims_after_alpha == {"paper.alpha-method#c1"}, f"alpha should have its one claim; got {alpha_claims_after_alpha}"
        assert alpha_qualifiers_after_alpha == {("paper.alpha-method#c1", "on_task", 0)}, (
            f"alpha should have its one qualifier; got {alpha_qualifiers_after_alpha}"
        )
        assert len(alpha_vocab_after_alpha) >= 1, "alpha should have vocab rows from its own registries"
        assert alpha_sources_after_alpha == {"ALPHA_KEY"}, f"alpha should have its one source; got {alpha_sources_after_alpha}"

        # Phase 2: index beta into the SAME DB.
        await _index(engine, _beta_knowledge_base(beta_root), collection="beta")

        # Beta content rows are present.
        beta_content_pages = await _page_ids_for_collection(engine, "beta")
        assert "paper.beta-approach" in beta_content_pages, f"beta paper missing; got {beta_content_pages}"

        # Alpha content rows are STILL present: a beta index must never prune alpha's rows.
        alpha_content_pages_after_beta = await _page_ids_for_collection(engine, "alpha")
        assert alpha_content_pages_after_beta == alpha_content_pages_after_alpha, (
            f"REGRESSION: beta index deleted alpha content pages "
            f"(before={alpha_content_pages_after_alpha}, after={alpha_content_pages_after_beta})"
        )

        alpha_sections_after_beta = await _section_ids_for_collection(engine, "alpha")
        assert alpha_sections_after_beta == alpha_sections_after_alpha, (
            f"REGRESSION: beta index deleted alpha sections (before={alpha_sections_after_alpha}, after={alpha_sections_after_beta})"
        )

        # Alpha's claims, qualifiers, vocab and sources must ALSO survive a beta index unscathed.
        alpha_claims_after_beta = await _claim_ids_for_collection(engine, "alpha")
        assert alpha_claims_after_beta == alpha_claims_after_alpha, (
            f"REGRESSION: beta index changed alpha claims (before={alpha_claims_after_alpha}, after={alpha_claims_after_beta})"
        )

        alpha_qualifiers_after_beta = await _qualifier_keys_for_collection(engine, "alpha")
        assert alpha_qualifiers_after_beta == alpha_qualifiers_after_alpha, (
            f"REGRESSION: beta index changed alpha claim_qualifiers "
            f"(before={alpha_qualifiers_after_alpha}, after={alpha_qualifiers_after_beta})"
        )

        alpha_vocab_after_beta = await _vocab_pairs_for_collection(engine, "alpha")
        assert alpha_vocab_after_beta == alpha_vocab_after_alpha, (
            f"REGRESSION: beta index changed alpha vocab (before={alpha_vocab_after_alpha}, after={alpha_vocab_after_beta})"
        )

        alpha_sources_after_beta = await _source_ids_for_collection(engine, "alpha")
        assert alpha_sources_after_beta == alpha_sources_after_alpha, (
            f"REGRESSION: beta index changed alpha sources (before={alpha_sources_after_alpha}, after={alpha_sources_after_beta})"
        )

        alpha_summary_after_beta = await _unit_summary_text(engine, collection="alpha", unit_id="paper.alpha-method")
        assert alpha_summary_after_beta == "Alpha's own indexed summary.", (
            f"REGRESSION: beta index changed or deleted alpha's unit_summaries row; got {alpha_summary_after_beta!r}"
        )

    async def test_alpha_pruning_leaves_beta_untouched(self, engine: AsyncEngine, tmp_path: Path) -> None:  # type: ignore[override]
        """Phase 3: re-index alpha-minus-entity; only alpha's entity page prunes, beta untouched.

        `meta.about` is excluded from the beta survival assertion for the same
        reason as :meth:`test_alpha_rows_survive_beta_index` — it is a global
        singleton whose collection column is overwritten by whoever indexes last.
        """
        alpha_root = tmp_path / "alpha"
        alpha_root.mkdir()
        beta_root = tmp_path / "beta"
        beta_root.mkdir()
        alpha_trimmed_root = tmp_path / "alpha_trimmed"
        alpha_trimmed_root.mkdir()

        # Seed: index both collections.
        await _index(engine, _alpha_knowledge_base(alpha_root), collection="alpha")
        await _index(engine, _beta_knowledge_base(beta_root), collection="beta")

        # Record beta's non-meta content pages as the survival baseline.
        beta_pages_before = await _page_ids_for_collection(engine, "beta")
        beta_sections_before = await _section_ids_for_collection(engine, "beta")
        beta_edges_before = await _edge_src_ids_for_collection(engine, "beta")

        assert "paper.beta-approach" in beta_pages_before, "test pre-condition: beta paper should be present"

        # Phase 3: re-index alpha without the entity page.
        await _index(engine, _alpha_knowledge_base_minus_entity(alpha_trimmed_root), collection="alpha")

        # The entity page should be gone from alpha.
        alpha_page_ids = await _page_ids_for_collection(engine, "alpha")
        assert "ent.alpha-entity" not in alpha_page_ids, "alpha entity page should have been pruned"

        # The paper page should still be in alpha.
        assert "paper.alpha-method" in alpha_page_ids, "alpha paper should still be present"

        # Beta content rows must be completely untouched.
        beta_pages_after = await _page_ids_for_collection(engine, "beta")
        beta_sections_after = await _section_ids_for_collection(engine, "beta")
        beta_edges_after = await _edge_src_ids_for_collection(engine, "beta")

        assert beta_pages_after == beta_pages_before, (
            f"beta pages changed after alpha re-index (before={beta_pages_before}, after={beta_pages_after})"
        )
        assert beta_sections_after == beta_sections_before, (
            f"beta sections changed after alpha re-index (before={beta_sections_before}, after={beta_sections_after})"
        )
        assert beta_edges_after == beta_edges_before, (
            f"beta edges changed after alpha re-index (before={beta_edges_before}, after={beta_edges_after})"
        )


class TestSharedPageIdAcrossKnowledgeBases:
    """Two independent knowledge bases declaring the SAME page id stay isolated, at every table owned under it.

    Under a bare, single-column `pages.id` primary key, the second knowledge base's index would have
    upserted over the first's row under the shared id (one `pages` row, with `sections`/`claims`/
    `claim_qualifiers` cascading from whichever page won the `ON CONFLICT`). The composite
    `(collection, id)` key lets both knowledge bases's `paper.shared-note` — and its section/claim/qualifier/
    unit-summary descendants — coexist, each distinguishable from the other by content alone.
    """

    async def test_shared_page_id_isolates_every_owned_table(self, engine: AsyncEngine, tmp_path: Path) -> None:  # type: ignore[override]
        """Both knowledge bases's `paper.shared-note` rows, and everything owned under them, survive with their OWN distinct content."""
        a_root = tmp_path / "shared_a"
        a_root.mkdir()
        b_root = tmp_path / "shared_b"
        b_root.mkdir()

        await _index(engine, _shared_knowledge_base_a(a_root), collection="shared-a")
        await _index(engine, _shared_knowledge_base_b(b_root), collection="shared-b")

        await _insert_unit_summary(
            engine, collection="shared-a", unit_id="paper.shared-note", unit_kind="page", summary="Knowledge base A's own indexed summary."
        )
        await _insert_unit_summary(
            engine, collection="shared-b", unit_id="paper.shared-note", unit_kind="page", summary="Knowledge base B's own indexed summary."
        )

        # 1. Both page rows exist, each with its OWN title — not the other's, not a clobbered one.
        title_a = await _page_title(engine, collection="shared-a", page_id="paper.shared-note")
        title_b = await _page_title(engine, collection="shared-b", page_id="paper.shared-note")
        assert title_a == "Shared Note — Knowledge base A's Version", f"knowledge base A's paper.shared-note title wrong or missing; got {title_a!r}"
        assert title_b == "Shared Note — Knowledge base B's Version", f"knowledge base B's paper.shared-note title wrong or missing; got {title_b!r}"

        # 2. Sections survive independently, each carrying its OWN body — never the other's.
        sections_a = await _section_bodies_for_page(engine, collection="shared-a", page_id="paper.shared-note")
        sections_b = await _section_bodies_for_page(engine, collection="shared-b", page_id="paper.shared-note")
        assert any("Knowledge base A's exclusive content" in body for body in sections_a), f"knowledge base A's section body missing; got {sections_a}"
        assert any("Knowledge base B's exclusive content" in body for body in sections_b), f"knowledge base B's section body missing; got {sections_b}"
        assert not any("Knowledge base B" in body for body in sections_a), f"knowledge base A's sections leaked knowledge base B's content: {sections_a}"
        assert not any("Knowledge base A" in body for body in sections_b), f"knowledge base B's sections leaked knowledge base A's content: {sections_b}"

        # 3. Claims survive independently, each carrying its OWN text.
        claims_a = await _claim_texts_for_page(engine, collection="shared-a", page_id="paper.shared-note")
        claims_b = await _claim_texts_for_page(engine, collection="shared-b", page_id="paper.shared-note")
        assert claims_a == {"Knowledge base A observes an A-only result."}, f"knowledge base A's claim text wrong or missing; got {claims_a}"
        assert claims_b == {"Knowledge base B observes a B-only result."}, f"knowledge base B's claim text wrong or missing; got {claims_b}"

        # 4. Qualifier rows survive independently, each pointing at its OWN knowledge base's ent.shared-helper — never the other's.
        qualifiers_a = await _qualifier_targets_for_claim(engine, collection="shared-a", claim_id="paper.shared-note#c1")
        qualifiers_b = await _qualifier_targets_for_claim(engine, collection="shared-b", claim_id="paper.shared-note#c1")
        assert qualifiers_a == {("on_task", "shared-a", "ent.shared-helper")}, f"knowledge base A's qualifier wrong or missing; got {qualifiers_a}"
        assert qualifiers_b == {("on_task", "shared-b", "ent.shared-helper")}, f"knowledge base B's qualifier wrong or missing; got {qualifiers_b}"

        # 5. Unit summaries survive independently, each carrying its OWN text.
        summary_a = await _unit_summary_text(engine, collection="shared-a", unit_id="paper.shared-note")
        summary_b = await _unit_summary_text(engine, collection="shared-b", unit_id="paper.shared-note")
        assert summary_a == "Knowledge base A's own indexed summary.", f"knowledge base A's unit summary wrong or missing; got {summary_a!r}"
        assert summary_b == "Knowledge base B's own indexed summary.", f"knowledge base B's unit summary wrong or missing; got {summary_b!r}"

        # 6. api.collection_about answers correctly for BOTH collections — never crossed, never empty.
        about_a = await _collection_about(engine, "shared-a")
        about_b = await _collection_about(engine, "shared-b")
        assert about_a is not None, "knowledge base A's about text is missing"
        assert about_b is not None, "knowledge base B's about text is missing"
        assert "Knowledge base A's own distinct purpose text" in about_a, f"knowledge base A's about text wrong; got {about_a!r}"
        assert "Knowledge base B's own distinct purpose text" in about_b, f"knowledge base B's about text wrong; got {about_b!r}"


class TestVocabIsolation:
    """The same registry term, declared with two different definitions in two knowledge bases, stays isolated.

    `kb.vocab`'s composite `(collection, vocab, value)` key lets two collections each own a
    `(vocab, value)` pair that used to be one single, globally-shared row — `VocabEntries`'s own
    prune-scoping docstring names this exact scenario ("even when both happen to declare the same
    term"). `api.lookup_value(p_collection, facet, term)` must resolve each collection's OWN
    definition, never the other's.
    """

    async def test_lookup_value_returns_each_collections_own_definition(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:  # type: ignore[override]
        """Two knowledge bases both declare `entity_type: widget` with different meanings; each's lookup answers its OWN."""
        a_root = tmp_path / "vocab_a"
        a_root.mkdir()
        b_root = tmp_path / "vocab_b"
        b_root.mkdir()

        await _index(engine, _vocab_knowledge_base_a(a_root), collection="vocab-a")
        await _index(engine, _vocab_knowledge_base_b(b_root), collection="vocab-b")

        async with engine.connect() as conn:
            row_a = (
                await conn.execute(text("SELECT definition FROM api.lookup_value(:c, 'entity_type', 'widget')"), {"c": "vocab-a"})
            ).mappings().one()
            row_b = (
                await conn.execute(text("SELECT definition FROM api.lookup_value(:c, 'entity_type', 'widget')"), {"c": "vocab-b"})
            ).mappings().one()

        assert row_a["definition"] == "Knowledge base A's meaning: a graphical UI control.", (
            f"vocab-a's own definition wrong or missing; got {row_a['definition']!r}"
        )
        assert row_b["definition"] == "Knowledge base B's meaning: a mechanical fastening part.", (
            f"vocab-b's own definition wrong or missing; got {row_b['definition']!r}"
        )

        # The raw rows survive independently too — not merely that the read path happens to answer right.
        vocab_a = await _vocab_pairs_for_collection(engine, "vocab-a")
        vocab_b = await _vocab_pairs_for_collection(engine, "vocab-b")
        assert ("entity_type", "widget") in vocab_a, f"vocab-a's widget row missing; got {vocab_a}"
        assert ("entity_type", "widget") in vocab_b, f"vocab-b's widget row missing; got {vocab_b}"
