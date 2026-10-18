"""The indexing slice's SQLAlchemy unit of work, which holds one rebuild in one transaction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from core.cqrs.contracts import CoordinationBuffer, flush
from sqlalchemy.ext.asyncio import AsyncSession

from engram.indexing.infrastructure.repository import (
    ClaimQualifiers,
    Claims,
    Edges,
    EmbedMeta,
    IndexFingerprints,
    LinkRefs,
    ManifestEntries,
    Pages,
    PageTypeContracts,
    Sections,
    SignalsQueries,
    SourceRepository,
    UnitSummaries,
    ValidationIssues,
    VocabEntries,
)

if TYPE_CHECKING:
    from uuid import UUID

    from core.cqrs.contracts import Event
    from sqlalchemy.ext.asyncio import AsyncEngine


class SqlAlchemyKnowledgeBaseUnitOfWork:
    """The SQLAlchemy `KnowledgeBaseUnitOfWork`: one session and one transaction for one rebuild.

    You must register the pgvector codec on the engine first. This class never manages the engine.
    Every repository shares the session, so every write of a rebuild lands in the one transaction `commit` persists.
    Leaving the context without `commit` rolls everything back.
    Use a fresh instance per transaction and never enter one twice.
    """

    pages: Pages
    sections: Sections
    claims: Claims
    claim_qualifiers: ClaimQualifiers
    edges: Edges
    sources: SourceRepository
    vocab: VocabEntries
    summaries: UnitSummaries
    meta: EmbedMeta
    manifest: ManifestEntries
    link_refs: LinkRefs
    validation_store: ValidationIssues
    fingerprint: IndexFingerprints
    page_type_contracts: PageTypeContracts
    signals: SignalsQueries

    def __init__(self, engine: AsyncEngine) -> None:
        """Open a session on `engine` and bind every repository to it.

        Args:
            engine: The engine, with the pgvector codec registered.
        """
        self._engine = engine
        self._committed = False
        self.session = AsyncSession(engine, expire_on_commit=False)
        self.coordination = CoordinationBuffer()
        # Bound here so an instance that was never entered already satisfies the protocol check.
        # The transaction begins in `__aenter__`.
        self.pages = Pages(self.session)
        self.sections = Sections(self.session)
        self.claims = Claims(self.session)
        self.claim_qualifiers = ClaimQualifiers(self.session)
        self.edges = Edges(self.session)
        self.sources = SourceRepository(self.session)
        self.vocab = VocabEntries(self.session)
        self.summaries = UnitSummaries(self.session)
        self.meta = EmbedMeta(self.session)
        self.manifest = ManifestEntries(self.session)
        self.link_refs = LinkRefs(self.session)
        self.validation_store = ValidationIssues(self.session)
        self.fingerprint = IndexFingerprints(self.session)
        self.page_type_contracts = PageTypeContracts(self.session)
        self.signals = SignalsQueries(self.session)

    async def __aenter__(self) -> Self:
        """Begin the rebuild transaction."""
        self._committed = False
        await self.session.begin()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the scope: roll back unless `commit` ran, then release the session."""
        del exc
        try:
            if not self._committed:
                await self.session.rollback()
                self.coordination.clear()
        finally:
            await self.session.close()

    async def commit(self) -> None:
        """Persist the entire rebuild transaction, with the events and deliveries recorded on it."""
        await flush(self.session, self.coordination)
        await self.session.commit()
        self._committed = True

    def record(self, *events: Event) -> None:
        """Record events to be written to the outbox in this transaction.

        Args:
            *events: The events to record.
        """
        self.coordination.record(*events)

    def record_delivery(self, event_id: UUID, subscriber: str) -> None:
        """Record a handled delivery to be written in this transaction.

        Args:
            event_id: The id of the delivered event.
            subscriber: The subscriber that handled it.
        """
        self.coordination.record_delivery(event_id, subscriber)
