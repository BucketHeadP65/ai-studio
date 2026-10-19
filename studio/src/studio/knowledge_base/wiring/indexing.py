"""What the index slice registers: the admin engine, the jobs table, the schema, and the handlers that index and ingest pages.

The engine registers the pgvector codec before it is handed out, so every pooled connection reads and writes `halfvec` columns. The unit
of work factory opens a fresh unit of work on each call, one per transaction. The validation report sink rewrites only the validation block
of the knowledge base's `_meta/dashboard.md`. The index handler cuts a heading section over the maximum where the chunk planner
the ingestion settings name says, over the Azure OpenAI resource the summaries use, and with no deployment or no endpoint it fills that
heading section.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.embedding import BatchEmbedder
from core.cqrs.contracts import JobQueries, Jobs, JobsAndQueries, PostgresJobs
from core.db import build_engine
from engram.indexing.contracts import (
    ChunkPlanner,
    ChunkSizes,
    CutPlanStage,
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBaseHandler,
    IndexUnitOfWork,
    IngestPageHandler,
    KnowledgeBaseReader,
    PydanticAiEmbedder,
    ReaderScopes,
    SqlAlchemyKnowledgeBaseUnitOfWork,
    Summarizer,
    SummaryStage,
    ValidationReportSink,
    cut_plan_identity,
    indexer_version,
    register_pgvector,
)
from engram.indexing.infrastructure.chunk_planner import PydanticAiChunkPlanner
from engram.indexing.infrastructure.cut_plans import SqlAlchemyCutPlans
from engram.indexing.infrastructure.summarizer import PydanticAiSummarizer
from engram.indexing.infrastructure.summary_uow import SqlAlchemySummaryUnitOfWork
from engram.kb.contracts import KnowledgeBaseSchema, VocabularyIndex, load_knowledge_base_schema, parse_vocabulary
from engram.retrieval.contracts import SqlReaderScopes
from sqlalchemy.ext.asyncio import AsyncEngine
from wireup import injectable

from studio.knowledge_base.indexing.infrastructure.validation_report_sink import FileValidationReportSink
from studio.knowledge_base.settings import (
    AzureOpenAiSettings,
    DatabaseSettings,
    InfrastructureSettings,
    IngestionSettings,
    KnowledgeBasePathOrigin,
    KnowledgeBaseSettings,
)

if TYPE_CHECKING:
    from pathlib import Path


class KnowledgeBaseUnitOfWorkFactory:
    """Opens a fresh unit of work over the admin engine on each call, one per transaction."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Hold the admin engine.

        Args:
            engine: The admin engine, with the pgvector codec registered.
        """
        self._engine = engine

    def __call__(self) -> IndexUnitOfWork:
        """Open a new unit of work over one transaction.

        Returns:
            The unit of work.
        """
        return SqlAlchemyKnowledgeBaseUnitOfWork(self._engine)


@injectable
async def engine_provider(database: DatabaseSettings) -> AsyncEngine:
    """Provide the admin engine, with the pgvector codec registered before it is handed out.

    Args:
        database: The database settings whose `url` the engine connects to.

    Returns:
        The admin engine.
    """
    engine = build_engine(database.url)
    await register_pgvector(engine)
    return engine


@injectable
def reader_scopes_provider(engine: AsyncEngine, database: DatabaseSettings) -> ReaderScopes:
    """Provide the owner-side provisioning of each private knowledge base's reader role.

    Args:
        engine: The admin engine, whose role creates the reader roles.
        database: The database settings whose `reader_secret` each private reader's password is derived from.

    Returns:
        The reader scopes over the admin engine.
    """
    return SqlReaderScopes(engine, database.reader_secret)


@injectable
def jobs_and_queries_provider(engine: AsyncEngine) -> JobsAndQueries:
    """Provide the jobs table in Postgres over the admin engine, the same table the job runner claims from.

    Args:
        engine: The admin engine.

    Returns:
        The one instance behind both the writes to and the reads of the jobs table.
    """
    return PostgresJobs(engine)


@injectable
def jobs_provider(jobs: JobsAndQueries) -> Jobs:
    """Provide the writes to the jobs table, on the one instance behind both ports.

    Args:
        jobs: The instance behind both ports.

    Returns:
        The writes to the jobs table.
    """
    return jobs


@injectable
def job_queries_provider(jobs: JobsAndQueries) -> JobQueries:
    """Provide the reads of the jobs table, on the one instance behind both ports.

    Args:
        jobs: The instance behind both ports.

    Returns:
        The reads of the jobs table.
    """
    return jobs


@injectable
def knowledge_base_reader_provider(knowledge_base: KnowledgeBaseSettings, schema: KnowledgeBaseSchema) -> KnowledgeBaseReader:
    """Provide the filesystem reader of the knowledge base's notes, the input of the parse stage.

    It finds notes in the folders and files the schema declares for its page types, relative to the knowledge base root.

    Args:
        knowledge_base: The knowledge base whose root and sources folder the reader walks.
        schema: The knowledge base's schema, whose `note_folders` and `note_files` the reader walks.

    Returns:
        The knowledge base reader.
    """
    return FilesystemKnowledgeBaseReader(knowledge_base.path, knowledge_base.sources_subdir, schema.note_folders, note_files=schema.note_files)


@injectable
def embedder_provider(infrastructure: InfrastructureSettings, ingestion: IngestionSettings) -> BatchEmbedder:
    """Provide the embedding model, the port of the embed stage.

    Args:
        infrastructure: The shared infrastructure settings that name the embedding connection.
        ingestion: The ingestion settings that shape the embeddings.

    Returns:
        The embedding port.
    """
    return PydanticAiEmbedder.from_settings(infrastructure, ingestion)


@injectable
def summarizer_provider(azure_openai: AzureOpenAiSettings, ingestion: IngestionSettings) -> Summarizer:
    """Provide the Azure model that writes the disclosure summaries.

    Args:
        azure_openai: The Azure OpenAI resource the deployment runs on.
        ingestion: The ingestion settings that name the summary model.

    Returns:
        The summarizer.
    """
    return PydanticAiSummarizer.from_settings(azure_openai, ingestion)


@injectable
def summary_stage_provider(engine: AsyncEngine, summarizer: Summarizer) -> SummaryStage:
    """Provide the disclosure-summary stage, which opens a fresh unit of work on each run.

    Providing the stage opens no session and touches no database.

    Args:
        engine: The admin engine each unit of work is opened over.
        summarizer: The model that writes the summaries.

    Returns:
        The summary stage.
    """
    return SummaryStage(lambda: SqlAlchemySummaryUnitOfWork(engine), summarizer)


@injectable
def validation_report_sink_provider(knowledge_base: KnowledgeBaseSettings) -> ValidationReportSink:
    """Provide the sink that writes each index run's validation report into the knowledge base's `_meta/dashboard.md`.

    Args:
        knowledge_base: The knowledge base the page belongs to.

    Returns:
        The validation report sink.
    """
    return FileValidationReportSink(knowledge_base.path)


@injectable
def schema_provider(knowledge_base: KnowledgeBaseSettings) -> KnowledgeBaseSchema:
    """Provide the knowledge base's schema, read from its `knowledge-base-schema.json`.

    Args:
        knowledge_base: The knowledge base whose schema file is read.

    Returns:
        The knowledge base's schema.

    Raises:
        KnowledgeBaseSchemaMissingError: When the knowledge base has no schema file.
        ValidationError: When the schema file is invalid.
    """
    return load_knowledge_base_schema(knowledge_base.path)


@injectable
def vocabulary_provider(knowledge_base: KnowledgeBaseSettings) -> VocabularyIndex:
    """Provide the knowledge base's vocabulary, read from its vocabulary pages, which an ingested page is checked against.

    The vocabulary is empty when the knowledge base has no vocabulary folder, so nothing is flagged as unregistered, the same
    as at index validation.

    Args:
        knowledge_base: The knowledge base whose vocabulary pages are read.

    Returns:
        The vocabulary index.
    """
    vocabulary_dir = knowledge_base.path / "_meta" / "vocabulary"
    documents = [(p.name, p.read_text(encoding="utf-8")) for p in sorted(vocabulary_dir.glob("*.md"))] if vocabulary_dir.is_dir() else []
    return parse_vocabulary(documents).index


def ingestion_chunk_sizes(ingestion: IngestionSettings) -> ChunkSizes:
    """The chunk sizes the ingestion settings name.

    Args:
        ingestion: The ingestion settings.

    Returns:
        The chunk sizes.
    """
    return ChunkSizes(target_tokens=ingestion.chunk_target_tokens, max_tokens=ingestion.chunk_max_tokens)


def ingestion_chunk_planner(azure_openai: AzureOpenAiSettings, ingestion: IngestionSettings) -> ChunkPlanner | None:
    """The chunk planner over the Azure OpenAI deployment the ingestion settings name.

    Args:
        azure_openai: The Azure OpenAI resource the deployment runs on.
        ingestion: The ingestion settings that name the chunk planner's deployment.

    Returns:
        The chunk planner, or `None` when the settings name no deployment or the resource has no endpoint.
    """
    if not _names_a_chunk_planner(azure_openai, ingestion):
        return None
    return PydanticAiChunkPlanner.from_settings(azure_openai, ingestion)


def _names_a_chunk_planner(azure_openai: AzureOpenAiSettings, ingestion: IngestionSettings) -> bool:
    """Whether the settings name a chunk planner deployment and the Azure OpenAI resource has an endpoint."""
    return bool(ingestion.chunk_planner_model and azure_openai.endpoint)


def ingestion_cut_plan_stage(engine: AsyncEngine, azure_openai: AzureOpenAiSettings, ingestion: IngestionSettings) -> CutPlanStage | None:
    """The stage that gives each load's mirrors their cut plans, over the chunk planner the ingestion settings name.

    Args:
        engine: The admin engine the stored cut plans are read and written over.
        azure_openai: The Azure OpenAI resource the chunk planner runs on.
        ingestion: The ingestion settings that name the chunk planner and its ceiling, deadline and concurrency.

    Returns:
        The stage, or `None` when there is no chunk planner.
    """
    planner = ingestion_chunk_planner(azure_openai, ingestion)
    if planner is None:
        return None
    cut_plans = SqlAlchemyCutPlans(engine)
    return CutPlanStage(
        planner,
        cut_plans,
        cut_plans,
        ceiling_tokens=ingestion.chunk_planner_ceiling_tokens,
        deadline_seconds=ingestion.chunk_planner_deadline_seconds,
        concurrency=ingestion.chunk_planner_concurrency,
    )


def ingestion_indexer_version(azure_openai: AzureOpenAiSettings, ingestion: IngestionSettings) -> str:
    """The indexer version an index under these settings stores: the chunk sizes and the chunk planner with its ceiling.

    It builds no model: the chunk planner's identity comes from its deployment name.

    Args:
        azure_openai: The Azure OpenAI resource the chunk planner runs on.
        ingestion: The ingestion settings that name the chunk sizes and the chunk planner.

    Returns:
        The version the index handler built from the same settings stores.
    """
    sizes = ingestion_chunk_sizes(ingestion)
    if not _names_a_chunk_planner(azure_openai, ingestion):
        return indexer_version(sizes)
    planner = cut_plan_identity(PydanticAiChunkPlanner.identity_for(ingestion.chunk_planner_model))
    return indexer_version(sizes, planner=planner, planner_ceiling_tokens=ingestion.chunk_planner_ceiling_tokens)


def _path_root(knowledge_base: KnowledgeBaseSettings, schema: KnowledgeBaseSchema) -> Path:
    """The folder the schema's `path_fields` values resolve against: the knowledge base root, or by default its parent."""
    return knowledge_base.path if schema.path_root == "knowledge_base" else knowledge_base.path.parent


