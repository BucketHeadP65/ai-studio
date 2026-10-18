"""The grant surface this app offers a declared step, built without a database.

`build_environment` derives what a step may be granted from the knowledge base surface it is handed: every tool
that surface offers minus the ones no step sees, plus the shell, web search, and the grounding
plugins. This composes the same `RetrievalKnowledgeBase` the app composes -- engram's two read tools plus
its four authoring tools -- over engram's shipped fakes, and reads `WEB_SEARCH_TOOL` and
`GROUNDING_TOOLS` off the same constants `build_environment` itself grants from, so a test asserts
against the surface the app really builds rather than against a typed-out list that can silently
disagree with it.
"""

from __future__ import annotations

from pathlib import Path

from engram.authoring.contracts import AuthoringKnowledgeBase, AuthoringTools
from engram.retrieval.capability import ReadSurface, RetrievalKnowledgeBase
from engram.retrieval.envelope import EnvelopeHandler
from engram.retrieval.queries import FetchHandler, GraphExpandHandler, GraphPathsHandler, SearchHandler
from engram.retrieval.tools import RetrievalHandlers
from engram.sources.contracts import SourceQueue, claimants_for
from engram.testing.fakes.authoring import FakeAdminGateway, FakeReindex
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.specs import research_spec

from harness.adapters.knowledge_base import GROUNDING_TOOLS
from harness.adapters.websearch import WEB_SEARCH_TOOL
from harness.domain.ports import NOT_FOR_STEPS, SHELL_TOOL

_KNOWLEDGE_BASE = "research"


def app_knowledge_base_surface() -> RetrievalKnowledgeBase[object]:
    """The reader the app hands `build_environment`: one knowledge base, with the authoring plugins open.

    Every handler behind it reads a fake gateway and a fake embedder, so nothing here connects to
    anything. The tools it mounts, and their names, are the production ones.
    """
    gateway = FakeSqlGateway()
    spec = research_spec()
    knowledge_base = AuthoringKnowledgeBase(name=_KNOWLEDGE_BASE, collection=_KNOWLEDGE_BASE, path=Path(), schema=spec)
    return RetrievalKnowledgeBase[object](
        surfaces=(
            ReadSurface(
                name=spec.slug,
                collection=_KNOWLEDGE_BASE,
                handlers=RetrievalHandlers(
                    search=SearchHandler(gateway, FakeEmbedding()),
                    fetch=FetchHandler(gateway, _KNOWLEDGE_BASE),
                    graph_expand=GraphExpandHandler(gateway, _KNOWLEDGE_BASE),
                    find_paths=GraphPathsHandler(gateway, _KNOWLEDGE_BASE),
                ),
                envelope=EnvelopeHandler(gateway, collection=_KNOWLEDGE_BASE),
                domain=spec.purpose,
                facet_keys=tuple(facet.key for facet in spec.facets),
            ),
        ),
        authoring=AuthoringTools(
            knowledge_bases={_KNOWLEDGE_BASE: knowledge_base},
            reindex=FakeReindex(),
            admin=FakeAdminGateway(),
            queue_for=lambda one: SourceQueue(one.path / "_meta" / "source-queue.yaml", connectors=claimants_for(one.schema.sources.connectors)),
        ),
    )


async def app_reads() -> tuple[str, ...]:
    """The knowledge base tools a program may be dispatched, derived the way `build_environment` derives them."""
    surface = app_knowledge_base_surface()
    return tuple(tool.name for tool in await surface.list_tools() if tool.name not in NOT_FOR_STEPS)


async def app_grants() -> tuple[str, ...]:
    """What a declared step may ask for by name here: the reads, the shell, web search, and grounding."""
    return (*await app_reads(), SHELL_TOOL, WEB_SEARCH_TOOL, *GROUNDING_TOOLS)
