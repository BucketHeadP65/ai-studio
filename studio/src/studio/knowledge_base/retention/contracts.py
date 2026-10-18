"""What code outside the retention area may use: the service, the note route bodies, and the refusal it raises."""

from __future__ import annotations

from studio.knowledge_base.retention.api_models import NotePreview, NotePreviewRequest, RetainedNote, RetainNoteRequest
from studio.knowledge_base.retention.domain import RetentionRefusal
from studio.knowledge_base.retention.exceptions import RetentionRefusedError
from studio.knowledge_base.retention.service import NoteRetention

__all__ = [
    "NotePreview",
    "NotePreviewRequest",
    "NoteRetention",
    "RetainNoteRequest",
    "RetainedNote",
    "RetentionRefusal",
    "RetentionRefusedError",
]
