"""Keeping a value a run produced as a note: a preview that writes nothing, and a retain that publishes it.

A preview resolves the value, reads it as a proposed note, checks it against the knowledge base's schema and renders it.
Retaining repeats every check against the confirmed draft and publishes the bytes only at a name nothing holds.
It never replaces a file. A target holding anything else is left as it is and reported as `changed`.
A receipt is written before publication, so a retry after a crash meets its own earlier outcome.
"""

from __future__ import annotations

import asyncio
import hashlib

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from core.ai.artifacts import ArtifactRef, ArtifactUnavailableError, artifact_digest
from core.observability import get_logger
from engram.authoring.contracts import AuthoringKnowledgeBase, CheckedNote, NoteDraft, NoteRefusedError, NoteWriter
from pydantic import ValidationError

from studio.knowledge_base.retention.api_models import NotePreview, RetainedNote
from studio.knowledge_base.retention.domain import NOTE_ID_HEX, RetentionReceipt, RetentionRefusal, RetentionState, schema_digest_of
from studio.knowledge_base.retention.exceptions import RetentionRefusedError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from core.ai.artifacts import ArtifactDocument, ArtifactResolver
    from engram.authoring.contracts import Reindex
    from pydantic import JsonValue

    from studio.knowledge_base.retention.api_models import RetainNoteRequest
    from studio.knowledge_base.retention.internal_contracts import Receipts

_logger = get_logger(__name__)


@dataclass(frozen=True)
class _Prepared:
    """One request checked through to its exact bytes, with nothing written yet."""

    knowledge_base: AuthoringKnowledgeBase
    checked: CheckedNote
    draft: NoteDraft
    origin: ArtifactRef
    schema_digest: str
    preview_digest: str

    @property
    def content(self) -> bytes:
        """The exact bytes this request would publish."""
        return self.checked.rendered.encode("utf-8")

    @property
    def content_digest(self) -> str:
        """The SHA-256 of those exact bytes."""
        return hashlib.sha256(self.content).hexdigest()

    @property
    def target(self) -> str:
        """Where the note lands, relative to the knowledge base root, in posix spelling."""
        return self.checked.path.relative_to(self.knowledge_base.path).as_posix()

    @property
    def read_ref(self) -> str:
        """How the note is addressed once it is indexed."""
        return f"page:{self.checked.id}"

    def published(self) -> bytes | None:
        """What sits at the target right now, `None` when the name is free."""
        try:
            return self.checked.path.read_bytes()
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            return None
        except OSError as error:
            msg = f"the file at {self.target} could not be read: {error}"
            raise RetentionRefusedError(RetentionRefusal.TARGET_UNWRITABLE, msg) from error

    def still_ours(self) -> bool:
        """Whether the target still holds exactly the bytes this request agreed to."""
        held = self.published()
        return held is not None and hashlib.sha256(held).hexdigest() == self.content_digest


