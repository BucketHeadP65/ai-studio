"""The watch list, the source-queue retries, and the page decisions of a registered knowledge base."""

from engram.sources.contracts import SourceQueueEntry, WatchEntry
from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from wireup import Injected
from wireup.integration.fastapi import WireupRoute

from studio.knowledge_base.http.api_models import DecisionsResult, PageDecisionOut, PageDecisionRequest, WatchEntryAddRequest
from studio.knowledge_base.http.service import KnowledgeBaseRegistry


class WatchListController:
    """A registered knowledge base's watch list, its source-queue retries, and its page decisions.

    It shares the `/api/knowledge-bases` prefix with the registry of knowledge bases, and changes a knowledge base's own control files and pages.
    """

    router = APIRouter(prefix="/api/knowledge-bases", tags=["watch list"], route_class=WireupRoute)

    @router.post("/{name}/source-queue/{key}/retry", response_model=None, summary="Retry a queued source", operation_id="retryQueuedSource")
    async def retry_queued_source(self, name: str, key: str, knowledge_base_registry: Injected[KnowledgeBaseRegistry]) -> SourceQueueEntry:
        """Set a `failed` queued row back to `requested`."""
        return await run_in_threadpool(knowledge_base_registry.retry_queued_source_row, name, key)

    @router.post("/{name}/watch-list", response_model=None, summary="Add a watch entry", operation_id="addWatchEntry")
    async def add_watch_entry(
        self, name: str, payload: WatchEntryAddRequest, knowledge_base_registry: Injected[KnowledgeBaseRegistry]
    ) -> WatchEntry:
        """Add an entry to the watch list, or hand back the existing one for the same scanner and ref."""
        return await run_in_threadpool(knowledge_base_registry.add_watch_list_entry, name, payload)

    @router.delete("/{name}/watch-list", response_model=None, summary="Remove a watch entry", operation_id="removeWatchEntry")
    async def remove_watch_entry(
        self, name: str, scanner: str, ref: str, knowledge_base_registry: Injected[KnowledgeBaseRegistry]
    ) -> dict[str, bool]:
        """Drop an entry from the watch list, whether or not its scanner is still installed."""
        removed = await run_in_threadpool(knowledge_base_registry.remove_watch_list_entry, name, scanner, ref)
        return {"removed": removed}

    @router.get("/{name}/decisions", response_model=None, summary="List the decided pages", operation_id="listDecisions")
    async def list_decisions(self, name: str, knowledge_base_registry: Injected[KnowledgeBaseRegistry]) -> DecisionsResult:
        """Every open decision this knowledge base carries, with the outcomes its schema declares."""
        return await knowledge_base_registry.list_decisions_for(name)

    @router.patch("/{name}/pages/{page_id}", response_model=None, summary="Decide a page", operation_id="decidePage")
    async def decide_page(
        self, name: str, page_id: str, payload: PageDecisionRequest, knowledge_base_registry: Injected[KnowledgeBaseRegistry]
    ) -> PageDecisionOut:
        """Move one page to its new status, write its links, and enqueue the knowledge base's refresh."""
        return await knowledge_base_registry.decide_one_page(name, page_id, payload)
