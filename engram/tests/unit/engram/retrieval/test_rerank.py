"""`RerankedSearch`: a search's hits in the order a decision model judges them relevant, none dropped."""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

import pytest

from core.ai.decision_models import DecisionModelQuestions
from core.ai.retrieval.contracts import SqlResult
from core.settings import GatewaySettings
from core.testing.fakes import FakeDecisionModel
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.models import override_allow_model_requests

from engram.config import RerankSettings
from engram.retrieval.contracts import RerankedSearch, Reranking, reranking
from engram.retrieval.domain import Search

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from core.ai.decision_models import RequestShape
    from core.ai.retrieval.contracts import Fetch, Row
    from pydantic_ai.models.decision import DecisionRequest, NoulQuestion

    from engram.config import PassageText


@pytest.fixture(autouse=True)
def _allow_the_scripted_model() -> Iterator[None]:
    with override_allow_model_requests(allow_model_requests=True):
        yield


_ROWS: tuple[Row, ...] = (
    {"id": "spend", "snippet": "SpendLimits charges each model request.", "score": 0.031},
    {"id": "restart", "snippet": "A harness boot pauses any running workflow.", "score": 0.029},
    {"id": "durable", "snippet": "A durable engine resumes a workflow after a restart.", "score": 0.027},
)


class _FakeSearch:
    def __init__(self, rows: tuple[Row, ...], *, delay_seconds: float = 0.0) -> None:
        self.rows = rows
        self.delay_seconds = delay_seconds

    async def __call__(self, query: Search) -> SqlResult:
        del query
        await asyncio.sleep(self.delay_seconds)
        return SqlResult(rows=self.rows, columns=("id", "snippet", "score"))


class _FakeFetch:
    def __init__(self, bodies: Mapping[str, str], *, delay_seconds: float = 0.0) -> None:
        self.bodies = bodies
        self.delay_seconds = delay_seconds
        self.fetched: list[str] = []

    async def __call__(self, query: Fetch) -> SqlResult:
        self.fetched.append(query.id)
        await asyncio.sleep(self.delay_seconds)
        return SqlResult(rows=({"id": query.id, "level": "section", "title": "t", "body": self.bodies[query.id]},))


def _by_keyword(request: DecisionRequest, _name: str, _question: NoulQuestion) -> float:
    state = str(request.state)
    return 0.9 if "boot" in state else 0.6 if "restart" in state else 0.1


def _reranked(
    model: FakeDecisionModel,
    rows: tuple[Row, ...] = _ROWS,
    *,
    passage: PassageText = "snippet",
    requests: RequestShape = "per_hit",
    deadline_seconds: float = 5.0,
    max_input_tokens: int = 32_000,
    search_seconds: float = 0.0,
) -> tuple[RerankedSearch[Search], _FakeFetch]:
    fetch = _FakeFetch({"spend": "spend body", "restart": "restart body after a boot", "durable": "durable body"})
    rerank = Reranking(
        questions=DecisionModelQuestions(model),
        passage=passage,
        requests=requests,
        deadline_seconds=deadline_seconds,
        max_input_tokens=max_input_tokens,
    )
    return rerank.over(_FakeSearch(rows, delay_seconds=search_seconds), fetch), fetch


async def test_hits_come_back_in_the_order_of_their_probability_beside_their_fused_score() -> None:
    search, _ = _reranked(FakeDecisionModel(yes=_by_keyword))

    result = await search(Search(query="q", collection=None))

    assert [row["id"] for row in result.rows] == ["restart", "durable", "spend"]
    assert [(row["rerank_score"], row["score"]) for row in result.rows] == [(0.9, 0.029), (0.6, 0.027), (0.1, 0.031)]


async def test_no_hit_is_dropped_and_ties_keep_the_search_order() -> None:
    search, _ = _reranked(FakeDecisionModel(yes=lambda _r, _n, _q: 0.5))

    result = await search(Search(query="q", collection=None))

    assert [row["id"] for row in result.rows] == ["spend", "restart", "durable"]


async def test_a_failure_returns_the_search_result_unchanged() -> None:
    search, _ = _reranked(FakeDecisionModel(error=ModelAPIError(model_name="fake-decision", message="down")))

    result = await search(Search(query="q", collection=None))

    assert result.rows == _ROWS


async def test_no_hits_asks_nothing() -> None:
    model = FakeDecisionModel()
    empty, _ = _reranked(model, rows=())

    assert (await empty(Search(query="q", collection=None))).rows == ()
    assert model.requests == []


