"""What the serve slice registers: the read-only engine, the two database gateways, and the handlers and services that read through them.

The read-only engine connects as the `kb_reader` role, whose server settings make every transaction read only and cap each statement at
10 seconds. It keeps no idle connections and registers the pgvector codec before it is handed out, so the `halfvec` query vector round-trips.
The admin gateway reads the `kb.*` tables the read-only role is not granted. The reader gateway reads as no knowledge base: each provider
binds it to its knowledge base's collection, and a route that names its knowledge base binds it per request. An anonymous read sees only
knowledge bases that are not private.
"""

from __future__ import annotations

from core.ai.embedding import BatchEmbedder
from core.cqrs.contracts import JobQueries, Jobs
from core.db import build_reader_engine
from engram.indexing.contracts import register_pgvector
from engram.kb.contracts import KnowledgeBaseSchema
from engram.retrieval.contracts import (
    DescribeSchemaHandler,
    EnvelopeHandler,
    FetchHandler,
    GraphBackends,
    GraphExpandHandler,
    GraphPathsHandler,
    KnowledgeBasesHandler,
    KnowledgeBaseValidationHandler,
    LookupValueHandler,
    PrivateReaders,
    SearchDialects,
    SearchHandler,
    StatusHandler,
    build_graph_backends,
    build_search_dialects,
    reranking,
    verify_reader_role,
)
from sqlalchemy.ext.asyncio import AsyncEngine
from wireup import injectable

from studio.knowledge_base.http.contracts import KnowledgeBaseRegistry
from studio.knowledge_base.http.infrastructure.registered_knowledge_bases import SettingsFileRegisteredKnowledgeBases
from studio.knowledge_base.reindex import SubprocessReindex
from studio.knowledge_base.retention.contracts import NoteRetention
from studio.knowledge_base.retention.infrastructure.artifact_resolver import HarnessArtifactResolver
from studio.knowledge_base.retention.infrastructure.knowledge_bases import knowledge_base_reader
from studio.knowledge_base.retention.infrastructure.receipts import RetentionReceipts
from studio.knowledge_base.settings import (
    DatabaseSettings,
    HarnessServiceSettings,
    InfrastructureSettings,
    KnowledgeBaseSettings,
    RetrievalSettings,
    SettingsFile,
)
from studio.knowledge_base.wiring.contracts import AdminSqlGateway, ReaderEngine, ReaderSqlGateway, SearchReranking


@injectable
async def reader_engine_provider(database: DatabaseSettings) -> ReaderEngine:
    """Provide the read-only engine, with the pgvector codec registered before it is handed out.

    Args:
        database: The database settings whose `reader_url` the engine connects to.

    Returns:
        The read-only engine.
    """
    engine = build_reader_engine(database.reader_url)
    await verify_reader_role(engine)
    await register_pgvector(engine)
    return ReaderEngine(engine)


@injectable
def reader_gateway_provider(reader_engine: ReaderEngine, database: DatabaseSettings) -> ReaderSqlGateway:
    """Provide the read-only gateway over the read-only engine, reading a private knowledge base through its own role.

    Args:
        reader_engine: The read-only engine.
        database: The database settings whose `reader_url` and `reader_secret` reach a private knowledge base's role.

    Returns:
        The read-only gateway, reading as no knowledge base.
    """
    return ReaderSqlGateway(reader_engine.engine, private_readers=PrivateReaders(database.reader_url, database.reader_secret))


@injectable
def admin_gateway_provider(engine: AsyncEngine) -> AdminSqlGateway:
    """Provide the gateway over the admin engine, for the reads of the `kb.*` tables.

    Args:
        engine: The admin engine.

    Returns:
        The admin gateway.
    """
    return AdminSqlGateway(engine)


@injectable
def search_handler_provider(gateway: ReaderSqlGateway, embedder: BatchEmbedder, schema: KnowledgeBaseSchema) -> SearchHandler:
    """Provide the search handler, reading as this knowledge base.

    Args:
        gateway: The read-only gateway.
        embedder: The embedding model each query is embedded with.
        schema: The knowledge base's schema, whose collection the handler reads.

    Returns:
        The search handler.
    """
    return SearchHandler(gateway.for_collection(schema.retrieval.collection), embedder)


