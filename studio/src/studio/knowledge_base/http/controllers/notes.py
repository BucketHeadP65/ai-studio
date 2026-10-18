"""Keeping a value one workflow run produced as a note in a knowledge base.

Two routes, and only the second one writes. `preview` says what the value would become in the
destination the caller names, and writes nothing. `notes` keeps exactly that preview, and writes
only into a name nothing holds.

Neither route takes an address for the value. The caller names it by the reference its own host
minted, and this app resolves that reference at the one harness it was configured with, so a
request chooses what to keep and never where to read it from.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from wireup import Injected
from wireup.integration.fastapi import WireupRoute

from studio.knowledge_base.http.api_models import error_response
from studio.knowledge_base.retention.contracts import (
    NotePreview,
    NotePreviewRequest,
    NoteRetention,
    RetainedNote,
    RetainNoteRequest,
    RetentionRefusal,
    RetentionRefusedError,
)

REFUSAL_STATUS: dict[RetentionRefusal, int] = {
    RetentionRefusal.UNKNOWN_KNOWLEDGE_BASE: 404,
    RetentionRefusal.UNREADABLE_SCHEMA: 409,
    RetentionRefusal.ORIGIN_UNAVAILABLE: 409,
    RetentionRefusal.NOT_A_NOTE_DRAFT: 422,
    RetentionRefusal.UNKNOWN_PAGE_TYPE: 400,
    RetentionRefusal.DRAFT_REFUSED: 422,
    RetentionRefusal.SCHEMA_CHANGED: 409,
    RetentionRefusal.PREVIEW_CHANGED: 409,
    RetentionRefusal.KEY_REUSED: 409,
    RetentionRefusal.UNREADABLE_RECEIPT: 409,
    RetentionRefusal.TARGET_UNWRITABLE: 500,
}
"""How each refusal reads over HTTP. Exhaustive over the reasons, so a new reason is a new entry here."""

CHANGED_STATUS = 409
CHANGED_ERROR = "target_changed"
"""A note whose file now holds something else. The file is untouched and the caller decides what to do.

It answers in the uniform `{error, detail}` shape like every other refusal, and carries the whole
outcome alongside, so a caller reads one sentence or the full report as it prefers.
"""


def refused_response(error: RetentionRefusedError) -> JSONResponse:
    """One retention refusal as the uniform `{error, detail}` body, at the status its reason reads as.

    Args:
        error: The refusal the retention service raised.

    Returns:
        The response.
    """
    return error_response(REFUSAL_STATUS[error.refusal], error.refusal.value, str(error))


class NotesController:
    """Previewing and keeping what a run produced, as a note in one registered knowledge base."""

    router = APIRouter(prefix="/api/knowledge-bases", tags=["notes"], route_class=WireupRoute)

    @router.post("/{name}/notes/preview", response_model=None, summary="Preview a note from a run's value", operation_id="previewNote")
    async def preview_note(self, name: str, body: NotePreviewRequest, retention: Injected[NoteRetention]) -> NotePreview | JSONResponse:
        """What the value `origin` addresses would become as a note in `name`, with nothing written.

        The value is resolved at this app's own configured harness and read as a proposed note,
        checked against `name`'s current schema, and rendered. The answer carries the normalized
        draft, the Markdown, where it would land, and the two digests a later keep must carry back:
        the schema's, and the preview's own.

        A name nothing registers is **404**. A value the harness will not answer for is **409**. A
        value that is not a proposed note, or a draft this knowledge base would not accept, is
        **422**. A page type the schema does not declare is **400**.
        """
        try:
            return await retention.preview(name, body.origin, body.edited_draft)
        except RetentionRefusedError as error:
            return refused_response(error)

    @router.post("/{name}/notes", response_model=None, summary="Keep a previewed note", operation_id="retainNote")
    async def retain_note(self, name: str, body: RetainNoteRequest, retention: Injected[NoteRetention]) -> RetainedNote | JSONResponse:
        """Keep exactly the note one preview showed, and index it.

        Every check the preview ran is run again against the current schema and the current value,
        and both digests must still come out the same, so a note is only ever written as it was
        read. The note is created at a name nothing holds. A file already there holding these exact
        bytes is this same note, already kept, and is left alone. A file holding anything else is
        left alone too, and the answer says so.

        `outcome` carries what happened: `saved`, `saved_not_indexed` (written, not indexed, and a
        retry of this same request indexes it without writing), `already_indexed` (nothing to do),
        or `changed` at **409** (the file holds something else and was not touched). A schema or a
        preview that has moved on, a request key already used for other content, and a value the
        harness will not answer for are each **409**.
        """
        try:
            kept = await retention.retain(name, body)
        except RetentionRefusedError as error:
            return refused_response(error)
        if kept.outcome == "changed":
            return error_response(CHANGED_STATUS, CHANGED_ERROR, kept.detail, extra=kept.model_dump(mode="json", exclude={"detail"}))
        return kept
