"""Studio's search re-ranking: the settings block, the container provider, and every site that wraps a search in it."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from core.ai.decision_models import DecisionModelQuestions
from core.ai.embedding import BatchEmbedder
from core.settings import OpenRouterSettings
from core.testing.fakes import FakeDecisionModel
from engram.config import RerankSettings
from engram.retrieval.contracts import ReadSurface, RerankedSearch, Reranking, Search, SearchHandler
from engram.retrieval.domain import SqlResult
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.specs import research_spec
from mcp.shared.memory import create_connected_server_and_client_session as connect_in_memory
from pydantic_ai.models import override_allow_model_requests

from studio.knowledge_base.cli import _build_mcp_server, _read_surface, _run_search
from studio.knowledge_base.settings import DatabaseSettings, InfrastructureSettings, RetrievalSettings
from studio.knowledge_base.wiring.contracts import ReaderSqlGateway, SearchReranking
from studio.knowledge_base.wiring.serve import search_reranking_provider
from tests.unit.knowledge_base.test_cli_serve import _FakeReaderSqlGateway, _inert_engine, _make_knowledge_base, _McpServerContainer

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pydantic_ai.models.decision import DecisionRequest, NoulQuestion

_DATABASE = DatabaseSettings(
    url="postgresql+asyncpg://u:p@h:5433/knowledge_bases", reader_url="postgresql+asyncpg://r:p@h:5433/knowledge_bases"
)


def test_retrieval_settings_carry_the_rerank_block_with_its_defaults() -> None:
    assert RetrievalSettings().rerank == RerankSettings()


def test_the_rerank_block_is_read_from_the_section() -> None:
    settings = RetrievalSettings.model_validate({"rerank": {"requests": "all_hits"}})

    assert settings.rerank.requests == "all_hits"


_HITS = tuple(
    {"id": name, "hit_level": "card", "page_id": name, "snippet": f"{letter} snippet", "score": score}
    for name, letter, score in (("first", "alpha", 0.031), ("second", "beta", 0.029), ("third", "gamma", 0.027))
)


@pytest.fixture(autouse=True)
def _allow_the_scripted_model() -> Iterator[None]:
    with override_allow_model_requests(allow_model_requests=True):
        yield


def _by_snippet(request: DecisionRequest, _name: str, _question: NoulQuestion) -> float:
    state = str(request.state)
    return 0.9 if "gamma" in state else 0.6 if "beta" in state else 0.1


def _reranking(model: FakeDecisionModel) -> Reranking:
    return Reranking(
        questions=DecisionModelQuestions(model), passage="snippet", requests="per_hit", deadline_seconds=5, max_input_tokens=32_000
    )


class _RerankContainer:
    """A container answering the reader gateway, the embedder and the re-ranking a search needs."""

    def __init__(self, gateway: FakeSqlGateway, search_reranking: SearchReranking) -> None:
        self._services: dict[type, object] = {
            ReaderSqlGateway: _FakeReaderSqlGateway(_inert_engine(), gateway),
            BatchEmbedder: FakeEmbedding(),
            SearchReranking: search_reranking,
        }

    async def get[T](self, klass: type[T]) -> T:
        service = self._services.get(klass)
        if not isinstance(service, klass):
            msg = f"_RerankContainer has no registration for {klass!r}"
            raise KeyError(msg)
        return service


def _surface(tmp_path: Path, rerank: Reranking | None) -> ReadSurface:
    spec = research_spec()
    engine = _inert_engine()
    surface = _read_surface(tmp_path, spec, ReadOnlySqlGateway(engine), FakeSqlGateway(), FakeEmbedding(), hybrid=False, rerank=rerank)
    return surface


def test_a_read_surface_with_a_reranking_wraps_its_search(tmp_path: Path) -> None:
    surface = _surface(tmp_path, _reranking(FakeDecisionModel()))

    assert isinstance(surface.handlers.search, RerankedSearch)


def test_a_read_surface_without_a_reranking_keeps_the_plain_search(tmp_path: Path) -> None:
    surface = _surface(tmp_path, None)

    assert isinstance(surface.handlers.search, SearchHandler)


async def test_the_cli_search_returns_the_hits_in_the_order_the_decision_model_gives() -> None:
    model = FakeDecisionModel(yes=_by_snippet)
    gateway = FakeSqlGateway(routed={"api.search": SqlResult(rows=_HITS, columns=("id", "snippet", "score"))})
    container = _RerankContainer(gateway, SearchReranking(reranking=_reranking(model)))

    result = await _run_search(container, Search(query="greek letters", collection="research", hybrid=False), hybrid=False)

    assert [row["id"] for row in result.rows] == ["third", "second", "first"]
    assert [(row["rerank_score"], row["score"]) for row in result.rows] == [(0.9, 0.027), (0.6, 0.029), (0.1, 0.031)]
    assert len(model.requests) == len(_HITS)


async def test_the_cli_search_keeps_the_search_order_when_the_container_holds_no_reranking() -> None:
    gateway = FakeSqlGateway(routed={"api.search": SqlResult(rows=_HITS, columns=("id", "snippet", "score"))})
    container = _RerankContainer(gateway, SearchReranking(reranking=None))

    result = await _run_search(container, Search(query="greek letters", collection="research", hybrid=False), hybrid=False)

    assert [row["id"] for row in result.rows] == ["first", "second", "third"]


async def test_the_mcp_server_serves_a_reranked_search(tmp_path: Path) -> None:
    model = FakeDecisionModel(yes=_by_snippet)
    gateway = FakeSqlGateway(routed={"api.search": SqlResult(rows=_HITS, columns=("id", "snippet", "score"))})
    container = _McpServerContainer(
        knowledge_base=_make_knowledge_base(tmp_path),
        gateway=gateway,
        search_reranking=SearchReranking(reranking=_reranking(model)),
    )
    server = await _build_mcp_server(container, settings_path=tmp_path / "no-registry.json")

    async with connect_in_memory(server) as client:
        await client.initialize()
        result = await client.call_tool("search", {"query": "greek letters"})

    assert result.isError is False
    assert len(model.requests) == len(_HITS)
    assert json.dumps(result.structuredContent).index("third") < json.dumps(result.structuredContent).index("first")


async def test_the_code_mode_sandbox_searches_through_the_reranking(tmp_path: Path) -> None:
    model = FakeDecisionModel(yes=_by_snippet)
    gateway = FakeSqlGateway(routed={"api.search": SqlResult(rows=_HITS, columns=("id", "snippet", "score"))})
    container = _McpServerContainer(
        knowledge_base=_make_knowledge_base(tmp_path),
        gateway=gateway,
        retrieval=RetrievalSettings(retrieval_profile="code-mode"),
        search_reranking=SearchReranking(reranking=_reranking(model)),
    )
    server = await _build_mcp_server(container, settings_path=tmp_path / "no-registry.json")

    async with connect_in_memory(server) as client:
        await client.initialize()
        result = await client.call_tool("run_code", {"code": "result = await search(query='greek letters')\nresult\n"})

    assert result.isError is False
    assert len(model.requests) == len(_HITS)
    text = json.dumps(result.structuredContent)
    assert text.index("third") < text.index("first")


def test_the_provider_answers_no_reranking_when_it_is_disabled() -> None:
    retrieval = RetrievalSettings.model_validate({"rerank": {"enabled": False}})
    infrastructure = InfrastructureSettings(database=_DATABASE, openrouter=OpenRouterSettings(api_key="a-key"))

    assert search_reranking_provider(retrieval, infrastructure) == SearchReranking(reranking=None)


def test_the_provider_answers_no_reranking_when_the_openrouter_key_is_empty() -> None:
    retrieval = RetrievalSettings.model_validate({"rerank": {"enabled": True}})
    infrastructure = InfrastructureSettings(database=_DATABASE, openrouter=OpenRouterSettings(api_key=""))

    assert search_reranking_provider(retrieval, infrastructure) == SearchReranking(reranking=None)


def test_the_provider_answers_a_reranking_when_it_is_enabled_and_the_key_is_set() -> None:
    retrieval = RetrievalSettings.model_validate({"rerank": {"enabled": True}})
    infrastructure = InfrastructureSettings(database=_DATABASE, openrouter=OpenRouterSettings(api_key="a-key"))

    assert isinstance(search_reranking_provider(retrieval, infrastructure).reranking, Reranking)