@injectable
def index_handler_provider(  # noqa: PLR0913, PLR0917 (wireup resolves each handler dependency by type, so each is its own parameter)
    reader: KnowledgeBaseReader,
    embedder: BatchEmbedder,
    unit_of_work_factory: KnowledgeBaseUnitOfWorkFactory,
    validation_report_sink: ValidationReportSink,
    schema: KnowledgeBaseSchema,
    summary_stage: SummaryStage,
    knowledge_base: KnowledgeBaseSettings,
    knowledge_base_origin: KnowledgeBasePathOrigin,
    reader_scopes: ReaderScopes,
    ingestion: IngestionSettings,
    azure_openai: AzureOpenAiSettings,
    engine: AsyncEngine,
) -> IndexKnowledgeBaseHandler:
    """Provide the index handler.

    The summary stage runs only when an index asks for summaries. The dangling code path check, on both the full and the
    incremental index, resolves the schema's `path_fields` against the folder the schema declares, and its message names
    where the knowledge base path came from. Every source mirror is cut into chunks of the sizes the ingestion settings name,
    and a heading section over the maximum is cut where the chunk planner they name says, when there is one.

    Args:
        reader: The reader of the knowledge base's notes.
        embedder: The embedding model the pages are embedded with.
        unit_of_work_factory: Opens one unit of work per transaction.
        validation_report_sink: Where each index run's validation report is written.
        schema: The knowledge base's schema.
        summary_stage: Writes the disclosure summaries when an index asks for them.
        knowledge_base: The knowledge base the handler indexes.
        knowledge_base_origin: Where the knowledge base path came from.
        reader_scopes: Gives a private knowledge base its own reader role after each real run.
        ingestion: The ingestion settings that name the chunk sizes and the chunk planner.
        azure_openai: The Azure OpenAI resource the chunk planner runs on.
        engine: The admin engine the stored cut plans are read and written over.

    Returns:
        The index handler.
    """
    return IndexKnowledgeBaseHandler(
        reader,
        embedder,
        unit_of_work_factory,
        validation_report_sink,
        spec=schema,
        summary_stage=summary_stage,
        path_root=_path_root(knowledge_base, schema),
        path_root_source=knowledge_base_origin.source,
        reader_scopes=reader_scopes,
        chunk_sizes=ingestion_chunk_sizes(ingestion),
        cut_plan_stage=ingestion_cut_plan_stage(engine, azure_openai, ingestion),
    )


