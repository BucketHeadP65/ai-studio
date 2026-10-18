r"""`api.search` over a real, indexed bed (local Postgres by default, ParadeDB opt-in) — BM25, hybrid, facets, only_current.

The full read path: a mini `knowledge_base_builder` knowledge base with distinct, searchable pages →
``memory index`` (the public CLI, real PG Unit-of-Work + :class:`FakeEmbedding`, the only
fake — exactly the e2e seeding approach) → queries through the production read-only
:class:`~engram.retrieval.infrastructure.gateway.ReaderSqlGateway` (the ``kb_reader`` role; the ``reader_url``
fixture). It exercises the `api.search` function (0024's `facets` jsonb shape) end to end against
real BM25 (pg_search) + pgvector RRF fusion:

* BM25-only (`query_vec` NULL) ranks the keyword-relevant page/claim first;
* hybrid (a :class:`FakeEmbedding`-derived query vector) runs the RRF fusion and returns rows
  — fake vectors make the *exact* ranking hash-arbitrary, so we assert the function executes,
  returns `<= k` rows, and surfaces the BM25-relevant id;
* facet pre-filters (`facets` jsonb / `level`) restrict the candidate space before ranking;
* `only_current` (the default) excludes a superseded page that otherwise matches;
* `k` is honoured (the > 50 hard cap is proven in `test_guardrails.py`).

The whole module skips when Docker is unavailable (the `pg` fixture, via `migrated`, raises
`pytest.skip`). Tests are *synchronous*: `runner.invoke` drives the Typer `index` command
(which calls `asyncio.run`), so the body has no running loop; the gateway reads each wrap their
own `asyncio.run` — the same shape as `tests/e2e/test_index_and_search.py`.
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
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.testing.fakes.embedding import FakeEmbedding, _vector
from engram.testing.infrastructure.knowledge_base_db import BED_BM25_EXTENSION
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import create_async_engine
from typer.testing import CliRunner

from studio.knowledge_base.cli import app
from studio.knowledge_base.indexing.infrastructure.validation_report_sink import FileValidationReportSink
from studio.knowledge_base.wiring.contracts import AdminSqlGateway
from studio.knowledge_base.wiring.indexing import KnowledgeBaseUnitOfWorkFactory

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from pathlib import Path

    from engram.retrieval.domain import SqlResult
    from sqlalchemy.ext.asyncio import AsyncEngine

# Most of this module is dialect-agnostic (membership, facet pre-filter, only_current, hit_level, k)
# and runs on BOTH beds. Only the single strict rank-0 assertion below is `pg_search`-ranking-specific:
# `pg_textsearch`'s RRF can rank a different leg first (a raw-chunk hit outscores the card for the same
# query, a faithful dialect difference, not a regression), so that ONE test is gated to the ParadeDB
# bed. Everything else exercises the analog default bed too.
_PG_SEARCH_RANK_ORDER = pytest.mark.skipif(
    BED_BM25_EXTENSION != "pg_search",
    reason="strict pg_search rank-0 order; pg_textsearch ranks a different leg first (faithful dialect difference)",
)

runner = CliRunner()

# ── three distinct, searchable papers + one superseded variant ──────────────
# card_text = "{kind}: {title} — {summary}" + a facet line (the BM25-indexed page text); each
# page's claim text repeats the distinctive terms so both card- and claim-level legs can hit.

_PAPER_GRAPH = """\
---
id: paper.graph-cost
type: paper
title: Graph Indexing Token Cost
summary: Building a knowledge graph index incurs substantial token cost during construction.
status: synthesized
zotero_key: GRAPHKEY1
year: 2025
paper_type: method
source_hash: g001
tasks: [long-doc-qa]
stages: [indexing]
---

## TL;DR

Graph index construction spends many tokens extracting entities and relations.

## Findings

- Graph indexing incurs substantial token cost during construction. ^c1
    - loc:: §3.1
    - kind:: observation
"""

_PAPER_CACHE = """\
---
id: paper.vector-cache
type: paper
title: Vector Caching of Hot Passages
summary: Caching hot passage embedding vectors slashes retrieval latency on repeat queries.
status: synthesized
zotero_key: CACHEKEY1
year: 2024
paper_type: method
tasks: [multi-hop-qa]
stages: [retrieval]
source_hash: c001
---

## TL;DR

A vector cache keeps frequently retrieved passage embeddings hot.

## Findings

- Caching hot passage vectors reduces retrieval latency substantially. ^c1
    - loc:: §4.2
    - kind:: observation