@injectable
def fetch_handler_provider(gateway: ReaderSqlGateway, schema: KnowledgeBaseSchema) -> FetchHandler:
    """Provide the fetch handler, scoped to this knowledge base's collection.

    Args:
        gateway: The read-only gateway.
        schema: The knowledge base's schema, whose collection the handler reads.

    Returns:
        The fetch handler.
    """
    return FetchHandler(gateway.for_collection(schema.retrieval.collection), schema.retrieval.collection)


@injectable
def lookup_value_handler_provider(gateway: ReaderSqlGateway, schema: KnowledgeBaseSchema) -> LookupValueHandler:
    """Provide the lookup-value handler, scoped to this knowledge base's collection.

    Args:
        gateway: The read-only gateway.
        schema: The knowledge base's schema, whose collection the handler reads.

    Returns:
        The lookup-value handler.
    """
    return LookupValueHandler(gateway.for_collection(schema.retrieval.collection), schema.retrieval.collection)


@injectable
def describe_schema_handler_provider(gateway: ReaderSqlGateway, schema: KnowledgeBaseSchema, knowledge_base: KnowledgeBaseSettings) -> DescribeSchemaHandler:
    """Provide the describe-schema handler, grounded in this knowledge base's page types, example ids, slug and root.

    Args:
        gateway: The read-only gateway.
        schema: The knowledge base's schema the description is grounded in.
        knowledge_base: The knowledge base whose root the description names.

    Returns:
        The describe-schema handler.
    """
    return DescribeSchemaHandler(gateway, (schema,), root=knowledge_base.path)


@injectable
def envelope_handler_provider(gateway: ReaderSqlGateway, schema: KnowledgeBaseSchema) -> EnvelopeHandler:
    """Provide the envelope handler, which enforces the disclosure the knowledge base's schema declares.

    It is scoped to this knowledge base's collection, so a `collection:` or `group:` reference naming another knowledge base
    is refused instead of served under the wrong schema, even when several knowledge bases share this database.

    Args:
        gateway: The read-only gateway.
        schema: The knowledge base's schema, whose collection and disclosure settings the handler follows.

    Returns:
        The envelope handler.
    """
    return EnvelopeHandler(
        gateway.for_collection(schema.retrieval.collection),
        mode=schema.retrieval.disclosure_mode,
        excluded_page_types=schema.disclosure_excluded_page_types(),
        group_by=schema.retrieval.group_by,
        orientation=schema.orientation(),
        collection=schema.retrieval.collection,
    )


@injectable
def graph_expand_handler_provider(gateway: ReaderSqlGateway, schema: KnowledgeBaseSchema) -> GraphExpandHandler:
    """Provide the multi-hop graph-expand handler behind the `graph_expand` tool, scoped to this knowledge base's collection.

    Args:
        gateway: The read-only gateway.
        schema: The knowledge base's schema, whose collection the handler reads.

    Returns:
        The graph-expand handler.
    """
    return GraphExpandHandler(gateway.for_collection(schema.retrieval.collection), schema.retrieval.collection)


@injectable
def graph_paths_handler_provider(gateway: ReaderSqlGateway, schema: KnowledgeBaseSchema) -> GraphPathsHandler:
    """Provide the ranked graph-paths handler behind the `find_paths` tool, scoped to this knowledge base's collection.

    Args:
        gateway: The read-only gateway.
        schema: The knowledge base's schema, whose collection the handler reads.

    Returns:
        The graph-paths handler.
    """
    return GraphPathsHandler(gateway.for_collection(schema.retrieval.collection), schema.retrieval.collection)


@injectable
def status_handler_provider(admin: AdminSqlGateway) -> StatusHandler:
    """Provide the status handler, which reads the `kb.*` tables.

    Args:
        admin: The admin gateway.

    Returns:
        The status handler.
    """
    return StatusHandler(admin_gateway=admin)


@injectable
def knowledge_bases_handler_provider(admin: AdminSqlGateway) -> KnowledgeBasesHandler:
    """Provide the handler that reads the indexed figures of every knowledge base.

    Args:
        admin: The admin gateway.

    Returns:
        The knowledge bases handler.
    """
    return KnowledgeBasesHandler(admin_gateway=admin)


@injectable
def knowledge_base_validation_handler_provider(admin: AdminSqlGateway) -> KnowledgeBaseValidationHandler:
    """Provide the handler that reads the standing validation issues of one knowledge base.

    Args:
        admin: The admin gateway.

    Returns:
        The knowledge base validation handler.
    """
    return KnowledgeBaseValidationHandler(admin_gateway=admin)


