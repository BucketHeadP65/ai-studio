"""The routes of the registry of knowledge bases: what exists, what is indexed, and what a client may create or register."""

from engram.init.contracts import InitReport
from engram.kb.contracts import KnowledgeBaseSchema
from engram.sources.contracts import SourceQueueEntry
from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from wireup import Injected
from wireup.integration.fastapi import WireupRoute

from studio.knowledge_base.http.api_models import (
    KnowledgeBaseCreateRequest,
    KnowledgeBaseDetail,
    KnowledgeBaseListResult,
    RegisterKnowledgeBaseRequest,
    RegistrationReport,
    SourceQueueAddRequest,
)
from studio.knowledge_base.http.service import KnowledgeBaseRegistry


class KnowledgeBasesController:
    """The registry of knowledge bases: list, create, read, register, unregister, and queue a source.

    There is no default knowledge base. A route that acts on one existing knowledge base names it by `{name}` in the path.
    """

    router = APIRouter(prefix="/api/knowledge-bases", tags=["knowledge bases"], route_class=WireupRoute)

    @router.get("", summary="List the knowledge bases", operation_id="listKnowledgeBases")
    async def list_knowledge_bases(self, knowledge_base_registry: Injected[KnowledgeBaseRegistry]) -> KnowledgeBaseListResult:
        """Every name the registry of knowledge bases or the database knows, merged into one row each."""
        return await knowledge_base_registry.list_knowledge_bases()

    @router.post("", response_model=None, summary="Create a knowledge base", operation_id="createKnowledgeBase")
    async def create_knowledge_base(
        self, payload: KnowledgeBaseCreateRequest, knowledge_base_registry: Injected[KnowledgeBaseRegistry]
    ) -> InitReport:
        """Scaffold a knowledge base from an inline schema or a template slug, then register it."""
        return await run_in_threadpool(knowledge_base_registry.create_knowledge_base_at, payload)

    @router.get("/{name}/schema", response_model=None, summary="Read a knowledge base's schema", operation_id="readKnowledgeBaseSchema")
    async def read_schema(self, name: str, knowledge_base_registry: Injected[KnowledgeBaseRegistry]) -> KnowledgeBaseSchema:
        """The registered knowledge base's parsed schema, read from its folder."""
        return await run_in_threadpool(knowledge_base_registry.read_knowledge_base_schema, name)

    @router.get("/{name}", response_model=None, summary="Read one knowledge base", operation_id="readKnowledgeBase")
    async def read_knowledge_base(self, name: str, knowledge_base_registry: Injected[KnowledgeBaseRegistry]) -> KnowledgeBaseDetail:
        """One knowledge base whole: its listing row, its vitals, its control files and its last refresh."""
        return await knowledge_base_registry.knowledge_base_detail(name)

    @router.post("/{name}/source-queue", response_model=None, summary="Queue a source URL", operation_id="queueSource")
    async def queue_source(
        self, name: str, payload: SourceQueueAddRequest, knowledge_base_registry: Injected[KnowledgeBaseRegistry]
    ) -> SourceQueueEntry:
        """Put one URL on the registered knowledge base's source queue."""
        return await run_in_threadpool(knowledge_base_registry.queue_source_url, name, payload)

    @router.put("/{name}/registration", response_model=None, summary="Register a knowledge base", operation_id="registerKnowledgeBase")
    async def register(
        self, name: str, payload: RegisterKnowledgeBaseRequest, knowledge_base_registry: Injected[KnowledgeBaseRegistry]
    ) -> RegistrationReport:
        """Register the knowledge base folder in the body under `name`, or register it again at the same folder."""
        return await run_in_threadpool(knowledge_base_registry.register_knowledge_base, name, payload)

    @router.delete("/{name}/registration", response_model=None, summary="Unregister a knowledge base", operation_id="unregisterKnowledgeBase")
    async def unregister(self, name: str, knowledge_base_registry: Injected[KnowledgeBaseRegistry]) -> RegistrationReport:
        """Take the registered knowledge base `name` out of the registry of knowledge bases, leaving its files alone."""
        return await run_in_threadpool(knowledge_base_registry.unregister_knowledge_base, name)
