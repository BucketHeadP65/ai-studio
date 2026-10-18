"""The request and response bodies of the two note routes, preview and retain."""

from __future__ import annotations

from core.ai.artifacts import ArtifactRef
from engram.authoring.contracts import NoteDraft
from pydantic import BaseModel, ConfigDict, Field

from studio.knowledge_base.retention.domain import RetentionOutcome


class NotePreviewRequest(BaseModel):
    """Ask what one value would become as a note, without keeping it."""

    model_config = ConfigDict(frozen=True, extra="forbid", use_attribute_docstrings=True)

    origin: ArtifactRef
    """The reference the run's own host minted, never a path and never a URL."""
    edited_draft: NoteDraft | None = None
    """The whole draft as the person now has it, when they have changed anything. It replaces the proposal whole."""


class RetainNoteRequest(BaseModel):
    """Ask to keep exactly the note one preview showed."""

    model_config = ConfigDict(frozen=True, extra="forbid", use_attribute_docstrings=True)

    idempotency_key: str = Field(min_length=1, max_length=200)
    """Names this attempt, 1 to 200 characters. A retry with the same key meets its own earlier outcome."""
    origin: ArtifactRef
    """The reference the preview was made from."""
    draft: NoteDraft
    """The draft as the preview normalized it."""
    schema_digest: str
    """The preview's schema digest, carried back unchanged."""
    preview_digest: str
    """The preview's own digest, carried back unchanged."""


class NotePreview(BaseModel):
    """What a destination would make of one proposed note, with nothing written.

    `preview_digest` covers the target, the schema, the origin, the normalized draft and the rendered bytes.
    Retention refuses when anything under it has moved.
    """

    model_config = ConfigDict(frozen=True)

    knowledge_base: str
    note_id: str
    draft: NoteDraft
    markdown: str
    path: str
    """Where the note would land, relative to the knowledge base root, in posix spelling."""
    read_ref: str
    """How the note would be addressed once indexed."""
    schema_digest: str
    preview_digest: str
    warnings: tuple[str, ...] = ()


class RetainedNote(BaseModel):
    """What became of one retention request: the note, where it is, and whether it is indexed."""

    model_config = ConfigDict(frozen=True)

    outcome: RetentionOutcome
    knowledge_base: str
    note_id: str
    path: str
    read_ref: str
    detail: str = ""
    warnings: tuple[str, ...] = ()
