"""The public names a host composes an agent from.

The note grammar (`KnowledgeBaseSchema`, `AuthoringPayload`), the embedded factories (`embedded_*`, `KnowledgeBaseConnection`), the capabilities they
yield (`RetrievalKnowledgeBase`, `SqlKnowledgeBase`), and the supporting factories. Everything else in the package is reachable by submodule, but only
these names are supported. Each name is imported on first attribute access, so importing a single submodule (`engram.kb`, `engram.config`) costs only
that submodule's own dependencies.
"""

from typing import TYPE_CHECKING

from core.lazy_imports import lazy_module_attributes

if TYPE_CHECKING:
    from engram.agent_memory.contracts import CATEGORIES, EngramMemoryStore, agent_memory_schema, embedded_memory_store
    from engram.kb.contracts import AuthoringPayload, KnowledgeBaseSchema, KnowledgeBaseSchemaMissingError, load_knowledge_base_schema
    from engram.retrieval.contracts import (
        Earned,
        KnowledgeBaseConnection,
        ReadDials,
        RetrievalKnowledgeBase,
        SqlKnowledgeBase,
        document_embedder,
        embedded_authoring,
        embedded_knowledge_base,
        embedded_read_tools,
        embedded_retrieval,
        policy_instructions,
    )

__all__ = [
    "CATEGORIES",
    "AuthoringPayload",
    "Earned",
    "EngramMemoryStore",
    "KnowledgeBaseConnection",
    "KnowledgeBaseSchema",
    "KnowledgeBaseSchemaMissingError",
    "ReadDials",
    "RetrievalKnowledgeBase",
    "SqlKnowledgeBase",
    "agent_memory_schema",
    "document_embedder",
    "embedded_authoring",
    "embedded_knowledge_base",
    "embedded_memory_store",
    "embedded_read_tools",
    "embedded_retrieval",
    "load_knowledge_base_schema",
    "policy_instructions",
]

_LAZY_IMPORTS = {
    "AuthoringPayload": "engram.kb.contracts",
    "KnowledgeBaseSchema": "engram.kb.contracts",
    "KnowledgeBaseSchemaMissingError": "engram.kb.contracts",
    "load_knowledge_base_schema": "engram.kb.contracts",
    "ReadDials": "engram.retrieval.contracts",
    "RetrievalKnowledgeBase": "engram.retrieval.contracts",
    "SqlKnowledgeBase": "engram.retrieval.contracts",
    "Earned": "engram.retrieval.contracts",
    "KnowledgeBaseConnection": "engram.retrieval.contracts",
    "document_embedder": "engram.retrieval.contracts",
    "embedded_knowledge_base": "engram.retrieval.contracts",
    "embedded_authoring": "engram.retrieval.contracts",
    "embedded_read_tools": "engram.retrieval.contracts",
    "embedded_retrieval": "engram.retrieval.contracts",
    "policy_instructions": "engram.retrieval.contracts",
    "EngramMemoryStore": "engram.agent_memory.contracts",
    "embedded_memory_store": "engram.agent_memory.contracts",
    "agent_memory_schema": "engram.agent_memory.contracts",
    "CATEGORIES": "engram.agent_memory.contracts",
}


__getattr__, __dir__ = lazy_module_attributes(__name__, _LAZY_IMPORTS, __all__)
