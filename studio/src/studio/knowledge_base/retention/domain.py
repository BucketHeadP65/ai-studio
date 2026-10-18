"""The values retention decides with: how far a request has got, what it came to, why it was refused, and its receipt."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from core.ai.artifacts import ArtifactRef, artifact_digest
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from engram.kb.contracts import KnowledgeBaseSchema

NOTE_ID_HEX = 24
"""How many hex characters of the origin's digest a minted note id carries.

It is fixed, so the id a preview shows is the id retention writes.
"""


class RetentionState(StrEnum):
    """How far one retention request has got, as its receipt records it."""

    PREPARED = "prepared"
    """The bytes and the target are settled and nothing is written yet."""
    SAVED = "saved"
    """The file is published. Whether it is indexed is a separate question."""
    INDEXED = "indexed"
    """The published file was indexed, and still matched the preview when that was recorded."""


type RetentionOutcome = Literal["saved", "saved_not_indexed", "already_indexed", "changed"]
"""What one retention request came to.

`saved` published the note, or met its own earlier publication, and indexed it.
`saved_not_indexed` published it and the index did not run, so a retry of the same request indexes and writes nothing.
`already_indexed` found the note published, indexed and unchanged, and did nothing.
`changed` found the target holding something else, which is left untouched.
"""


class RetentionRefusal(StrEnum):
    """Why a preview or a retention will not happen, as the HTTP answer names it."""

    UNKNOWN_KNOWLEDGE_BASE = "unknown_knowledge_base"
    UNREADABLE_SCHEMA = "unreadable_schema"
    ORIGIN_UNAVAILABLE = "origin_unavailable"
    NOT_A_NOTE_DRAFT = "not_a_note_draft"
    UNKNOWN_PAGE_TYPE = "unknown_page_type"
    DRAFT_REFUSED = "draft_refused"
    SCHEMA_CHANGED = "schema_changed"
    PREVIEW_CHANGED = "preview_changed"
    KEY_REUSED = "key_reused"
    UNREADABLE_RECEIPT = "unreadable_receipt"
    TARGET_UNWRITABLE = "target_unwritable"


class RetentionReceipt(BaseModel):
    """What one retention request settled on, kept from before publication until after indexing.

    It holds what a later attempt needs to tell its own note from somebody else's file: the agreed bytes, the target and the origin.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency_key: str
    """The key the request named itself with."""
    state: RetentionState
    knowledge_base: str
    """The registry name of the destination knowledge base."""
    note_id: str
    target: str
    """Where the note goes, relative to the knowledge base root, in posix spelling."""
    origin: ArtifactRef
    schema_digest: str
    preview_digest: str
    content_digest: str
    """The SHA-256 of the bytes the request publishes."""
    content_bytes: int
    """How many bytes the request publishes."""
    read_ref: str | None = None
    """How the published note is addressed once it is indexed. Absent until it is."""


def schema_digest_of(schema: KnowledgeBaseSchema) -> str:
    """The digest of one knowledge base's whole schema, as a preview and a retention both compute it.

    Args:
        schema: The knowledge base's schema.

    Returns:
        The digest.
    """
    return artifact_digest(schema.model_dump(mode="json"))