class NoteRetention:
    """Previews and retains one proposed note in one registered knowledge base.

    Two retains of the same note in this process run one after the other.
    """

    def __init__(
        self,
        resolver: ArtifactResolver,
        knowledge_bases: Callable[[str], AuthoringKnowledgeBase],
        reindex: Reindex,
        receipts_at: Callable[[Path], Receipts],
        *,
        today: Callable[[], date] = lambda: datetime.now(tz=UTC).date(),
    ) -> None:
        """Hold where values are resolved, how a destination is looked up, how it is indexed, where receipts are kept, and what day it is.

        Args:
            resolver: Resolves the reference a request names into the value it addresses.
            knowledge_bases: The registered knowledge base one name means. It raises `RetentionRefusedError`
                for a name nothing registers or a schema that will not load.
            reindex: Makes a published file an indexed page.
            receipts_at: The receipts kept under one knowledge base's root.
            today: The date stamped as `created` onto a draft that carries none.
        """
        self._resolver = resolver
        self._knowledge_bases = knowledge_bases
        self._reindex = reindex
        self._receipts_at = receipts_at
        self._today = today
        self._locks: dict[str, asyncio.Lock] = {}

    async def preview(self, name: str, origin: ArtifactRef, edited_draft: NoteDraft | None = None) -> NotePreview:
        """What `origin` would become as a note in the knowledge base `name`, with nothing written.

        Args:
            name: The registry name of the destination knowledge base.
            origin: The reference to the value.
            edited_draft: The person's whole draft, which replaces the proposal and is never merged with it.

        Returns:
            The normalized draft, its Markdown, where it would land, and the two digests a retain must carry back.

        Raises:
            RetentionRefusedError: The destination, the value or the draft will not do.
        """
        prepared = await self._prepare(name, origin, edited_draft)
        return NotePreview(
            knowledge_base=name,
            note_id=prepared.checked.id,
            draft=prepared.draft,
            markdown=prepared.checked.rendered,
            path=prepared.target,
            read_ref=prepared.read_ref,
            schema_digest=prepared.schema_digest,
            preview_digest=prepared.preview_digest,
            warnings=prepared.checked.warnings,
        )

    async def retain(self, name: str, request: RetainNoteRequest) -> RetainedNote:
        """Publish exactly the preview the person confirmed, and index it.

        Every check the preview ran is run again, and both digests must still come out the same.
        Only then is anything written, and only at a name nothing holds.

        Args:
            name: The registry name of the destination knowledge base.
            request: The confirmed preview and the key that names this attempt.

        Returns:
            What happened, as `saved`, `saved_not_indexed`, `already_indexed` or `changed`.

        Raises:
            RetentionRefusedError: The destination, the value, the draft, the schema, the preview or the
                key will not do. Nothing is written for any of them.
        """
        prepared = await self._prepare(name, request.origin, request.draft)
        if prepared.schema_digest != request.schema_digest:
            msg = "this knowledge base's schema has changed since the preview, so preview it again before keeping it"
            raise RetentionRefusedError(RetentionRefusal.SCHEMA_CHANGED, msg)
        if prepared.preview_digest != request.preview_digest:
            msg = "this is not the note that was previewed, so preview it again before keeping it"
            raise RetentionRefusedError(RetentionRefusal.PREVIEW_CHANGED, msg)
        async with self._lock_for(name, prepared.target):
            return await self._publish(name, request.idempotency_key, prepared)

    async def _prepare(self, name: str, origin: ArtifactRef, draft: NoteDraft | None) -> _Prepared:
        """Resolve, read, check and render one proposal, resolving the origin even when the person supplied a draft."""
        knowledge_base = self._knowledge_bases(name)
        resolved = _draft_of(await self._resolve(origin))
        proposed = draft if draft is not None else resolved
        page_type = knowledge_base.schema.page_type(proposed.page_type)
        if page_type is None:
            declared = ", ".join(page_type_schema.page_type for page_type_schema in knowledge_base.schema.page_types)
            msg = f"{name!r} declares no page type {proposed.page_type!r}. It declares {declared}"
            raise RetentionRefusedError(RetentionRefusal.UNKNOWN_PAGE_TYPE, msg)
        normalized = self._stamped(proposed)
        note_id = f"{page_type.id_prefix}{artifact_digest(_origin_identity(origin))[:NOTE_ID_HEX]}"
        try:
            checked = NoteWriter(knowledge_base).check(normalized.payload(note_id))
        except NoteRefusedError as error:
            msg = f"this draft is not a page {name!r} accepts: {error}"
            raise RetentionRefusedError(RetentionRefusal.DRAFT_REFUSED, msg) from error
        digest = schema_digest_of(knowledge_base.schema)
        return _Prepared(
            knowledge_base=knowledge_base,
            checked=checked,
            draft=normalized,
            origin=origin,
            schema_digest=digest,
            preview_digest=_preview_digest(name, digest, origin, normalized, checked.rendered),
        )

    async def _resolve(self, origin: ArtifactRef) -> ArtifactDocument:
        """The value `origin` addresses, as its own host answers for it."""
        try:
            return await self._resolver.resolve(origin)
        except ArtifactUnavailableError as error:
            raise RetentionRefusedError(RetentionRefusal.ORIGIN_UNAVAILABLE, str(error)) from error

    def _stamped(self, draft: NoteDraft) -> NoteDraft:
        """`draft` carrying the day it was written, so its bytes do not change at midnight."""
        if draft.frontmatter.get("created"):
            return draft
        return draft.model_copy(update={"frontmatter": {**dict(draft.frontmatter), "created": self._today().isoformat()}})

    def _lock_for(self, name: str, target: str) -> asyncio.Lock:
        """The lock that orders this process's own retains of one note, and nobody else's."""
        return self._locks.setdefault(f"{name}\0{target}", asyncio.Lock())

    async def _publish(self, name: str, idempotency_key: str, prepared: _Prepared) -> RetainedNote:
        """Record, publish, index and record again, in that order, under the note's own lock."""
        receipts = self._receipts_at(prepared.knowledge_base.path)
        standing = receipts.read(idempotency_key)
        _refuse_reused_key(standing, name, prepared)
        held = prepared.published()
        created = False
        if held is None:
            if standing is not None and standing.state is not RetentionState.PREPARED:
                return _gone(name, prepared)
            receipts.write(_receipt(idempotency_key, RetentionState.PREPARED, name, prepared))
            created = _create(prepared)
        if not prepared.still_ours():
            return _changed(name, prepared)
        if not created and standing is not None and standing.state is RetentionState.INDEXED:
            return RetainedNote(
                outcome="already_indexed",
                knowledge_base=name,
                note_id=prepared.checked.id,
                path=prepared.target,
                read_ref=prepared.read_ref,
                detail="this exact note was already kept and indexed, so nothing was written",
                warnings=prepared.checked.warnings,
            )
        receipts.write(_receipt(idempotency_key, RetentionState.SAVED, name, prepared))
        return await self._index(name, idempotency_key, prepared, receipts)

    async def _index(self, name: str, idempotency_key: str, prepared: _Prepared, receipts: Receipts) -> RetainedNote:
        """Index the published note, and record that only while the file still matches what was indexed."""
        result = await self._reindex(prepared.knowledge_base)
        if not prepared.still_ours():
            return _changed(name, prepared)
        if result.outcome == "not_indexed":
            return RetainedNote(
                outcome="saved_not_indexed",
                knowledge_base=name,
                note_id=prepared.checked.id,
                path=prepared.target,
                read_ref=prepared.read_ref,
                detail=result.detail,
                warnings=prepared.checked.warnings,
            )
        receipts.write(_receipt(idempotency_key, RetentionState.INDEXED, name, prepared, read_ref=prepared.read_ref))
        return RetainedNote(
            outcome="saved",
            knowledge_base=name,
            note_id=prepared.checked.id,
            path=prepared.target,
            read_ref=prepared.read_ref,
            detail=result.detail,
            warnings=prepared.checked.warnings,
        )


