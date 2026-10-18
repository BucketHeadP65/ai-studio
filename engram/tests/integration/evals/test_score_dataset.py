"""`ScoreDatasetHandler` over the real search: raw chunks and a digest page map to their source documents.

One collection holds three mirrors, each one raw chunk `<source>:0`, and one digest page whose `source_id` names
the second mirror. The search runs through `SearchHandler` as `kb_reader`, and the page's source is read through
the admin gateway.

What this pins:

* a raw chunk hit scores as its own source document;
* a page hit scores as the source its page record names;
* the means are over every case, a miss included, and no hit is left unmapped.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest
import yaml

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from engram.evals.commands import ScoreDataset
from engram.evals.generation.contracts import AuthoredCase, build_dataset, save_dataset
from engram.evals.handlers import ScoreDatasetHandler
from engram.evals.scoring.contracts import AdminHitSources
from engram.evals.shared.contracts import ExpectedQa, QaCaseMeta, QaInput
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.retrieval.queries import SearchHandler
from engram.testing.fakes.embedding import FakeEmbedding
from tests.integration.test_migrations import _asyncpg_dsn

_COLLECTION = "bench"


async def _seed(migrated: str) -> None:
    admin = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        for source_id, body in (("d1", "a heliotrope in bloom"), ("d2", "nothing to see"), ("d3", "an astrolabe on a desk")):
            await admin.execute(
                "INSERT INTO kb.sources_raw (collection, source_id, title, source_hash, provider, provider_key, meta)"
                " VALUES ($1, $2, $2, 'h', 'beir', $2, '{}'::jsonb)",
                _COLLECTION,
                source_id,
            )
            await admin.execute(
                "INSERT INTO kb.raw_chunks (collection, id, source_id, seq, section_path, heading, body)"
                " VALUES ($1, $2, $3, 0, 'Body'::ltree, 'Body', $4)",
                _COLLECTION,
                f"{source_id}:0",
                source_id,
                body,
            )
        await admin.execute(
            "INSERT INTO kb.pages (collection, id, page_type, slug, title, status, card_text, body_hash, facets, source_id)"
            " VALUES ($1, 'dg.two', 'digest', 'dg.two', 'Digest two', 'active', 'sextant sextant sextant', 'h', '{}'::jsonb, 'd2')",
            _COLLECTION,
        )
    finally:
        await admin.close()


def _dataset(tmp_path: Path) -> Path:
    cases = [
        AuthoredCase(
            question=QaInput(question="heliotrope"),
            expected=ExpectedQa(answer="", expected_source_ids=("d1",)),
            meta=QaCaseMeta(judgments={"d1": 1}),
            case_name="q1",
        ),
        AuthoredCase(
            question=QaInput(question="sextant"),
            expected=ExpectedQa(answer="", expected_source_ids=("d2",)),
            meta=QaCaseMeta(judgments={"d2": 1}),
            case_name="q2",
        ),
        AuthoredCase(
            question=QaInput(question="astrolabe"),
            expected=ExpectedQa(answer="", expected_source_ids=("d1",)),
            meta=QaCaseMeta(judgments={"d1": 1}),
            case_name="q3",
        ),
    ]
    path = tmp_path / "bench.yaml"
    save_dataset(build_dataset(cases), path)
    return path


async def test_raw_and_page_hits_score_as_their_source_documents(migrated: str, reader_url: str, tmp_path: Path) -> None:
    await _seed(migrated)
    reader = create_async_engine(reader_url, poolclass=NullPool)
    admin = create_async_engine(migrated, poolclass=NullPool)
    try:
        handler = ScoreDatasetHandler(
            SearchHandler(ReadOnlySqlGateway(reader, collection=_COLLECTION), FakeEmbedding()), AdminHitSources(ReadOnlySqlGateway(admin))
        )
        report = await handler.handle(ScoreDataset(dataset=str(_dataset(tmp_path)), collection=_COLLECTION, out=str(tmp_path / "scores")))
    finally:
        await reader.dispose()
        await admin.dispose()

    ranked = {case["name"]: case["ranked"] for case in yaml.safe_load(Path(report.out).read_text(encoding="utf-8"))["cases"]}
    assert ranked == {"q1": ["d1"], "q2": ["d2"], "q3": ["d3"]}
    assert report.cases == 3
    assert report.unmapped_hits == 0
    for metric in ("recall@1", "mrr", "ndcg@10"):
        assert report.means[metric] == pytest.approx(2 / 3)
