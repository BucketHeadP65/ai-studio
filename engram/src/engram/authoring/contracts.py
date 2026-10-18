"""The public surface of the authoring slice."""

from engram.authoring.domain import (
    ASSIGNED_FRONTMATTER,
    REINDEX_FINDINGS_SHOWN,
    AuthoringKnowledgeBase,
    CheckedNote,
    EntityName,
    KnownEntities,
    NoteDraft,
    NoteSection,
    ReindexFinding,
    ReindexResult,
    collection_of,
    reindex_result_from_subprocess,
)
from engram.authoring.exceptions import NoteRefusedError
from engram.authoring.infrastructure.entity_pages import EntityPages
from engram.authoring.infrastructure.note_writer import NoteWriter
from engram.authoring.infrastructure.registered_knowledge_bases import doc_authoring_knowledge_bases
from engram.authoring.internal_contracts import Reindex
from engram.authoring.tools import AuthoringTools

__all__ = [
    "ASSIGNED_FRONTMATTER",
    "REINDEX_FINDINGS_SHOWN",
    "AuthoringKnowledgeBase",
    "AuthoringTools",
    "CheckedNote",
    "EntityName",
    "EntityPages",
    "KnownEntities",
    "NoteDraft",
    "NoteRefusedError",
    "NoteSection",
    "NoteWriter",
    "Reindex",
    "ReindexFinding",
    "ReindexResult",
    "collection_of",
    "doc_authoring_knowledge_bases",
    "reindex_result_from_subprocess",
]
