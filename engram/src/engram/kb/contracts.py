"""The public names of the knowledge base slice: the note grammar, the knowledge base schema and their files."""

from __future__ import annotations

from engram.kb.domain.note import (
    BaseFrontmatter,
    ClaimBlock,
    KnowledgeBaseNote,
    RelationSpec,
    Section,
    UnitLink,
    VocabularyIndex,
    parse_frontmatter,
    unit_links_in,
)
from engram.kb.domain.note_threads import (
    NOTES_DIR,
    NOTES_SUFFIX,
    Note,
    NoteAnchor,
    NoteSheet,
    Reply,
    add_reply,
    open_threads,
    set_status,
    sidecar_for,
)
from engram.kb.domain.parser import parse
from engram.kb.domain.payload import AuthoringPayload, payload_json_schema
from engram.kb.domain.renderer import UnreadClaim, claims_not_read_back, payload_of, render
from engram.kb.domain.retrieval_policy import ApplyRetrievalPolicy, RetrievalPolicyReport
from engram.kb.domain.schema import (
    CanonicalSection,
    ClaimGrammarSpec,
    DerivationEntry,
    FacetSpec,
    FrontmatterFieldSpec,
    KnowledgeBaseSchema,
    LinkClass,
    NotesSelector,
    PagesSelector,
    PageTypeSpec,
    RelationDecl,
    SectionSummarySpec,
    SourceQueuePolicy,
    SourcesSelector,
    SourcesSpec,
    VocabEntry,
    resolve_schema,
)
from engram.kb.domain.verify import quotes_in_mirror
from engram.kb.domain.vocabulary import facet_choices_drift, parse_vocabulary, schema_vocabulary_drift
from engram.kb.exceptions import KnowledgeBaseSchemaMissingError, NoteGrammarError
from engram.kb.infrastructure.indexed_schema import load_indexed_schema
from engram.kb.infrastructure.note_sidecars import load_sheet, notes_home, save_sheet
from engram.kb.infrastructure.policy_page import DEFAULT_GUIDANCE, read_policy, render_policy, write_policy
from engram.kb.infrastructure.retrieval_policy_files import ACTIVE_POLICY_PATH, apply_retrieval_policy
from engram.kb.infrastructure.schema_file import load_knowledge_base_schema
from engram.kb.internal_contracts import IndexedSchemaQueries
from engram.kb.service import ApplyRetrievalPolicyHandler

__all__ = [
    "ACTIVE_POLICY_PATH",
    "DEFAULT_GUIDANCE",
    "NOTES_DIR",
    "NOTES_SUFFIX",
    "ApplyRetrievalPolicy",
    "ApplyRetrievalPolicyHandler",
    "AuthoringPayload",
    "BaseFrontmatter",
    "CanonicalSection",
    "ClaimBlock",
    "ClaimGrammarSpec",
    "DerivationEntry",
    "FacetSpec",
    "FrontmatterFieldSpec",
    "IndexedSchemaQueries",
    "KnowledgeBaseNote",
    "KnowledgeBaseSchema",
    "KnowledgeBaseSchemaMissingError",
    "LinkClass",
    "Note",
    "NoteAnchor",
    "NoteGrammarError",
    "NoteSheet",
    "NotesSelector",
    "PageTypeSpec",
    "PagesSelector",
    "RelationDecl",
    "RelationSpec",
    "Reply",
    "RetrievalPolicyReport",
    "Section",
    "SectionSummarySpec",
    "SourceQueuePolicy",
    "SourcesSelector",
    "SourcesSpec",
    "UnitLink",
    "UnreadClaim",
    "VocabEntry",
    "VocabularyIndex",
    "add_reply",
    "apply_retrieval_policy",
    "claims_not_read_back",
    "facet_choices_drift",
    "load_indexed_schema",
    "load_knowledge_base_schema",
    "load_sheet",
    "notes_home",
    "open_threads",
    "parse",
    "parse_frontmatter",
    "parse_vocabulary",
    "payload_json_schema",
    "payload_of",
    "quotes_in_mirror",
    "read_policy",
    "render",
    "render_policy",
    "resolve_schema",
    "save_sheet",
    "schema_vocabulary_drift",
    "set_status",
    "sidecar_for",
    "unit_links_in",
    "write_policy",
]