"""

_PAPER_SPEECH = """\
---
id: paper.speech-unrelated
type: paper
title: Acoustic Phoneme Recognition
summary: Acoustic models transcribe spoken phonemes directly from raw audio waveforms.
status: synthesized
zotero_key: SPEECHKEY1
year: 2023
paper_type: analysis
tasks: [fact-verification]
stages: [generation]
source_hash: s001
---

## TL;DR

Waveform features feed an acoustic model that emits phoneme posteriors.

## Findings

- Acoustic models transcribe phonemes from raw audio waveforms. ^c1
    - loc:: §2
    - kind:: observation
"""

# A superseded older version of the graph paper — same searchable terms, so it competes in
# BM25 ranking, but only_current (the api.search default) must exclude it.
_PAPER_GRAPH_OLD = """\
---
id: paper.graph-cost-v0
type: paper
title: Graph Indexing Token Cost (early draft)
summary: An early note on graph index construction token cost, later superseded.
status: synthesized
superseded_by: "[[paper.graph-cost]]"
zotero_key: GRAPHKEY0
year: 2024
paper_type: method
tasks: [long-doc-qa]
stages: [indexing]
source_hash: g000
---

## TL;DR

Early measurements of graph indexing token cost.
"""


# A paper whose section body and raw mirror both run far past the 480-character snippet bound, in
# multibyte text: the generated `api.search` must hand back a bounded excerpt, not the whole unit,
# and must cut it on a character boundary rather than a byte one. "Fluxidrome" is invented, so it
# appears in exactly this one paper and cannot be crowded out of a keyword ranking.
_LONG_SECTION_BODY = "Το fluxidrome μετρά τη διασπορά των ενσωματώσεων σε πολύγλωσσα σώματα κειμένων. " * 12
_LONG_RAW_BODY = "Fluxidrome μετρήσεις σε ακατέργαστο κείμενο, με πλήρη παραπομπή και αναλυτικό σχολιασμό. " * 12

_PAPER_LONG = f"""\
---
id: paper.fluxidrome
type: paper
title: Fluxidrome Dispersion
summary: Fluxidrome measures embedding dispersion across multilingual corpora.
status: synthesized
zotero_key: LONGKEY1
year: 2025
paper_type: analysis
tasks: [multi-hop-qa]
stages: [retrieval]
source_hash: l001
---

## TL;DR

Fluxidrome scores embedding dispersion.

## Detail

{_LONG_SECTION_BODY}

## Findings

- Fluxidrome dispersion tracks multilingual retrieval quality. ^c1
    - loc:: §5
    - kind:: observation
"""


def _mirror(source_key: str, source_hash: str, body: str) -> str:
    """A minimal raw mirror — its only job here is to satisfy the pages.zotero_key FK."""
    return f"---\nsource_key: {source_key}\nprovider: example-library\nsource_hash: {source_hash}\n---\n# Mirror\n\n## 1 Body\n\n{body}\n"


def _build_knowledge_base(root: Path) -> Path:
    """Lay down the four papers, their mirrors, registries, and meta stubs."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_unit_document("Papers", "graph-cost", _PAPER_GRAPH)
    builder.add_unit_document("Papers", "vector-cache", _PAPER_CACHE)
    builder.add_unit_document("Papers", "speech-unrelated", _PAPER_SPEECH)
    builder.add_unit_document("Papers", "graph-cost-v0", _PAPER_GRAPH_OLD)
    builder.add_unit_document("Papers", "fluxidrome", _PAPER_LONG)
    builder.add_source_mirror("graph-cost", _mirror("GRAPHKEY1", "g001", "Graph index construction token cost."))
    builder.add_source_mirror("vector-cache", _mirror("CACHEKEY1", "c001", "Vector caching of hot passages."))
    builder.add_source_mirror("speech-unrelated", _mirror("SPEECHKEY1", "s001", "Acoustic phoneme recognition."))
    builder.add_source_mirror("graph-cost-v0", _mirror("GRAPHKEY0", "g000", "Early graph indexing token cost."))
    builder.add_source_mirror("fluxidrome", _mirror("LONGKEY1", "l001", _LONG_RAW_BODY))
    builder.add_vocabulary()
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema()
    return root