@injectable
def harness_artifact_resolver_provider(harness: HarnessServiceSettings) -> HarnessArtifactResolver:
    """Provide the resolver of the harness's artifacts, at the one base URL this app was configured with.

    The address is settings, never a request, so a caller names which value it wants and never where the value is read from.

    Args:
        harness: The harness's base URL and the timeout of each request to it.

    Returns:
        The harness artifact resolver.
    """
    return HarnessArtifactResolver(harness.base_url, timeout_seconds=harness.timeout_seconds)


@injectable
def note_retention_provider(resolver: HarnessArtifactResolver, settings_file: SettingsFile) -> NoteRetention:
    """Provide the note-retention service over the harness's values, the registry of knowledge bases and a subprocess index.

    The index runs as a subprocess against the destination's own root, because this process holds no knowledge base of its own.

    Args:
        resolver: Reads the values the harness holds.
        settings_file: The settings file that holds the registry of knowledge bases.

    Returns:
        The note-retention service.
    """
    return NoteRetention(resolver, knowledge_base_reader(settings_file.path), SubprocessReindex(), RetentionReceipts)


@injectable
def knowledge_base_registry_provider(
    settings_file: SettingsFile,
    database: DatabaseSettings,
    indexed_knowledge_bases: KnowledgeBasesHandler,
    validation: KnowledgeBaseValidationHandler,
    admin: AdminSqlGateway,
    jobs: Jobs,
    job_queries: JobQueries,
) -> KnowledgeBaseRegistry:
    """Provide the registry of knowledge bases over the settings file this app was given.

    Args:
        settings_file: The settings file that holds the registry of knowledge bases.
        database: The database a knowledge base's identity names.
        indexed_knowledge_bases: Reads the indexed vitals of every knowledge base.
        validation: Reads the standing validation issues of one knowledge base.
        admin: The admin gateway a knowledge base's identity is read through.
        jobs: The writes to the jobs table.
        job_queries: The reads of the jobs table.

    Returns:
        The registry of knowledge bases.
    """
    registered = SettingsFileRegisteredKnowledgeBases(settings_file.path)
    return KnowledgeBaseRegistry(
        registered_knowledge_bases=registered,
        queries=registered,
        database=database,
        indexed_knowledge_bases=indexed_knowledge_bases,
        validation=validation,
        admin=admin,
        jobs=jobs,
        job_queries=job_queries,
    )


@injectable
def search_dialects_provider() -> SearchDialects:
    """Provide the registry of BM25 dialects, which holds both search dialects.

    A caller selects one by `settings.retrieval.dialect` instead of branching on the backend.

    Returns:
        The search dialect registry.
    """
    return build_search_dialects()


@injectable
def graph_backends_provider() -> GraphBackends:
    """Provide the registry of link-graph backends, which holds the recursive CTE backend and the Apache AGE backend.

    A caller selects one by `settings.retrieval.graph_backend` instead of branching on the backend.

    Returns:
        The graph backend registry.
    """
    return build_graph_backends()


@injectable
def search_reranking_provider(retrieval: RetrievalSettings, infrastructure: InfrastructureSettings) -> SearchReranking:
    """Provide the re-ranking studio's searches run through.

    Args:
        retrieval: The retrieval settings whose `rerank` block says how hits are re-ranked.
        infrastructure: The infrastructure settings whose OpenRouter section holds the key.

    Returns:
        The re-ranking, which holds `None` when it is off or OpenRouter has no key.
    """
    return SearchReranking(reranking(retrieval.rerank, infrastructure.openrouter))


REGISTRATIONS: list[object] = [
    reader_engine_provider,
    reader_gateway_provider,
    admin_gateway_provider,
    search_handler_provider,
    fetch_handler_provider,
    lookup_value_handler_provider,
    describe_schema_handler_provider,
    envelope_handler_provider,
    graph_expand_handler_provider,
    graph_paths_handler_provider,
    status_handler_provider,
    knowledge_bases_handler_provider,
    knowledge_base_validation_handler_provider,
    harness_artifact_resolver_provider,
    note_retention_provider,
    knowledge_base_registry_provider,
    search_dialects_provider,
    graph_backends_provider,
    search_reranking_provider,
]
"""Everything this slice puts in the container, each under the type its consumers declare."""