def _draft_of(document: ArtifactDocument) -> NoteDraft:
    """The proposed note `document` carries, refusing a value that is not a whole, valid `NoteDraft`."""
    try:
        return NoteDraft.model_validate(document.value)
    except ValidationError as error:
        msg = f"the value {document.ref.label!r} addresses is not a note this app can keep: {error}"
        raise RetentionRefusedError(RetentionRefusal.NOT_A_NOTE_DRAFT, msg) from error


def _origin_identity(origin: ArtifactRef) -> dict[str, JsonValue]:
    """Which value a reference addresses, without how it is labelled, so a relabelled value mints the same note id."""
    return {
        "run_id": origin.run_id,
        "node_id": origin.node_id,
        "turn": origin.turn,
        "id": origin.id,
        "value_digest": origin.value_digest,
    }


def _preview_digest(name: str, schema_digest: str, origin: ArtifactRef, draft: NoteDraft, markdown: str) -> str:
    """The digest covering one whole preview: where, against what, from what, as what, landing as what."""
    return artifact_digest(
        {
            "knowledge_base": name,
            "schema_digest": schema_digest,
            "origin": origin.model_dump(mode="json"),
            "draft": draft.model_dump(mode="json"),
            "markdown": markdown,
        }
    )


def _receipt(
    idempotency_key: str, state: RetentionState, name: str, prepared: _Prepared, *, read_ref: str | None = None
) -> RetentionReceipt:
    """One request's receipt at the state it has reached."""
    return RetentionReceipt(
        idempotency_key=idempotency_key,
        state=state,
        knowledge_base=name,
        note_id=prepared.checked.id,
        target=prepared.target,
        origin=prepared.origin,
        schema_digest=prepared.schema_digest,
        preview_digest=prepared.preview_digest,
        content_digest=prepared.content_digest,
        content_bytes=len(prepared.content),
        read_ref=read_ref,
    )


def _refuse_reused_key(standing: RetentionReceipt | None, name: str, prepared: _Prepared) -> None:
    """Refuse a key already on record for a different note, target, origin or set of bytes."""
    if standing is None:
        return
    same = (standing.knowledge_base, standing.note_id, standing.target, standing.content_digest, standing.origin) == (
        name,
        prepared.checked.id,
        prepared.target,
        prepared.content_digest,
        prepared.origin,
    )
    if not same:
        msg = "this request key was already used to keep different content, so use a new one"
        raise RetentionRefusedError(RetentionRefusal.KEY_REUSED, msg)


def _create(prepared: _Prepared) -> bool:
    """Publish the prepared bytes at a free name, answering whether this call is the one that wrote them."""
    try:
        NoteWriter(prepared.knowledge_base).create(prepared.checked)
    except FileExistsError:
        return False
    except OSError as error:
        msg = f"the note could not be written at {prepared.target}: {error}"
        raise RetentionRefusedError(RetentionRefusal.TARGET_UNWRITABLE, msg) from error
    return True


def _changed(name: str, prepared: _Prepared) -> RetainedNote:
    """The answer when the target holds something other than this note, whoever put it there."""
    _logger.info("note_retention_changed: {target} in {knowledge_base} holds other content, nothing was written", target=str(prepared.target), knowledge_base=name)
    return _not_this_note(
        name, prepared, "the file at this path holds something other than this note, and it was left exactly as it is"
    )


def _gone(name: str, prepared: _Prepared) -> RetainedNote:
    """The answer when this request already published its note and the file is no longer there, which is never written back."""
    _logger.info("note_retention_gone: {target} in {knowledge_base} was kept and has since been removed", target=str(prepared.target), knowledge_base=name)
    return _not_this_note(
        name, prepared, "this note was kept before and the file is no longer there, so it was not written again"
    )


def _not_this_note(name: str, prepared: _Prepared, detail: str) -> RetainedNote:
    """One answer for every way the target is not this note: nothing written, and why."""
    return RetainedNote(
        outcome="changed",
        knowledge_base=name,
        note_id=prepared.checked.id,
        path=prepared.target,
        read_ref=prepared.read_ref,
        detail=detail,
        warnings=prepared.checked.warnings,
    )
