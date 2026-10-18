"""The public names of the read side: the read tools, the SQL gateway, the queries and the embedded openers."""

from __future__ import annotations

from engram.retrieval.capability import ReadSurface, RetrievalKnowledgeBase, SqlKnowledgeBase
from engram.retrieval.decisions import DecidedPage, decide_page, list_decisions, page_type_paths
from engram.retrieval.dials import ReadDials
from engram.retrieval.domain import Graph, Search
from engram.retrieval.earned import Earned
from engram.retrieval.envelope import EnvelopeHandler, parse_ref, qualify_ref
from engram.retrieval.exceptions import BadRefError, ReaderRoleOverprivilegedError
from engram.retrieval.graph_backend import GraphBackends, build_graph_backends
from engram.retrieval.infrastructure.embedded import (
    IngestDocument,
    KnowledgeBaseConnection,
    document_embedder,
    embedded_authoring,
    embedded_knowledge_base,
    embedded_read_tools,
    embedded_retrieval,
    policy_instructions,
    switch_source,
)
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway, verify_reader_role
from engram.retrieval.infrastructure.reader_roles import PrivateReaders, SqlReaderScopes, private_reader_url
from engram.retrieval.queries import (
    DescribeSchemaHandler,
    FetchHandler,
    GraphExpandHandler,
    GraphHandler,
    GraphPathsHandler,
    KnowledgeBaseInfo,
    KnowledgeBasesHandler,
    KnowledgeBaseValidationHandler,
    LookupValueHandler,
    SearchHandler,
    StatusHandler,
    ValidationRow,
    graph_next_steps,
    halfvec_literal,
    paths_next_steps,
)
from engram.retrieval.reader_roles import private_reader_role
from engram.retrieval.rerank import RerankedSearch, Reranking, reranking
from engram.retrieval.search_dialect import SearchDialects, build_search_dialects
from engram.retrieval.tools import (
    FetchEnvelope,
    build_retrieval_toolset,
    envelope_next_steps,
    fetch_opens_map,
    search_next_steps,
    search_result_status,
)

__all__ = [
    "BadRefError",
    "DecidedPage",
    "DescribeSchemaHandler",
    "Earned",
    "EnvelopeHandler",
    "FetchEnvelope",
    "FetchHandler",
    "Graph",
    "GraphBackends",
    "GraphExpandHandler",
    "GraphHandler",
    "GraphPathsHandler",
    "IngestDocument",
    "KnowledgeBaseConnection",
    "KnowledgeBaseInfo",
    "KnowledgeBaseValidationHandler",
    "KnowledgeBasesHandler",
    "LookupValueHandler",
    "PrivateReaders",
    "ReadDials",
    "ReadOnlySqlGateway",
    "ReadSurface",
    "ReaderRoleOverprivilegedError",
    "RerankedSearch",
    "Reranking",
    "RetrievalKnowledgeBase",
    "Search",
    "SearchDialects",
    "SearchHandler",
    "SqlKnowledgeBase",
    "SqlReaderScopes",
    "StatusHandler",
    "ValidationRow",
    "build_graph_backends",
    "build_retrieval_toolset",
    "build_search_dialects",
    "decide_page",
    "document_embedder",
    "embedded_authoring",
    "embedded_knowledge_base",
    "embedded_read_tools",
    "embedded_retrieval",
    "envelope_next_steps",
    "fetch_opens_map",
    "graph_next_steps",
    "halfvec_literal",
    "list_decisions",
    "page_type_paths",
    "parse_ref",
    "paths_next_steps",
    "policy_instructions",
    "private_reader_role",
    "private_reader_url",
    "qualify_ref",
    "reranking",
    "search_next_steps",
    "search_result_status",
    "switch_source",
    "verify_reader_role",
]
