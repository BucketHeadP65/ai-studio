"""Search hits re-ranked by a decision model: the hits it judges likelier to help answer the query come first."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.ai.decision_models import DecisionModelQuestions, RequestShape, decision_model
from core.ai.retrieval.contracts import Fetch
from core.observability import get_logger

from .infrastructure.gateway import read_snapshot, release_snapshot

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.ai.retrieval.contracts import Row, Search, SqlResult, SupportsFetch, SupportsSearch
    from core.settings import GatewaySettings
    from pydantic_ai.capabilities import AbstractCapability

    from engram.config import PassageText, RerankSettings

_CHARS_PER_TOKEN = 4
_FRAMING_TOKENS = 400

_logger = get_logger(__name__)


@dataclass(frozen=True)
class Reranking:
    """How a search's hits are re-ranked: who judges them, on which text, in how many requests, and how fast."""

    questions: DecisionModelQuestions
    """Who judges each hit."""
    passage: PassageText = "snippet"
    """What is judged for each hit: its snippet, or its whole text cut to fit."""
    requests: RequestShape = "per_hit"
    """One request per hit, or one for every hit."""
    deadline_seconds: float = 3.0
    """How long the whole re-rank may take, chunk fetching included, before the search answers in its own order.

    The clock starts when the search answers, so a slow search never shortens it."""
    max_input_tokens: int = 32_000
    """The most tokens one request may carry."""

    def over[S: Search](self, search: SupportsSearch[S], fetch: SupportsFetch) -> RerankedSearch[S]:
        """`search` with its hits re-ranked, fetching each hit's text through `fetch` in chunk mode.

        Args:
            search: The search handler whose hits are re-ranked.
            fetch: The fetch handler a hit's whole text is read through.

        Returns:
            The re-ranking search handler.
        """
        return RerankedSearch(search=search, fetch=fetch, reranking=self)


@dataclass(frozen=True)
class RerankedSearch[S: Search]:
    """A search handler whose hits come back in the order a decision model judges them relevant. It drops nothing.

    Each hit keeps its fused `score` and gains `rerank_score`, its probability. A single hit is judged like any other. A failure, a missed deadline, or no hits returns the
    search's own result unchanged.
    """

    search: SupportsSearch[S]
    """The search handler whose hits are re-ranked."""
    fetch: SupportsFetch
    """The fetch handler a hit's whole text is read through."""
    reranking: Reranking
    """How the hits are re-ranked."""

    async def __call__(self, query: S) -> SqlResult:
        """Run the search, then put its hits in the order of their probability.

        Args:
            query: The search to run.

        Returns:
            The hits, re-ranked, or the search's own result when the re-rank answered nothing.
        """
        loop = asyncio.get_running_loop()
        async with read_snapshot():
            result = await self.search(query)
            if not result.rows:
                return result
            started = loop.time()
            keys = [f"h{index}" for index in range(len(result.rows))]
            try:
                texts = await asyncio.wait_for(self._texts(result.rows), self.reranking.deadline_seconds)
            except TimeoutError:
                _logger.warning(
                    "re-rank answered in the search's own order: no chunks within {seconds}s", seconds=self.reranking.deadline_seconds
                )
                return result
            # No transaction stays open through the model call. A read after it sees a new snapshot.
            await release_snapshot()
        remaining = self.reranking.deadline_seconds - (loop.time() - started)
        limit = self._limit(query.query, len(texts))
        probabilities = await self.reranking.questions.relevance(
            query.query,
            {key: text[:limit] for key, text in zip(keys, texts, strict=True)},
            requests=self.reranking.requests,
            deadline_seconds=max(remaining, 0.001),
        )
        if probabilities is None:
            return result
        order = sorted(range(len(keys)), key=lambda index: -probabilities[keys[index]])
        rows = tuple({**result.rows[index], "rerank_score": probabilities[keys[index]]} for index in order)
        return result.model_copy(update={"rows": rows})

    async def _texts(self, rows: Sequence[Row]) -> list[str]:
        """Each hit's text to judge: its snippet, or in chunk mode its whole text, falling back to the snippet."""
        snippets = [str(row.get("snippet") or "") for row in rows]
        if self.reranking.passage == "snippet":
            return snippets
        fetched = await asyncio.gather(*(self.fetch(Fetch(id=str(row.get("id")))) for row in rows), return_exceptions=True)
        texts: list[str] = []
        failures = 0
        for found, snippet in zip(fetched, snippets, strict=True):
            if isinstance(found, Exception):
                failures += 1
                texts.append(snippet)
            elif isinstance(found, BaseException):
                raise found
            else:
                texts.append(str(found.rows[0].get("body") or snippet) if found.rows else snippet)
        if failures:
            _logger.warning("re-rank judged {count} hits on their snippets: their chunk fetch failed", count=failures)
        return texts

    def _limit(self, query: str, count: int) -> int:
        """The most characters one passage may keep, so a request stays within `max_input_tokens`."""
        budget = self.reranking.max_input_tokens * _CHARS_PER_TOKEN - len(query) - _FRAMING_TOKENS * _CHARS_PER_TOKEN
        share = budget // count if self.reranking.requests == "all_hits" else budget
        return max(share, 1)


def reranking(
    settings: RerankSettings, gateway: GatewaySettings, *, capabilities: Sequence[AbstractCapability[None]] = ()
) -> Reranking | None:
    """The re-ranking `settings` describe, or `None` when they turn it off or the gateway has no key.

    A missing key logs one warning naming the setting.

    Args:
        settings: The `retrieval.rerank` block.
        gateway: The gateway section of the infrastructure settings, which reaches the decision model.
        capabilities: What every decision model request runs through, such as the process's spend limits.

    Returns:
        The re-ranking, or `None`.
    """
    if not settings.enabled:
        return None
    if not gateway.api_key:
        _logger.warning("search re-ranking is on, and infrastructure.gateway.api_key is empty, so search answers in its own order")
        return None
    questions = DecisionModelQuestions(
        decision_model(settings.model, gateway),
        capabilities=capabilities,
        max_concurrent_requests=settings.max_concurrent_requests,
    )
    return Reranking(
        questions=questions,
        passage=settings.passage,
        requests=settings.requests,
        deadline_seconds=settings.deadline_seconds,
        max_input_tokens=settings.max_input_tokens,
    )