class _Factory:
    """A `ctx.obj` factory whose bus runs a real-PG, fake-embed index handler (e2e shape)."""

    def __init__(self, engine: AsyncEngine, knowledge_base: Path) -> None:
        self._reader = FilesystemKnowledgeBaseReader(knowledge_base)
        handler = IndexKnowledgeBaseHandler(
            self._reader, FakeEmbedding(), KnowledgeBaseUnitOfWorkFactory(engine), FileValidationReportSink(knowledge_base), spec=load_knowledge_base_schema(knowledge_base)
        )
        self._bus = MessageBus({IndexKnowledgeBase: handler}, {})
        self._gateway = AdminSqlGateway(engine)  # the identity announce's own read, over the same admin engine

    def __call__(self) -> _Container:
        return _Container(self._bus, self._gateway, self._reader)


class _Container:
    """Minimal async container exposing the message bus and identity-announce reads to the CLI command."""

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


@pytest.fixture
def indexed(migrated: str, tmp_path: Path) -> str:
    """Index the mini knowledge base into the freshly-migrated DB; return the admin URL.

    Drives the real `memory index` over a real PG Unit-of-Work with a `FakeEmbedding` (so
    every page/section/claim gets a deterministic vector), the same write path the e2e test
    proves. The index engine has the pgvector codec registered (so halfvec embeddings write);
    the returned URL is the admin URL — search reads go through `reader_url` (same DB, the
    `kb_reader` role).
    """
    knowledge_base = _build_knowledge_base(tmp_path)
    engine = create_async_engine(migrated, poolclass=NullPool)

    async def _register() -> None:
        await orm.register_pgvector(engine)

    asyncio.run(_register())
    result = runner.invoke(app, ["index", "--knowledge-base", str(knowledge_base)], obj=_Factory(engine, knowledge_base))
    assert result.exit_code == 0, result.stdout
    return migrated


def _run[T](coro: Coroutine[object, object, T]) -> T:
    """Drive a coroutine from a synchronous test body (no loop is running)."""
    return asyncio.run(coro)


