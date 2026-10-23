r"""The normal `studio mcp` surface reading a second registered knowledge base, against a real database.

One migrated database holds three collections indexed through the official builder and index
handler: `repo-docs` (the served knowledge base), `research` (registered beside it), and
`unregistered` (present in the database, named in no registry). The server is built exactly the way
`studio mcp` builds it, then driven over the SDK's in-memory session.

What is pinned here is the handoff, not the SQL: the `collection` argument selects `research`, every
ref that comes back reads as itself, the bodies are research's, every envelope and child ref stays
research-qualified, and every stamp names research. The map descends the same way — collection,
group, the overflowed bucket's page-type sub-group, a page — losing its collection at no rung. A
collection the database holds but the registry never named is a tool error, never a silent read.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from core.ai.embedding import BatchEmbedder
from core.wiring.contracts import ContainerResolver
from engram.indexing.contracts import FilesystemKnowledgeBaseReader, IndexKnowledgeBase, IndexKnowledgeBaseHandler
from engram.indexing.infrastructure import orm
from engram.kb.contracts import KnowledgeBaseSchema
from engram.retrieval.domain import SearchResult
from engram.retrieval.tools import FetchEnvelope
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from mcp.client import Client
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import create_async_engine

from studio.knowledge_base.cli import _build_mcp_server
from studio.knowledge_base.container import create_container, dispose_engines
from studio.knowledge_base.indexing.infrastructure.validation_report_sink import FileValidationReportSink
from studio.knowledge_base.settings import ApplicationSettings, DatabaseSettings, KnowledgeBaseSettings
from studio.knowledge_base.wiring.indexing import KnowledgeBaseUnitOfWorkFactory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mcp.types import CallToolResult

# One page over the disclosure row cap, so the ungrouped app bucket degrades to its page-type
# sub-groups instead of truncating — the rung `group:research//reference` only exists past it.
_OVERFLOWING = 51


def _page(collection: str, slug: str) -> str:
    return (
        f"---\nid: paper.{slug}\ntype: reference\ntitle: {slug}\nsummary: Graph evidence assessment\nstatus: active\n---\n\n"
        f"## Findings\n\n{collection} graph evidence assessment body for {slug}.\n"
    )


def _knowledge_base(root: Path, *, pages: int) -> KnowledgeBaseSchema:
    """Lay down one knowledge base whose slug and collection are its folder name, and return its schema."""
    spec = KnowledgeBaseSchema.model_validate(
        {
            "slug": root.name,
            "page_types": [{"page_type": "reference", "id_prefix": "paper", "folder": "Units/References"}],
            "retrieval": {"collection": root.name, "group_by": "app", "mode": "fixed", "profile": "search-keyword"},
        }
    )
    builder = KnowledgeBaseBuilder(root)
    builder.add_knowledge_base_schema(spec)
    for index in range(pages):
        slug = "graph" if index == 0 else f"filler-{index}"
        builder.add_unit_document("Units/References", slug, _page(root.name, slug))
    return spec


async def _index(root: Path, spec: KnowledgeBaseSchema, url: str) -> None:
    """Index one knowledge base through the official handler, over its own engine."""
    engine = create_async_engine(url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        handler = IndexKnowledgeBaseHandler(
            FilesystemKnowledgeBaseReader(root, note_folders=spec.note_folders),
            FakeEmbedding(),
            KnowledgeBaseUnitOfWorkFactory(engine),
            FileValidationReportSink(root),
            spec=spec,
        )
        await handler.handle(IndexKnowledgeBase(collection=root.name, collection_named=True, embed=False))
    finally:
        await engine.dispose()


async def _read(client: Client, ref: str) -> FetchEnvelope:
    """Fetch `ref` and assert it came back as itself, from research, with research-qualified children."""
    result = await client.call_tool("fetch", {"unit_id": ref})
    assert not result.is_error, result.content
    opened = FetchEnvelope.model_validate(json.loads(_text(result)))
    assert opened.ref == ref
    assert opened.knowledge_base is not None
    assert opened.knowledge_base.slug == "research"
    # The truncation sentinel is a notice, not an address, and is the one child carrying no collection.
    for child in (child for child in opened.children if child.kind != "truncated"):
        assert child.ref.split(":", 1)[1].startswith("research/"), child.ref
    return opened


def _text(result: CallToolResult) -> str:
    """The tool result's single text block, which carries the answer's own JSON unnested."""
    block = result.content[0]
    assert block.type == "text"
    return block.text


@pytest.fixture
async def served(migrated: str, reader_url: str, tmp_path: Path) -> AsyncIterator[tuple[ContainerResolver, Path]]:
    """Three indexed collections, a registry naming two of them, and the resolver `studio mcp` builds from."""
    roots = [tmp_path / name for name in ("repo-docs", "research", "unregistered")]
    for root in roots:
        await _index(root, _knowledge_base(root, pages=_OVERFLOWING if root.name == "research" else 1), migrated)
    registry = tmp_path / "settings.json"
    registry.write_text(
        json.dumps({"knowledge_bases": {root.name: {"path": str(root)} for root in roots[:2]}}), encoding="utf-8"
    )
    base = ApplicationSettings()
    settings = ApplicationSettings(
        infrastructure=base.infrastructure.model_copy(update={"database": DatabaseSettings(url=migrated, reader_url=reader_url)})
    )
    container = create_container(settings, knowledge_base=KnowledgeBaseSettings(path=roots[0]))
    container.override.set(BatchEmbedder, FakeEmbedding())
    resolver = ContainerResolver(container)
    try:
        yield resolver, registry
    finally:
        await dispose_engines(resolver)
        await container.close()


class TestTheNormalServerHandsOffToASecondKnowledgeBase:
    async def test_a_research_hit_reads_research_all_the_way_down(self, served: tuple[ContainerResolver, Path]):
        resolver, registry = served
        server = await _build_mcp_server(resolver, settings_path=registry)

        async with Client(server) as client:
            result = await client.call_tool("search", {"query": "graph evidence assessment", "collection": "research"})
            assert not result.is_error, result.content
            searched = SearchResult.model_validate(json.loads(_text(result)))
            assert searched.knowledge_base is not None
            assert searched.knowledge_base.slug == "research"
            page_ref = next(hit.ref for hit in searched.hits if hit.hit_level == "card")
            assert page_ref is not None
            assert page_ref.startswith("page:research/")

            page = await _read(client, page_ref)
            assert "research graph evidence assessment body" in str(page.content)
            section_ref = page.children[0].ref
            assert section_ref.startswith("section:research/")
            assert section_ref in str(page.next_steps)

            section = await _read(client, section_ref)
            assert "research graph evidence assessment body" in str(section.content)
            assert page_ref in str(section.next_steps)

    async def test_the_map_descends_to_a_page_without_losing_research(self, served: tuple[ContainerResolver, Path]):
        resolver, registry = served
        server = await _build_mcp_server(resolver, settings_path=registry)

        async with Client(server) as client:
            root = await _read(client, "collection:research")
            group_ref = next(child.ref for child in root.children if child.ref == "group:research/")

            group = await _read(client, group_ref)
            nested_ref = group.children[0].ref
            assert nested_ref == "group:research//reference"

            nested = await _read(client, nested_ref)
            page_ref = nested.children[0].ref
            assert page_ref.startswith("page:research/")

            await _read(client, page_ref)

    async def test_a_collection_the_registry_never_named_is_refused(self, served: tuple[ContainerResolver, Path]):
        resolver, registry = served
        server = await _build_mcp_server(resolver, settings_path=registry)

        async with Client(server) as client:
            searched = await client.call_tool("search", {"query": "graph", "collection": "unregistered"})
            fetched = await client.call_tool("fetch", {"unit_id": "page:unregistered/paper.graph"})

        assert searched.is_error
        assert fetched.is_error
