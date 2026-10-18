"""The public names of the indexing slice: its commands, results, ports, use cases and the adapters callers build."""

from __future__ import annotations

from engram.indexing.domain.card import content_hash, token_count
from engram.indexing.domain.manifest import ManifestEntry
from engram.indexing.domain.mirror_chunks import (
    DEFAULT_CHUNK_SIZES,
    DEFAULT_PLANNER_CEILING_TOKENS,
    PARAGRAPH_NUMBERING_VERSION,
    ChunkSizes,
    cut_plan_identity,
)
from engram.indexing.domain.models import (
    CitingPagesChanged,
    DryRunPreview,
    IndexKnowledgeBase,
    IndexReport,
    IngestPage,
    IngestResult,
    MirrorDoc,
    PagesLoaded,
    PartitionPreview,
    RawDocument,
    ResolvedEdge,
    ResolvedNote,
    Severity,
    ValidationIssue,
    ValidationReport,
)
from engram.indexing.domain.resolver import resolve, section_loc
from engram.indexing.domain.summary_units import SummaryUnit, SummaryUpsertRow
from engram.indexing.domain.switches import RetrievalSwitches, Signals
from engram.indexing.domain.validator import validate_note
from engram.indexing.exceptions import CollectionIsForeignError, CollectionIsIndexedError, IngestValidationError, ProposalUnknownError
from engram.indexing.infrastructure.embedder import PydanticAiEmbedder
from engram.indexing.infrastructure.knowledge_base_reader import FilesystemKnowledgeBaseReader
from engram.indexing.infrastructure.orm import (
    ClaimORM,
    EdgeORM,
    IndexFingerprintORM,
    IndexManifestORM,
    LinkRefORM,
    PageORM,
    SectionORM,
    register_pgvector,
)
from engram.indexing.infrastructure.summary_governance import SummaryGovernance
from engram.indexing.infrastructure.uow import SqlAlchemyKnowledgeBaseUnitOfWork
from engram.indexing.infrastructure.vocab_governance import VocabGovernance
from engram.indexing.internal_contracts import (
    ChunkPlanner,
    CutPlanQueries,
    CutPlans,
    IndexUnitOfWork,
    KnowledgeBaseReader,
    ReaderScopes,
    Summarizer,
    ValidationReportSink,
)
from engram.indexing.read_models import PageKeyRowResult, PendingEmbedChunk, PendingEmbedRow, PendingSummary, StoredFingerprint
from engram.indexing.service.cut_plan_stage import CutPlanStage
from engram.indexing.service.index import INDEXER_VERSION, IndexKnowledgeBaseHandler, indexer_version
from engram.indexing.service.ingest import IngestPageHandler
from engram.indexing.service.summary_stage import SummaryStage
from engram.indexing.service.vocab import load_vocab

__all__ = [
    "DEFAULT_CHUNK_SIZES",
    "DEFAULT_PLANNER_CEILING_TOKENS",
    "INDEXER_VERSION",
    "PARAGRAPH_NUMBERING_VERSION",
    "ChunkPlanner",
    "ChunkSizes",
    "CitingPagesChanged",
    "ClaimORM",
    "CollectionIsForeignError",
    "CollectionIsIndexedError",
    "CutPlanQueries",
    "CutPlanStage",
    "CutPlans",
    "DryRunPreview",
    "EdgeORM",
    "FilesystemKnowledgeBaseReader",
    "IndexFingerprintORM",
    "IndexKnowledgeBase",
    "IndexKnowledgeBaseHandler",
    "IndexManifestORM",
    "IndexReport",
    "IndexUnitOfWork",
    "IngestPage",
    "IngestPageHandler",
    "IngestResult",
    "IngestValidationError",
    "KnowledgeBaseReader",
    "LinkRefORM",
    "ManifestEntry",
    "MirrorDoc",
    "PageKeyRowResult",
    "PageORM",
    "PagesLoaded",
    "PartitionPreview",
    "PendingEmbedChunk",
    "PendingEmbedRow",
    "PendingSummary",
    "ProposalUnknownError",
    "PydanticAiEmbedder",
    "RawDocument",
    "ReaderScopes",
    "ResolvedEdge",
    "ResolvedNote",
    "RetrievalSwitches",
    "SectionORM",
    "Severity",
    "Signals",
    "SqlAlchemyKnowledgeBaseUnitOfWork",
    "StoredFingerprint",
    "Summarizer",
    "SummaryGovernance",
    "SummaryStage",
    "SummaryUnit",
    "SummaryUpsertRow",
    "ValidationIssue",
    "ValidationReport",
    "ValidationReportSink",
    "VocabGovernance",
    "content_hash",
    "cut_plan_identity",
    "indexer_version",
    "load_vocab",
    "register_pgvector",
    "resolve",
    "section_loc",
    "token_count",
    "validate_note",
]
