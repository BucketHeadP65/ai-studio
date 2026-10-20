r"""End-to-end knowledge base HTTP session against a real, migrated database — Docker-gated.

The Graph station's whole read path through the production
:func:`~studio.knowledge_base.http.api.create_app` app, driven over an httpx `ASGITransport` (no
listening socket) against a **real** wireup container built from settings pointing at a
freshly-migrated database (the `migrated` / `reader_url` conftest fixtures, exactly as
`tests/e2e/test_mcp_session.py`). A :class:`~engram.testing.fakes.embedding.FakeEmbedding` is
the only fake; the index runs `embed=False` through the container's own bus so the embedder
is never touched.

The asserted contract: index the knowledge base, then `GET /api/graph` serves the indexed card over
the reader role.

The module skips cleanly when Docker is unavailable (the `pg` fixture, via `migrated`,
raises `pytest.skip`), so it import-collects with Docker DOWN. Tests are `async def` (the
httpx client + the container both live on the pytest-asyncio loop).
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from core.ai.embedding import BatchEmbedder
from core.cqrs.contracts import MessageBus
from engram.indexing.contracts import IndexKnowledgeBase
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from engram.testing.specs import research_spec
from httpx import ASGITransport, AsyncClient

from studio.knowledge_base.container import create_container
from studio.knowledge_base.http.api import create_app
from studio.knowledge_base.settings import ApplicationSettings, DatabaseSettings, KnowledgeBaseSettings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import wireup

# A loopback host: `TrustedHostMiddleware` answers only `localhost`/`127.0.0.1`, so a request to
# any other host is refused with 400 before the route runs.
_BASE_URL = "http://localhost"

# One small, well-formed paper + its mirror — enough for a clean index with a card and a
# claim, so api.cards / api.search have rows to serve after the index job.
_PAPER = """\
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


def _mirror(source_key: str, source_hash: str, body: str) -> str:
    """A minimal raw mirror — its only job is to satisfy the pages.zotero_key FK."""
    return f"---\nsource_key: {source_key}\nprovider: example-library\nsource_hash: {source_hash}\n---\n# Mirror\n\n## 1 Body\n\n{body}\n"


def _build_knowledge_base(root: Path) -> Path:
    """Lay down the one paper, its mirror, the registries, and the meta stubs."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_unit_document("Wiki/Papers", "graph-cost", _PAPER)
    builder.add_source_mirror("graph-cost", _mirror("GRAPHKEY1", "g001", "Graph index construction token cost."))
    builder.add_vocabulary()
    builder.add_meta_stubs()
    spec = research_spec()
    (root / "knowledge-base-schema.json").write_text(spec.model_dump_json(exclude_none=True), encoding="utf-8")
    return root


def _settings(admin_url: str, reader_url: str) -> ApplicationSettings:
    """Production settings over the migrated DB; embedding knobs are dummies (index runs embed=False)."""
    base = ApplicationSettings()
    return ApplicationSettings(
        infrastructure=base.infrastructure.model_copy(update={"database": DatabaseSettings(url=admin_url, reader_url=reader_url)})
    )


def _container(settings: ApplicationSettings, knowledge_base: Path) -> wireup.AsyncContainer:
    """A real container with the :class:`BatchEmbedder` overridden to the deterministic fake (no Azure)."""
    container = create_container(settings, knowledge_base=KnowledgeBaseSettings(path=knowledge_base))
    container.override.set(BatchEmbedder, FakeEmbedding())
    return container


async def _client(settings: ApplicationSettings, container: wireup.AsyncContainer, settings_path: Path) -> AsyncIterator[AsyncClient]:
    """An httpx client over the production app + real container, with the lifespan run."""
    app = create_app(settings, container=container, settings_path=settings_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=_BASE_URL) as client, app.router.lifespan_context(app):
        yield client


@pytest.fixture
def knowledge_base(tmp_path: Path) -> Path:
    """The knowledge base tree the session acts on."""
    return _build_knowledge_base(tmp_path)


@pytest.fixture
def settings(migrated: str, reader_url: str) -> ApplicationSettings:
    """Settings over the migrated DB (admin URL) + the reader role URL."""
    return _settings(migrated, reader_url)


class TestGraphSession:
    async def test_index_then_graph_serves_the_card(self, settings: ApplicationSettings, knowledge_base: Path, tmp_path: Path):
        container = _container(settings, knowledge_base)
        bus = await container.get(MessageBus)
        await bus.handle(IndexKnowledgeBase(collection="research", full=True, embed=False))

        registry = {"knowledge_bases": {"graphs": {"path": str(tmp_path), "description": ""}}}
        settings_path = tmp_path / "application_settings.json"
        settings_path.write_text(json.dumps(registry), encoding="utf-8")
        gen = _client(settings, container, settings_path)
        client = await anext(gen)
        try:
            found = await client.get("/api/graph", params={"knowledge_base": "graphs"})
            assert found.status_code == 200
            nodes = found.json()["nodes"]
            assert any(node["id"] == "paper.graph-cost" for node in nodes), nodes
        finally:
            with pytest.raises(StopAsyncIteration):
                await anext(gen)