def _search(reader_url: str, sql: str, params: dict[str, object], *, cap: int = 50) -> SqlResult:
    """Run one `api.search` (or other api) call through the reader gateway."""

    async def _go() -> SqlResult:
        engine = create_async_engine(reader_url, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            return await ReadOnlySqlGateway(engine).rows(sql, params, cap=cap)
        finally:
            await engine.dispose()

    return _run(_go())


def _halfvec_literal(vector: list[float]) -> str:
    """Format a vector as the pgvector `halfvec` text literal `[f1,f2,…]` (the bound qvec)."""
    return "[" + ",".join(repr(component) for component in vector) + "]"


def _ids(result: SqlResult) -> list[str]:
    return [str(row["id"]) for row in result.rows]


def _page_ids(result: SqlResult) -> set[str]:
    return {str(row["page_id"]) for row in result.rows}


# Bind every api.search arg by name (positional-with-defaults). The query vector is a halfvec
# text literal cast `CAST(:qvec AS text)::halfvec` — the text cast pins asyncpg's param type so
# the registered pgvector codec does not binary-encode the string (the same shape production's
# SearchHandler uses). :qvec NULL → BM25-only leg; only_current is bound so a test can flip it.
# `facets` is a jsonb bag of facet name -> value-list narrowing (production's SearchHandler binds
# it the same way, `json.dumps(query.facets)` cast in SQL).
_SEARCH_SQL = (
    "SELECT * FROM api.search("
    "query => :q, k => :k, query_vec => CAST(:qvec AS text)::halfvec, "
    "facets => CAST(:facets AS text)::jsonb, level => :level, only_current => :only_current)"
)


def _params(
    query: str,
    *,
    k: int = 12,
    qvec: str | None = None,
    facets: dict[str, list[str]] | None = None,
    level: str | None = None,
    only_current: bool = True,
) -> dict[str, object]:
    return {"q": query, "k": k, "qvec": qvec, "facets": json.dumps(facets or {}), "level": level, "only_current": only_current}


@pytest.fixture
def reader(indexed: str, reader_url: str) -> str:
    """The reader URL over the *indexed* DB (forces indexing before any search read)."""
    del indexed  # ordering dependency: index must run before we read
    return reader_url


class TestBm25Search:
    @_PG_SEARCH_RANK_ORDER
    def test_keyword_query_ranks_the_relevant_page_first(self, reader: str):
        result = _search(reader, _SEARCH_SQL, _params("graph indexing token cost", k=5))

        assert result.rows  # BM25 found matches
        assert "paper.graph-cost" in _page_ids(result)
        # The graph paper's own page/claim outranks the unrelated speech paper.
        assert result.rows[0]["page_id"] == "paper.graph-cost"

    def test_disjoint_query_returns_the_other_paper(self, reader: str):
        result = _search(reader, _SEARCH_SQL, _params("hot passage caching retrieval latency", k=5))

        assert "paper.vector-cache" in _page_ids(result)
        assert "paper.speech-unrelated" not in _page_ids(result)


class TestHybridSearch:
    def test_hybrid_executes_and_returns_capped_rows_including_the_bm25_id(self, reader: str):
        query = "graph indexing token cost"
        qvec = _halfvec_literal(_vector(query, 1024))  # the deterministic fake query vector

        result = _search(reader, _SEARCH_SQL, _params(query, k=5, qvec=qvec), cap=5)

        # Fake vectors make exact ranking hash-arbitrary; assert it RAN, respected k, and the
        # BM25-relevant page is present among the fused hits.
        assert 0 < len(result.rows) <= 5
        assert "paper.graph-cost" in _page_ids(result)


class TestFacetPrefilter:
    def test_task_filter_restricts_to_matching_pages(self, reader: str):
        # "indexing" appears in both graph papers; the multi-hop-qa task is only the cache paper,
        # which has no graph terms — so a graph query under facets={"tasks": ["multi-hop-qa"]}
        # yields no graph hits.
        result = _search(reader, _SEARCH_SQL, _params("graph indexing token cost", k=10, facets={"tasks": ["multi-hop-qa"]}))

        assert "paper.graph-cost" not in _page_ids(result)

    def test_level_filter_restricts_to_one_tier(self, reader: str):
        result = _search(reader, _SEARCH_SQL, _params("graph indexing token cost", k=10, level="claim"))

        assert result.rows
        assert {str(row["hit_level"]) for row in result.rows} == {"claim"}


class TestOnlyCurrent:
    def test_default_excludes_the_superseded_page(self, reader: str):
        result = _search(reader, _SEARCH_SQL, _params("graph indexing token cost early draft", k=10, only_current=True))

        assert "paper.graph-cost-v0" not in _ids(result)

    def test_only_current_false_includes_the_superseded_page(self, reader: str):
        result = _search(reader, _SEARCH_SQL, _params("graph indexing token cost early draft", k=10, only_current=False))

        assert "paper.graph-cost-v0" in _ids(result)


class TestKHonoured:
    def test_k_limits_the_returned_rows(self, reader: str):
        # "indexing" matches both graph papers' cards + the graph claim; k=1 returns exactly one.
        result = _search(reader, _SEARCH_SQL, _params("graph indexing", k=1, only_current=False), cap=50)

        assert len(result.rows) == 1


# ── the per-leg facts and the bounded snippet the generated body projects ───
# `api.search` returns, beside the fused `score`, the rank each leg gave the row and the semantic
# leg's raw distance. The vector legs carry no match filter (they rank every pooled row with an
# embedding), so a query vector makes every candidate semantically ranked, and the keyword legs add
# a `lexical_rank` on top for the rows the query's terms actually matched. These are facts about
# which leg selected a row, never a cutoff: nothing here is filtered out for scoring too low.


def _column_names(result: SqlResult) -> tuple[str, ...]:
    return result.columns


class TestComponentRanks:
    """Each hit says which legs ranked it, and the function gates on none of it."""

    def test_the_function_publishes_the_per_leg_columns_beside_the_fused_score(self, reader: str):
        result = _search(reader, _SEARCH_SQL, _params("graph indexing token cost", k=5))

        assert _column_names(result) == (
            "id",
            "src_collection",
            "hit_level",
            "page_id",
            "page_title",
            "snippet",
            "score",
            "lexical_rank",
            "vector_rank",
            "vector_distance",
            "snippet_truncated",
        )

    def test_a_keyword_only_search_ranks_on_the_lexical_leg_alone(self, reader: str):
        # Exactly one card carries these terms, and the keyword leg numbers it 1 with nothing on the
        # semantic side, since a NULL query vector empties every vector leg.
        result = _search(reader, _SEARCH_SQL, _params("graph indexing token cost", k=50, level="card"))

        assert [(str(row["id"]), row["lexical_rank"], row["vector_rank"], row["vector_distance"]) for row in result.rows] == [
            ("paper.graph-cost", 1, None, None)
        ]

    def test_the_keyword_leg_numbers_its_matches_from_one_in_fused_order(self, reader: str):
        # Four claims match, one per paper. With only the keyword leg contributing, RRF is monotonic in
        # the rank, so the fused order IS the leg's own order and the ranks read 1, 2, 3, 4 exactly.
        result = _search(reader, _SEARCH_SQL, _params("retrieval latency token cost phonemes", k=50, level="claim", only_current=False))

        assert [row["lexical_rank"] for row in result.rows] == [1, 2, 3, 4]
        assert str(result.rows[0]["id"]) == "paper.graph-cost#c1"
        assert all(row["vector_rank"] is None for row in result.rows)

    def test_a_hybrid_search_ranks_a_matching_row_on_both_legs(self, reader: str):
        query = "graph indexing token cost"
        qvec = _halfvec_literal(_vector(query, 1024))

        result = _search(reader, _SEARCH_SQL, _params(query, k=50, qvec=qvec, level="card"), cap=50)

        # The vector leg carries no match filter, so it ranks every pooled card: the ranks it hands
        # back are exactly 1..N, no gap and no duplicate, and its distances rise with them.
        assert result.rows
        assert all(row["vector_rank"] is not None and row["vector_distance"] is not None for row in result.rows)
        ordered = sorted(
            (
                (int(str(row["vector_rank"])), float(str(row["vector_distance"])), str(row["id"]), row["lexical_rank"])
                for row in result.rows
            ),
            key=lambda entry: entry[0],
        )
        assert [rank for rank, _, _, _ in ordered] == list(range(1, len(result.rows) + 1))
        distances = [distance for _, distance, _, _ in ordered]
        assert distances == sorted(distances)
        # The one card the keyword leg also matched keeps the same lexical rank it had with no vector.
        assert [(unit_id, lexical) for _, _, unit_id, lexical in ordered if lexical is not None] == [("paper.graph-cost", 1)]

    def test_a_query_no_term_matches_still_returns_vector_only_candidates(self, reader: str):
        query = "zzqxwvunobtainium phlogiston"  # present in no card, section, claim or raw chunk
        qvec = _halfvec_literal(_vector(query, 1024))

        result = _search(reader, _SEARCH_SQL, _params(query, k=6, qvec=qvec), cap=6)

        assert result.rows, "a semantic-only query is not filtered away by any threshold"
        assert len(result.rows) <= 6
        for row in result.rows:
            assert row["lexical_rank"] is None  # no keyword support behind any of them
            assert row["vector_rank"] is not None
            assert row["vector_distance"] is not None
        # The row says which leg selected it and nothing more: no relevance verdict rides along.
        assert "support" not in _column_names(result)
        assert "relevant" not in _column_names(result)

    def test_the_same_query_without_a_vector_returns_nothing_rather_than_a_low_scoring_match(self, reader: str):
        result = _search(reader, _SEARCH_SQL, _params("zzqxwvunobtainium phlogiston", k=6))

        assert result.rows == ()

    def test_k_still_bounds_the_fused_rows_when_both_legs_run(self, reader: str):
        query = "graph indexing token cost"
        qvec = _halfvec_literal(_vector(query, 1024))

        result = _search(reader, _SEARCH_SQL, _params(query, k=2, qvec=qvec), cap=50)

        assert 0 < len(result.rows) <= 2


class TestBoundedSnippets:
    """A snippet is an excerpt for choosing a fetch, never the unit's full text."""

    def test_an_oversized_section_comes_back_as_a_bounded_excerpt(self, reader: str):
        result = _search(reader, _SEARCH_SQL, _params("fluxidrome dispersion", k=10, level="section"))

        assert result.rows
        oversized = [dict(row) for row in result.rows if row["snippet_truncated"]]
        assert oversized, "the long multibyte section body was not recognized as oversized"
        for row in oversized:
            snippet = str(row["snippet"])
            assert len(snippet) == 480  # 479 characters plus the ellipsis that says it was cut
            assert snippet.endswith("…")
            assert snippet.encode("utf-8").decode("utf-8") == snippet  # cut on a character, never inside one

    def test_every_hit_of_every_level_stays_within_the_snippet_bound(self, reader: str):
        query = "fluxidrome dispersion multilingual"
        qvec = _halfvec_literal(_vector(query, 1024))

        result = _search(reader, _SEARCH_SQL, _params(query, k=20, qvec=qvec), cap=50)

        assert result.rows
        for row in result.rows:
            snippet = row["snippet"]
            assert snippet is None or len(str(snippet)) <= 480
            assert isinstance(row["snippet_truncated"], bool)

    def test_a_short_unit_keeps_its_whole_text_and_says_it_was_not_cut(self, reader: str):
        result = _search(reader, _SEARCH_SQL, _params("hot passage caching retrieval latency", k=10, level="claim"))

        assert result.rows
        for row in result.rows:
            assert row["snippet_truncated"] is False
            assert not str(row["snippet"]).endswith("…")
