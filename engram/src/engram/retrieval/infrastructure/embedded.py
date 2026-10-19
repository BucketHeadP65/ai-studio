"""engram's read and write surfaces composed for a host that imports engram without a DI container.

Each context manager takes a DSN in any spelling `core.db.async_dsn` accepts.

* `embedded_knowledge_base` opens the SQL tools (`run_sql` and `describe_schema`) as a capability.
* `embedded_retrieval` and `embedded_read_tools` open the read tools (`search`, leveled `fetch`,
  and `graph_expand` and `find_paths` when the graph switch is on) over one or several knowledge bases.
* `embedded_authoring` opens the validated ingest path over a writer DSN, and `document_embedder`
  builds the embedder it needs.
* `slug_only_stamp` is the freshness stamp a reader-only host can give.
* `policy_instructions` reads a knowledge base's applied `RetrievalPolicy.instructions` on demand.

Every context manager disposes its engine on exit.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from asyncpg import PostgresError
from core.ai import create_embedding_model
from core.ai.embedding import Embeddings, query_model_of
from core.ai.retrieval.contracts import KnowledgeBaseStamp
from core.db import async_dsn, build_reader_engine
from core.observability import get_logger
from pydantic import ValidationError
from pydantic_ai import Embedder
from pydantic_ai.embeddings import EmbeddingSettings
from pydantic_ai.tools import AgentDepsT
from sqlalchemy import NullPool
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from engram.evals.tuning.contracts import load_active_policy
from engram.indexing.contracts import (
    IngestPage,
    IngestPageHandler,
    PydanticAiEmbedder,
    RetrievalSwitches,
    SqlAlchemyKnowledgeBaseUnitOfWork,
    register_pgvector,
)
from engram.kb.contracts import AuthoringPayload, KnowledgeBaseSchema, parse_vocabulary
from engram.retrieval.capability import DEFAULT_TOOL_NAMES, ReadSurface, RetrievalKnowledgeBase, SqlKnowledgeBase
from engram.retrieval.earned import Earned, SwitchCache
from engram.retrieval.envelope import EnvelopeHandler
from engram.retrieval.queries import DescribeSchemaHandler, FetchHandler, GraphExpandHandler, GraphPathsHandler, SearchHandler
from engram.retrieval.tools import RetrievalHandlers

from .gateway import ReadOnlySqlGateway, verify_reader_role
from .reader_roles import PrivateReaders

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from core.ai.embedding import BatchEmbedder, QueryEmbedder
    from core.ai.retrieval.contracts import StampSource
    from core.settings import InfrastructureSettings

    from engram.authoring.contracts import AuthoringTools
    from engram.indexing.contracts import IngestResult
    from engram.kb.contracts import VocabularyIndex
    from engram.retrieval.dials import DialsSource
    from engram.retrieval.domain import SqlGateway
    from engram.retrieval.queries import KnowledgeBaseSchemaView
    from engram.retrieval.rerank import Reranking

_logger = get_logger(__name__)


@asynccontextmanager
async def embedded_knowledge_base(
    reader_url: str,
    *,
    domain: str,
    embed_query: QueryEmbedder | None = None,
    guidance: Callable[[], str | None] | None = None,
    tool_names: tuple[str, str] = DEFAULT_TOOL_NAMES,
    spec: KnowledgeBaseSchemaView | None = None,
    grounding: Sequence[KnowledgeBaseSchemaView] = (),
    reader_secret: str = "",
) -> AsyncGenerator[SqlKnowledgeBase[AgentDepsT] | Earned[AgentDepsT]]:
    """Open engram's read surface over `reader_url` as a capability, and dispose it on exit.

    Args:
        reader_url: The SQLAlchemy async DSN for the read-only role (`postgresql+asyncpg://...`).
            Every tool on this surface is a read.
        domain: What knowledge this knowledge base holds, in the host's own words.
        embed_query: Embeds a `vector_query => '<text>'` for the hybrid leg. `None`, or a callable
            that raises, degrades that leg to BM25 with a note rather than failing the query.
        guidance: How to retrieve well from this knowledge base, read per request.
            `policy_instructions` builds one from the knowledge base's applied policy. `None` speaks none.
        tool_names: `(run_sql, describe_schema)` under this host's names.
        spec: The knowledge base this capability reads as, and the only thing that decides its identity.
            A full `KnowledgeBaseSchema` reads as that knowledge base's collection, and `retrieval.mode ==
            "auto"` also wraps the capability in `Earned`, seeded from that knowledge base's stored
            `sql` switch. `None`, or a narrower spec that names no collection, reads anonymously,
            seeing only what the privacy predicate allows, and is never `Earned`.
        grounding: Further schema specs `describe_schema` should be grounded in, after `spec`.
            They never give the capability an identity.
        reader_secret: The secret a private knowledge base's reader role password is derived from. A private `spec`
            is read through that role. Empty, it reads anonymously and sees none of itself.

    Yields:
        The live capability: the bare `SqlKnowledgeBase`, or `Earned` wrapping it. The engine behind it is
        disposed when the context exits.
    """
    own = spec if isinstance(spec, KnowledgeBaseSchema) else None
    described = [spec, *grounding] if spec is not None else list(grounding)
    engine = build_reader_engine(reader_url)
    private_readers = PrivateReaders(reader_url, reader_secret)
    try:
        await verify_reader_role(engine)
        await register_pgvector(engine)
        gateway = ReadOnlySqlGateway(
            engine, collection=own.retrieval.collection if own is not None else None, private_readers=private_readers
        )
        sql_knowledge_base = SqlKnowledgeBase[AgentDepsT](
            gateway=gateway,
            describe_handler=DescribeSchemaHandler(gateway, described),
            domain=domain,
            embed_query=embed_query,
            guidance=guidance,
            tool_names=tool_names,
        )
        if own is not None and own.retrieval.mode == "auto":
            read_switches = switch_source(gateway, own.retrieval.collection)
            cache = SwitchCache(read=read_switches, current=await read_switches())
            yield Earned[AgentDepsT](sql_knowledge_base, earned=lambda: cache.switches().sql, id_suffix=f"sql:{own.retrieval.collection}")
        else:
            yield sql_knowledge_base
    finally:
        await private_readers.dispose()
        await engine.dispose()


@dataclass
class _QueryEmbedder:
    """A `QueryEmbedder` callable as the slice of the embedding port the search handler reads."""

    embed: QueryEmbedder | None

    @property
    def model_id(self) -> str:
        return query_model_of(self.embed) or ""

    @property
    def dimensions(self) -> int:
        return 0

    async def embed_batch(self, texts: Sequence[str]) -> Embeddings:
        if self.embed is None:
            msg = "this knowledge base was opened without a query embedder. Its search is keyword-only"
            raise RuntimeError(msg)
        return Embeddings(vectors=[list(await self.embed(text)) for text in texts], input_tokens=0)


_RETRIEVAL_SWITCHES_SQL = "SELECT api.retrieval_switches(:collection) AS switches"


def switch_source(gateway: SqlGateway, collection: str) -> Callable[[], Awaitable[RetrievalSwitches]]:
    """Build a reader of one collection's stored retrieval switches that reads them again on every call.

    A database error, a missing row or a malformed row answers every switch off. A connection that
    cannot be opened still raises.

    Args:
        gateway: The reader gateway `api.retrieval_switches` is read through.
        collection: The collection whose switches are read.

    Returns:
        The reader, to set as a `ReadSurface.switch_source`.
    """

    async def read_switches() -> RetrievalSwitches:
        try:
            result = await gateway.rows(_RETRIEVAL_SWITCHES_SQL, {"collection": collection}, cap=1)
        except SQLAlchemyError:
            _logger.warning("reading the retrieval switches for {collection!r} failed. Answering with every switch off", collection=collection)
            return RetrievalSwitches()
        if not result.rows:
            return RetrievalSwitches()
        raw = result.rows[0].get("switches")
        if not isinstance(raw, dict):
            return RetrievalSwitches()
        try:
            return RetrievalSwitches.model_validate(raw)
        except ValidationError:
            return RetrievalSwitches()

    return read_switches


async def read_hits_per_document(search: SearchHandler, *, knowledge_base: str, collection: str) -> int:
    """Read the most hits one document of a knowledge base keeps in `search`'s search, the number its tool description states.

    A database error, an unreachable database or a database whose cap function takes no knowledge base reads as 0, so
    no description states a cap the host could not read. That failure is logged as a warning naming the knowledge base
    and the error.

    Args:
        search: The search handler whose database is read.
        knowledge_base: What the host calls the knowledge base `search` reads, named in the warning.
        collection: That knowledge base's collection, whose cap is read.

    Returns:
        The cap, 0 when the search keeps every hit or the cap could not be read.
    """
    try:
        return await search.hits_per_document(collection)
    except (SQLAlchemyError, PostgresError, OSError) as error:
        _logger.warning(
            "reading the cap on hits per document for {knowledge_base!r} failed ({failure}). Its search description states no cap",
            error=error,
            knowledge_base=knowledge_base,
            failure=f"{type(error).__name__}: {error}",
        )
        return 0


@asynccontextmanager
async def embedded_retrieval(
    reader_url: str,
    *,
    spec: KnowledgeBaseSchema | Path,
    domain: str | None = None,
    embed_query: QueryEmbedder | None = None,
    guidance: Callable[[], str | None] | None = None,
    k_source: Callable[[], int] | None = None,
    dials_source: DialsSource | None = None,
    reader_secret: str = "",
) -> AsyncGenerator[RetrievalKnowledgeBase[AgentDepsT]]:
    """Open one knowledge base's `search` + leveled `fetch` over `reader_url` as a capability.

    The knowledge base's own spec is the whole configuration: its collection scopes the search, its
    `disclosure_mode` and per-page_type `disclosed` declarations govern what the leveled fetch reveals,
    and its `purpose` speaks for the knowledge base unless the host has better words.

    Args:
        reader_url: The DSN for the read-only role, in any spelling `core.db.async_dsn` accepts.
            Read-only is the security boundary: every tool on this surface is a read.
        spec: The knowledge base's `KnowledgeBaseSchema`, or the path to its `knowledge-base-schema.json`.
        domain: What knowledge this knowledge base holds, in the host's own words. `None` speaks the
            spec's `purpose`.
        embed_query: Embeds the query for the search's vector leg. `None` builds the search keyword-only.
        guidance: How to retrieve well from this knowledge base, read per request. `policy_instructions`
            builds one from the knowledge base's applied policy. `None` speaks none.
        k_source: The search row cap when the host owns it rather than the agent, read on every call.
            When set, `search` takes no `k`. `None` leaves the cap to the agent.
        dials_source: The live read modes (`ReadDials`) when the host owns them, read on every call.
            `None` serves hybrid search and leveled reads.
        reader_secret: The secret a private knowledge base's reader role password is derived from. Empty, a private
            knowledge base reads as empty.

    Yields:
        The live capability. The engine behind it is disposed when the context exits.
    """
    connection = KnowledgeBaseConnection(
        reader_url, spec=spec, domain=domain, embed_query=embed_query, guidance=guidance, reader_secret=reader_secret
    )
    async with embedded_read_tools(connection, k_source=k_source, dials_source=dials_source) as reader:
        yield reader


@dataclass(frozen=True)
class KnowledgeBaseConnection:
    """One knowledge base as `embedded_read_tools` opens it: where it lives, and its voice."""

    reader_url: str
    """The DSN for the read-only role, in any spelling `core.db.async_dsn` accepts."""
    spec: KnowledgeBaseSchema | Path
    """The knowledge base's `KnowledgeBaseSchema`, or the path to its spec JSON."""
    domain: str | None = None
    """What this knowledge base holds, in the host's own words. `None` speaks the spec's `purpose`."""
    embed_query: QueryEmbedder | None = None
    """Embeds the query for the search's vector leg. `None` builds this knowledge base's search keyword-only."""
    guidance: Callable[[], str | None] | None = None
    """How to retrieve well from this knowledge base, read per request. `None` speaks none."""
    reader_secret: str = ""
    """The secret a private knowledge base's reader role password is derived from. Empty, a private one reads as empty."""


@asynccontextmanager
async def embedded_read_tools(
    *connections: KnowledgeBaseConnection,
    k_source: Callable[[], int] | None = None,
    dials_source: DialsSource | None = None,
    cross_search: bool = False,
    default_knowledge_base_source: Callable[[], str | None] | None = None,
    authoring: AuthoringTools | None = None,
    rerank: Reranking | None = None,
) -> AsyncGenerator[RetrievalKnowledgeBase[AgentDepsT]]:
    """Open several knowledge bases behind the same read tools, one `search` and one `fetch`.

    Each knowledge base's spec is its whole configuration: its collection scopes its search, its
    declared facets are the keys `search` filters it by in `facets`, its disclosure declarations
    govern what the leveled fetch reveals there, and its `purpose` speaks for it unless the host has
    better words. `search` and `fetch` take a `knowledge_base` argument
    naming one of them, required unless the host turns on `cross_search`. A knowledge base whose
    spec sets `retrieval.mode` to `"auto"` reads its stored retrieval switches fresh on every call,
    `.embeddings` for the search's vector leg and `.graph` for whether `graph_expand` and `find_paths`
    are exposed, and a `"fixed"` spec never reads them. Each knowledge base's cap on hits per document is read once,
    when it opens, and the `search` description states it.

    Args:
        connections: One `KnowledgeBaseConnection` per knowledge base, in the order they are listed and searched.
        k_source: The search row cap when the host owns it rather than the agent, read on every call
            and applied across every knowledge base. When set, `search` takes no `k` and the value bounds
            the merged hits. `None` leaves the cap to the agent.
        dials_source: The live read modes (`ReadDials`) when the host owns them, read on every call
            and applied across every knowledge base. `None` serves hybrid search, where a knowledge base
            has an embedder, and leveled reads.
        cross_search: Whether one `search` or `fetch` call may read more than one knowledge base.
            `False` (the default) makes `knowledge_base` a required argument on both tools and refuses a
            fan-out. `True` makes it optional, an omitted one reads every knowledge base, and
            `knowledge_base="all"` is accepted.
        default_knowledge_base_source: The knowledge base an omitted `knowledge_base` searches, when the
            host owns one. Consulted only while `cross_search` is on. `None` (the default) searches
            every knowledge base.
        authoring: The authoring tools, when a step is granted them. `None` (the default) keeps the
            surface read-only.
        rerank: How every knowledge base's search hits are re-ranked. `None` (the default) keeps each search's own order.

    Yields:
        The live capability. Every engine behind it is disposed when the context exits.
    """
    engines: list[AsyncEngine] = []
    private_readers: list[PrivateReaders] = []
    try:
        surfaces: list[ReadSurface] = []
        for connection in connections:
            knowledge_base_spec = connection.spec if isinstance(connection.spec, KnowledgeBaseSchema) else KnowledgeBaseSchema.from_path(connection.spec)
            engine = build_reader_engine(connection.reader_url)
            engines.append(engine)
            await verify_reader_role(engine)
            await register_pgvector(engine)
            private_readers.append(PrivateReaders(connection.reader_url, connection.reader_secret))
            gateway = ReadOnlySqlGateway(engine, collection=knowledge_base_spec.retrieval.collection, private_readers=private_readers[-1])
            search = SearchHandler(gateway, _QueryEmbedder(connection.embed_query))
            fetch = FetchHandler(gateway, knowledge_base_spec.retrieval.collection)
            surfaces.append(
                ReadSurface(
                    name=knowledge_base_spec.slug,
                    collection=knowledge_base_spec.retrieval.collection,
                    handlers=RetrievalHandlers(
                        search=rerank.over(search, fetch) if rerank is not None else search,
                        fetch=fetch,
                        graph_expand=GraphExpandHandler(gateway, knowledge_base_spec.retrieval.collection),
                        find_paths=GraphPathsHandler(gateway, knowledge_base_spec.retrieval.collection),
                    ),
                    envelope=EnvelopeHandler(
                        gateway,
                        mode=knowledge_base_spec.retrieval.disclosure_mode,
                        excluded_page_types=knowledge_base_spec.disclosure_excluded_page_types(),
                        group_by=knowledge_base_spec.retrieval.group_by,
                        orientation=knowledge_base_spec.orientation(),
                        collection=knowledge_base_spec.retrieval.collection,
                    ),
                    domain=connection.domain or knowledge_base_spec.purpose,
                    guidance=connection.guidance,
                    stamp=slug_only_stamp(knowledge_base_spec.slug),
                    hybrid=connection.embed_query is not None,
                    switch_source=switch_source(gateway, knowledge_base_spec.retrieval.collection)
                    if knowledge_base_spec.retrieval.mode == "auto"
                    else None,
                    facet_keys=tuple(facet.key for facet in knowledge_base_spec.facets),
                    hits_per_document=await read_hits_per_document(
                        search, knowledge_base=knowledge_base_spec.slug, collection=knowledge_base_spec.retrieval.collection
                    ),
                )
            )
        yield RetrievalKnowledgeBase[AgentDepsT](
            surfaces=tuple(surfaces),
            k_source=k_source,
            cross_search=cross_search,
            default_knowledge_base_source=default_knowledge_base_source,
            dials_source=dials_source,
            authoring=authoring,
        )
    finally:
        for readers in private_readers:
            await readers.dispose()
        for engine in engines:
            await engine.dispose()


def document_embedder(
    *, deployment: str, infrastructure: InfrastructureSettings, dimensions: int, batch_size: int = 64, batch_tokens: int = 50_000, concurrency: int = 4
) -> BatchEmbedder:
    """The write-side embedder from the embedding contract's three facts.

    You must pass the deployment and width the knowledge base was embedded with, recorded in
    `kb.embed_meta`. A different one ranks badly and raises nothing. Requests are grouped by engram's
    token count and truncated to its embed-input budget.

    Args:
        deployment: The embedding deployment name.
        infrastructure: The shared infrastructure settings. Its `embeddings` picks Azure OpenAI or the gateway.
        dimensions: The vector width.
        batch_size: The most texts one embedding request carries.
        batch_tokens: The most tokens one embedding request carries.
        concurrency: The most embedding requests in flight at once.

    Returns:
        The embedding port the write path embeds rows with.
    """
    model = create_embedding_model(deployment, infrastructure=infrastructure)
    embedder = Embedder(model, settings=EmbeddingSettings(dimensions=dimensions))
    return PydanticAiEmbedder(
        embedder, deployment=deployment, dimensions=dimensions, batch_size=batch_size, batch_tokens=batch_tokens, concurrency=concurrency
    )


class IngestDocument(Protocol):
    """The write callable `embedded_authoring` yields: one authoring payload in, the ingest's counts out."""

    async def __call__(self, payload: Mapping[str, object], *, collection: str, embed: bool = True) -> IngestResult:
        """Validate `payload` against the knowledge base's spec and land it as rows.

        Args:
            payload: The authoring payload.
            collection: The collection the page is written into.
            embed: Whether to embed the rows as they land.

        Returns:
            The ingest's counts.

        Raises:
            pydantic.ValidationError: When the payload does not satisfy the spec's page grammar.
            IngestValidationError: When the page's entity type, facet values, claim types or relations
                are not in the knowledge base's vocabulary. Nothing is written.
            CollectionIsIndexedError: When a knowledge base indexes `collection`, so the next index
                would prune the page this would write.
        """
        ...


@asynccontextmanager
async def embedded_authoring(
    writer_url: str,
    *,
    spec: KnowledgeBaseSchema | Path,
    embedder: BatchEmbedder,
) -> AsyncGenerator[IngestDocument]:
    """Open engram's write path over `writer_url`, the same validated route the indexer's ingest stage runs.

    The page grammar and the knowledge base's vocabulary are enforced before anything is written, and a
    collection some knowledge base indexes is refused. A spec object has no vocabulary pages, so its
    pages are checked against an empty vocabulary, which refuses any entity type, claim type or relation.

    Args:
        writer_url: The DSN for a role that may write, in any spelling `core.db.async_dsn`
            accepts. With a read-only role every write is refused by Postgres.
        spec: The knowledge base spec the payloads are validated against, or the path to its spec JSON at
            the knowledge base root, whose `_meta/vocabulary/` pages are the vocabulary.
        embedder: The write-side embedder. `document_embedder` builds one. Rows are embedded as they land.

    Yields:
        The `IngestDocument` callable. The engine behind it is disposed when the context exits.
    """
    if isinstance(spec, KnowledgeBaseSchema):
        knowledge_base_spec, vocabulary = spec, parse_vocabulary(()).index
    else:
        knowledge_base_spec, vocabulary = KnowledgeBaseSchema.from_path(spec), _vocabulary_beside(spec)
    engine = create_async_engine(async_dsn(writer_url), poolclass=NullPool)
    try:
        await register_pgvector(engine)
        handler = IngestPageHandler(
            lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine), embedder, spec=knowledge_base_spec, vocabulary=vocabulary
        )

        async def ingest(payload: Mapping[str, object], *, collection: str, embed: bool = True) -> IngestResult:
            authored = AuthoringPayload.model_validate({**payload, "knowledge_base_schema": knowledge_base_spec})
            result = await handler.handle(IngestPage(collection=collection, payload=authored, embed=embed))
            # No bus drains the handler's events here, so clear them after each ingest.
            handler.events.clear()
            return result

        yield ingest
    finally:
        await engine.dispose()


def _vocabulary_beside(spec_path: Path) -> VocabularyIndex:
    """The vocabulary parsed from `_meta/vocabulary/*.md` beside the spec file, empty when that folder is absent."""
    vocabulary_dir = spec_path.parent / "_meta" / "vocabulary"
    pages = sorted(vocabulary_dir.glob("*.md")) if vocabulary_dir.is_dir() else []
    return parse_vocabulary((page.name, page.read_text(encoding="utf-8")) for page in pages).index


def policy_instructions(knowledge_base_path: Path) -> Callable[[], str | None]:
    """The knowledge base's applied-policy instructions, re-read when the policy file changes underneath.

    The file is read again only when its mtime or size changes. A file that does not load is logged
    and answers `None`, never raises.

    Args:
        knowledge_base_path: The knowledge base root `_meta/active-policy.yaml` is read from.

    Returns:
        A callable answering the applied policy's instructions, or `None` when the knowledge base carries no
        policy or the file could not be read.
    """
    path = knowledge_base_path / "_meta" / "active-policy.yaml"
    stamp: tuple[float, int] | None = None
    cached: str | None = None

    def read() -> str | None:
        nonlocal stamp, cached
        try:
            stat = path.stat()
        except OSError:
            stamp, cached = None, None
            return None
        current = (stat.st_mtime, stat.st_size)
        if current == stamp:
            return cached
        try:
            resolution = load_active_policy(knowledge_base_path)
        except (ValidationError, ValueError, OSError):
            _logger.warning("ignoring the active policy at {path}: it did not load", path=str(path))
            stamp, cached = current, None
            return None
        stamp = current
        cached = resolution.instructions if resolution is not None else None
        return cached

    return read


def slug_only_stamp(slug: str) -> StampSource:
    """Name a knowledge base on every answer and leave `changed_since` unmeasured.

    Use it when a host holds only a reader DSN, which cannot read the index manifest. A host with both
    roles builds its own stamp with `engram.identity.read_stamp` instead.

    Args:
        slug: The knowledge base's own slug, as its `knowledge-base-schema.json` declares it.

    Returns:
        A `StampSource` naming `slug` with nothing claimed about how current its rows are.
    """

    async def stamp() -> KnowledgeBaseStamp:
        return KnowledgeBaseStamp(slug=slug, changed_since=None)

    return stamp