@injectable
def ingest_handler_provider(
    embedder: BatchEmbedder,
    unit_of_work_factory: KnowledgeBaseUnitOfWorkFactory,
    schema: KnowledgeBaseSchema,
    vocabulary: VocabularyIndex,
    reader_scopes: ReaderScopes,
) -> IngestPageHandler:
    """Provide the handler that writes one page straight into the index, checked against the knowledge base's vocabulary.

    It refuses a page whose entity type, facet value, claim type or relation is not in the vocabulary before anything is
    written. It reads no notes and writes no validation report.

    Args:
        embedder: The embedding model the page is embedded with.
        unit_of_work_factory: Opens the unit of work the page is written in.
        schema: The knowledge base's schema.
        vocabulary: The vocabulary each page is checked against.
        reader_scopes: Gives a private knowledge base its own reader role after a write.

    Returns:
        The ingest handler.
    """
    return IngestPageHandler(unit_of_work_factory, embedder, spec=schema, vocabulary=vocabulary, reader_scopes=reader_scopes)


REGISTRATIONS: list[object] = [
    engine_provider,
    reader_scopes_provider,
    jobs_and_queries_provider,
    jobs_provider,
    job_queries_provider,
    injectable(KnowledgeBaseUnitOfWorkFactory),
    knowledge_base_reader_provider,
    embedder_provider,
    summarizer_provider,
    summary_stage_provider,
    validation_report_sink_provider,
    schema_provider,
    vocabulary_provider,
    index_handler_provider,
    ingest_handler_provider,
]
"""Everything this slice puts in the container, each under the type its consumers declare."""
