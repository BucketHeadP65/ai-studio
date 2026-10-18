r"""Top-k retrieval parity smoke: the same knowledge base searched under the `pg_search` and `pg_textsearch` dialects.

The dual-dialect BM25 seam lets ``api.search`` run against either ParadeDB's ``pg_search`` or
``pg_textsearch``. This script guards that the two backends
rank a fixed query set *the same way* by comparing the top-k id lists each returns and asserting
their overlap clears a threshold — a cheap regression net for ranking drift between dialects.

Two cleanly separable parts:

* :func:`top_k_overlap` — a **pure** function (no DB, no network) computing the Jaccard overlap
  of two top-k id lists. This is the unit-tested surface.
* :func:`main` — a thin CLI that connects to both backends (reusing engram's existing search
  path: :func:`~studio.knowledge_base.settings.fetch_application_settings` → :func:`~studio.knowledge_base.container.create_container` →
  the wireup-resolved :class:`~engram.retrieval.queries.SearchHandler`), runs the fixed query set
  against each, computes overlap via :func:`top_k_overlap`, and exits non-zero on any divergence
  below the threshold.

Running it needs two live databases: the configured one from ``--knowledge-base`` (or the working directory, when
it holds a knowledge-base-schema.json) and a ``pg_textsearch`` one from ``PARITY_DB_URL`` (plus optional
``PARITY_DB_READER_URL``)::

    PARITY_DB_URL=postgresql+asyncpg://… uv run python scripts/dialect_parity_smoke.py \
        [--knowledge-base PATH] [-k 12] [--threshold 0.7]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from typing import TYPE_CHECKING

from core.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from engram.config import KnowledgeBaseSettings
    from engram.retrieval.domain import Search, SqlResult

    from studio.knowledge_base.settings import ApplicationSettings

    SettingsFactory = Callable[[], ApplicationSettings]

_logger = get_logger(__name__)

DEFAULT_THRESHOLD = 0.7
"""Minimum top-k id overlap (Jaccard) two backends must clear to count as parity."""

DEFAULT_K = 12
"""How many top hits each backend's result is truncated to before comparison (mirrors ``search -k``)."""

# A small, fixed query set spanning the knowledge base' facets (concepts, comparisons, methods) so the
# parity check exercises representative ranking paths rather than a single lucky query.
QUERY_SET: tuple[str, ...] = (
    "retrieval augmented generation",
    "vector search recall vs latency tradeoff",
    "transformer attention mechanism",
    "evaluation metrics for ranking",
    "fine-tuning vs prompting",
)


def top_k_overlap(ids_a: Sequence[str], ids_b: Sequence[str], k: int) -> float:
    """Compute the Jaccard overlap of the top-``k`` ids of two ranked result lists.

    Each list is truncated to its first ``k`` ids and de-duplicated to a set; the result is
    ``|A ∩ B| / |A ∪ B|``. Two empty top-k sets (including ``k == 0``) count as full overlap
    (``1.0``) — there is no divergence to report between two empty result sets.

    Args:
        ids_a: The first backend's ranked id list (best first).
        ids_b: The second backend's ranked id list (best first).
        k: How many leading ids of each list to compare; the rest are ignored.

    Returns:
        The overlap fraction in ``[0.0, 1.0]`` — ``1.0`` for identical top-k sets, ``0.0`` for
        disjoint ones.

    Raises:
        ValueError: If ``k`` is negative.
    """
    if k < 0:
        msg = f"k must be non-negative, got {k}"
        raise ValueError(msg)
    top_a = set(ids_a[:k])
    top_b = set(ids_b[:k])
    union = top_a | top_b
    if not union:
        return 1.0
    return len(top_a & top_b) / len(union)


def _hit_ids(result: SqlResult) -> list[str]:
    """Project an ``api.search`` result into its ranked list of unit ids (the ``id`` column)."""
    return [str(row["id"]) for row in result.rows]


async def _search_ids(settings_factory: SettingsFactory, knowledge_base: KnowledgeBaseSettings, query: Search) -> list[str]:
    """Run one :class:`~engram.retrieval.domain.Search` against a backend, returning its top hit ids.

    Builds a wireup container from ``settings_factory()`` and resolves the
    :class:`~engram.retrieval.queries.SearchHandler` exactly as the ``engram search`` CLI does, so
    the comparison rides the production search path (embedding + dialect-selected SQL), not a
    re-implementation. The container is disposed after the read.
    """
    from engram.retrieval.queries import SearchHandler  # noqa: PLC0415

    from studio.knowledge_base.container import create_container  # noqa: PLC0415 — keep the DI/DB graph off the unit-tested import path

    container = create_container(settings_factory(), knowledge_base=knowledge_base)
    try:
        handler = await container.get(SearchHandler)
        result = await handler(query)
    finally:
        await container.close()
    return _hit_ids(result)