async def test_one_hit_is_judged_and_scored_with_its_probability() -> None:
    model = FakeDecisionModel(yes=lambda _r, _n, _q: 0.8)
    one, _ = _reranked(model, rows=_ROWS[:1])

    result = await one(Search(query="q", collection=None))

    assert [(row["id"], row["rerank_score"], row["score"]) for row in result.rows] == [("spend", 0.8, 0.031)]
    assert len(model.requests) == 1


async def test_snippet_mode_judges_the_snippet_and_fetches_nothing() -> None:
    model = FakeDecisionModel()
    search, fetch = _reranked(model)

    await search(Search(query="q", collection=None))

    assert fetch.fetched == []
    assert any("SpendLimits charges" in str(request.state) for request in model.requests)


async def test_chunk_mode_judges_each_hits_whole_text_cut_to_fit() -> None:
    model = FakeDecisionModel()
    search, fetch = _reranked(model, passage="chunk", max_input_tokens=1_000)
    fetch.bodies = {"spend": "x" * 50_000, "restart": "restart body", "durable": "durable body"}

    await search(Search(query="q", collection=None))

    assert sorted(fetch.fetched) == ["durable", "restart", "spend"]
    assert all(len(str(request.state)) <= 1_000 * 4 for request in model.requests)


async def test_a_failed_chunk_fetch_judges_that_hit_on_its_snippet() -> None:
    model = FakeDecisionModel(yes=_by_keyword)
    search, fetch = _reranked(model, passage="chunk")
    fetch.bodies = {"restart": "restart body after a boot", "durable": "durable body"}

    result = await search(Search(query="q", collection=None))

    assert sorted(str(row["id"]) for row in result.rows) == ["durable", "restart", "spend"]
    assert any("SpendLimits charges" in str(request.state) for request in model.requests)


async def test_chunk_fetching_past_the_deadline_returns_the_search_order() -> None:
    search, fetch = _reranked(FakeDecisionModel(yes=_by_keyword), passage="chunk", deadline_seconds=0.05)
    fetch.delay_seconds = 1

    result = await search(Search(query="q", collection=None))

    assert result.rows == _ROWS


async def test_a_search_slower_than_the_deadline_still_leaves_the_re_rank_its_whole_deadline() -> None:
    search, _ = _reranked(FakeDecisionModel(yes=_by_keyword), deadline_seconds=0.2, search_seconds=0.3)

    result = await search(Search(query="q", collection=None))

    assert [row["id"] for row in result.rows] == ["restart", "durable", "spend"]


async def test_an_empty_snippet_is_still_ranked() -> None:
    rows: tuple[Row, ...] = ({"id": "a", "snippet": None, "score": 0.2}, {"id": "b", "snippet": "boot", "score": 0.1})
    search, _ = _reranked(FakeDecisionModel(yes=_by_keyword), rows=rows)

    result = await search(Search(query="q", collection=None))

    assert [row["id"] for row in result.rows] == ["b", "a"]


async def test_two_hits_with_one_id_each_keep_their_own_probability() -> None:
    rows: tuple[Row, ...] = ({"id": "same", "snippet": "spend", "score": 0.2}, {"id": "same", "snippet": "boot", "score": 0.1})
    search, _ = _reranked(FakeDecisionModel(yes=_by_keyword), rows=rows)

    result = await search(Search(query="q", collection=None))

    assert [row["snippet"] for row in result.rows] == ["boot", "spend"]


def test_reranking_is_off_when_disabled_or_without_a_gateway_key() -> None:
    assert reranking(RerankSettings(enabled=False), GatewaySettings(api_key="k")) is None
    assert reranking(RerankSettings(), GatewaySettings()) is None


def test_without_a_gateway_key_reranking_warns_naming_the_key(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        reranking(RerankSettings(), GatewaySettings())

    assert "infrastructure.gateway.api_key" in caplog.text


def test_reranking_asks_jev_latest_by_default() -> None:
    assert RerankSettings().model == "jev-latest"


def test_reranking_carries_the_settings() -> None:
    built = reranking(RerankSettings(passage="chunk", requests="all_hits", deadline_seconds=2.0), GatewaySettings(api_key="k"))

    assert built is not None
    assert (built.passage, built.requests, built.deadline_seconds) == ("chunk", "all_hits", 2.0)
