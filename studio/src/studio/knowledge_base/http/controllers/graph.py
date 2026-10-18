"""The link-graph read behind the client's Graph station."""

from core.ai.retrieval.contracts import GraphResult
from engram.retrieval.contracts import Graph, GraphHandler
from fastapi import APIRouter
from wireup import Injected
from wireup.integration.fastapi import WireupRoute

from studio.knowledge_base.http.service import KnowledgeBaseRegistry
from studio.knowledge_base.wiring.contracts import ReaderSqlGateway


class GraphController:
    """The graph of one named knowledge base, read under that knowledge base's own identity.

    The prefix is the bare `/api`, for the one path of its family. The name is required, because this process holds no knowledge base of its own.
    A knowledge base that is private still draws its own graph here.
    """

    router = APIRouter(prefix="/api", tags=["graph"], route_class=WireupRoute)

    @router.get("/graph", response_model=None, summary="Read the link graph", operation_id="readGraph")
    async def read_graph(
        self,
        gateway: Injected[ReaderSqlGateway],
        knowledge_base_registry: Injected[KnowledgeBaseRegistry],
        page_type: str | None = None,
        rel: str | None = None,
        knowledge_base: str | None = None,
    ) -> GraphResult:
        """The named knowledge base's link graph, filtered by page type and relation."""
        collection = await knowledge_base_registry.graph_collection(knowledge_base)
        handler = GraphHandler(gateway.for_collection(collection), collection)
        return await handler(Graph(page_type=page_type, rel=rel))