def _build_settings_pair(knowledge_base_arg: str) -> tuple[ApplicationSettings, ApplicationSettings, KnowledgeBaseSettings]:
    """Build the (configured, `pg_textsearch`) settings pair plus the knowledge base both act on.

    The configured side reuses :func:`~studio.knowledge_base.settings.fetch_application_settings`. The other side
    layers the `pg_textsearch` dialect over a copy: ``retrieval.dialect=pg_textsearch`` plus the admin and reader URLs,
    sourced from ``PARITY_DB_URL`` / ``PARITY_DB_READER_URL`` so the script needs no separate
    config file.
    """
    import os  # noqa: PLC0415 — only the CLI path reads the second database's URLs

    from pathlib import Path  # noqa: PLC0415 — same: keep this off the unit-tested import path

    from studio.knowledge_base.settings import (  # noqa: PLC0415
        ApplicationSettings,
        DatabaseSettings,
        DbDialect,
        KnowledgeBaseSettings,
        RetrievalSettings,
        fetch_application_settings,
    )

    local = fetch_application_settings()
    knowledge_base = KnowledgeBaseSettings(path=Path(knowledge_base_arg).resolve())
    parity_url = os.environ["PARITY_DB_URL"]
    parity_reader_url = os.environ.get("PARITY_DB_READER_URL", parity_url)
    parity_infrastructure = local.infrastructure.model_copy(
        update={"database": DatabaseSettings(url=parity_url, reader_url=parity_reader_url)},
    )
    parity_retrieval = RetrievalSettings(dialect=DbDialect.PG_TEXTSEARCH)
    parity = ApplicationSettings(
        retrieval=parity_retrieval,
        ingestion=local.ingestion,
        sources=local.sources,
        evals=local.evals,
        infrastructure=parity_infrastructure,
    )
    return local, parity, knowledge_base


async def _run_parity(*, knowledge_base_arg: str, k: int, threshold: float) -> int:
    """Run the fixed query set against both backends and report per-query overlap.

    Returns:
        ``0`` when every query clears ``threshold``, else ``1`` (the count of failing queries is
        logged alongside each divergence).
    """
    from engram.kb import contracts as kb  # noqa: PLC0415 — DB-adjacent import stays off the unit path
    from engram.retrieval.domain import Search  # noqa: PLC0415 — DB-adjacent import stays off the unit path

    local_settings, parity_settings, knowledge_base = _build_settings_pair(knowledge_base_arg)
    collection = kb.load_knowledge_base_schema(knowledge_base.path).retrieval.collection
    failures = 0
    for query_text in QUERY_SET:
        message = Search(query=query_text, k=k, collection=collection)
        local_ids = await _search_ids(lambda: local_settings, knowledge_base, message)
        parity_ids = await _search_ids(lambda: parity_settings, knowledge_base, message)
        overlap = top_k_overlap(local_ids, parity_ids, k=k)
        if overlap < threshold:
            failures += 1
            only_local = [i for i in local_ids[:k] if i not in set(parity_ids[:k])]
            only_parity = [i for i in parity_ids[:k] if i not in set(local_ids[:k])]
            _logger.error(
                "DIVERGENCE {overlap:.2f} < {threshold:.2f} for {query!r}, only local: {only_local}, only parity: {only_parity}",
                overlap=overlap,
                threshold=threshold,
                query=query_text,
                only_local=only_local,
                only_parity=only_parity,
            )
        else:
            _logger.info("OK {overlap:.2f} >= {threshold:.2f} for {query!r}", overlap=overlap, threshold=threshold, query=query_text)
    if failures:
        _logger.error("Parity FAILED: {failures}/{total} queries below threshold {threshold:.2f}", failures=failures, total=len(QUERY_SET), threshold=threshold)
    else:
        _logger.info("Parity OK: all {total} queries >= threshold {threshold:.2f}", total=len(QUERY_SET), threshold=threshold)
    return 1 if failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: compare top-k parity across both backends and return a process exit code.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on full parity, ``1`` when any query's overlap falls below the threshold.
    """
    parser = argparse.ArgumentParser(description="Top-k retrieval parity smoke: the same search under the pg_search and pg_textsearch dialects.")
    parser.add_argument(
        "--knowledge-base", default=".", help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."
    )
    parser.add_argument("-k", type=int, default=DEFAULT_K, help="Top hits to compare per query.")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="Minimum top-k overlap to pass.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return asyncio.run(_run_parity(knowledge_base_arg=args.knowledge_base, k=args.k, threshold=args.threshold))


if __name__ == "__main__":  # pragma: no cover — needs two live databases
    sys.exit(main())
