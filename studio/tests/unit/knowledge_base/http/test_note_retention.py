"""Keeping a run's value as a note: the whole boundary, over the served routes.

Every refusal here is checked twice: the caller is told why, and the knowledge base on disk and
the harness's own call log are the same afterwards as they were before. A request that is refused
writes nothing, and a request that is refused before the value is ever needed asks nobody.

The races and the crashes are built from on-disk state rather than injected. A crash after
publication is a receipt at one state beside a file at the next. A concurrent edit is an ordinary
`atomic_write` at the target between one call and the next. Both are exactly what the running
system would leave behind.
"""

from __future__ import annotations

import hashlib
import json

from contextlib import asynccontextmanager
from datetime import date
from typing import TYPE_CHECKING

import httpx
import pytest

from core.cqrs.contracts import MemoryJobs
from core.files import atomic_write
from engram.authoring.contracts import ASSIGNED_FRONTMATTER
from engram.testing.fakes.authoring import FakeReindex
from httpx import ASGITransport, AsyncClient, Response

from studio.knowledge_base.http.api import create_app
from studio.knowledge_base.retention.contracts import NotePreview, NoteRetention, RetainedNote
from studio.knowledge_base.retention.domain import NOTE_ID_HEX, RetentionReceipt, RetentionState
from studio.knowledge_base.retention.infrastructure.artifact_resolver import HarnessArtifactResolver
from studio.knowledge_base.retention.infrastructure.knowledge_bases import knowledge_base_reader
from studio.knowledge_base.retention.infrastructure.receipts import RetentionReceipts
from studio.knowledge_base.settings import ApplicationSettings
from tests.support.fakes.harness_artifacts import FakeHarness
from tests.support.http_app import fake_broadcaster, wiring, write_settings_json

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from pydantic import JsonValue

KNOWLEDGE_BASE = "notebook"
ARTIFACT = "retention_draft"
STAMPED = date(2026, 9, 13)
SOMEBODY_ELSE = "somebody's own page, written first\n"
OWNER_REWROTE_IT = "the owner rewrote this the moment it landed\n"

NOTE_DRAFT: dict[str, JsonValue] = {
    "page_type": "concept",
    "title": "What the run concluded",
    "summary": "One reading of what the evidence supports.",
    "frontmatter": {"status": "draft"},
    "sections": [
        {"heading": "TL;DR", "body": "It holds under the stated conditions."},
        {"heading": "Sources", "body": "The two papers read for this."},
    ],
}


def _spec(slug: str) -> dict[str, JsonValue]:
    """The smallest schema that declares a page type a draft can land as."""
    return {
        "slug": slug,
        "page_types": [{"page_type": "concept", "id_prefix": "c-", "folder": "Wiki/Concepts"}],
        "retrieval": {"collection": slug},
    }


def _knowledge_base(root: Path, slug: str = KNOWLEDGE_BASE) -> Path:
    """A registered knowledge base on disk: the directory shape, and its own schema."""
    (root / "Units").mkdir(parents=True, exist_ok=True)
    (root / "_meta").mkdir(parents=True, exist_ok=True)
    (root / "knowledge-base-schema.json").write_text(json.dumps(_spec(slug)), encoding="utf-8")
    return root


class Bed:
    """One registered knowledge base, one fake harness, and the served app over both."""

    def __init__(self, tmp_path: Path) -> None:
        """Lay out the knowledge base, the registry pointing at it, and the harness that answers for the value."""
        self.root = _knowledge_base(tmp_path / KNOWLEDGE_BASE)
        self.settings_path = write_settings_json(
            tmp_path / "application_settings.json", {KNOWLEDGE_BASE: {"path": str(self.root), "description": ""}}
        )
        self.harness = FakeHarness(values={ARTIFACT: dict(NOTE_DRAFT)})
        self.reindex = FakeReindex()
        self.retention = NoteRetention(
            HarnessArtifactResolver("http://harness.invalid", transport=self.harness.transport()),
            knowledge_base_reader(self.settings_path),
            self.reindex,
            RetentionReceipts,
            today=lambda: STAMPED,
        )

    @property
    def origin(self) -> dict[str, JsonValue]:
        """The reference the harness minted for its note-draft value, as the wire carries it."""
        return self.harness.ref(ARTIFACT).model_dump(mode="json")

    def tree(self) -> dict[str, bytes]:
        """Every file under the knowledge base, so a refusal can be proven to have changed nothing."""
        return {str(path.relative_to(self.root)): path.read_bytes() for path in sorted(self.root.rglob("*")) if path.is_file()}

    def note_at(self, note_id: str) -> Path:
        """Where a note of that id lands in this knowledge base."""
        return self.root / "Wiki" / "Concepts" / f"{note_id.removeprefix('c-')}.md"

    def receipt_path(self, key: str) -> Path:
        """Where this knowledge base keeps the record of one request key."""
        return self.root / ".engram" / "retention" / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"

    def receipt_for(self, key: str) -> RetentionReceipt:
        """The record this knowledge base kept for one request key."""
        return RetentionReceipt.model_validate_json(self.receipt_path(key).read_text(encoding="utf-8"))

    def put_receipt(self, receipt: RetentionReceipt) -> None:
        """Put one record on disk as an interrupted attempt would have left it."""
        path = self.receipt_path(receipt.idempotency_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, receipt.model_dump_json(indent=2))


@asynccontextmanager
async def _client(bed: Bed) -> AsyncGenerator[AsyncClient]:
    """The served app over this bed, with the retention service the bed built.

    `raise_app_exceptions=False` lets the app's own catch-all answer, since the uniform refusal
    body is part of what these check.
    """
    built = wiring()
    built.container.override.set(NoteRetention, bed.retention)
    app = create_app(
        ApplicationSettings(json_file=bed.settings_path),
        container=built.container,
        settings_path=bed.settings_path,
        jobs=MemoryJobs(),
        broadcaster=fake_broadcaster(),
    )
    async with (
        AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://localhost") as client,
        app.router.lifespan_context(app),
    ):
        yield client


@pytest.fixture
def bed(tmp_path: Path) -> Bed:
    """One laid-out knowledge base per test."""
    return Bed(tmp_path)


async def _ask_preview(client: AsyncClient, bed: Bed, **overrides: JsonValue) -> Response:
    """Ask for a preview of the bed's own value, whatever the answer turns out to be."""
    body: dict[str, JsonValue] = {"origin": bed.origin, **overrides}
    return await client.post(f"/api/knowledge-bases/{KNOWLEDGE_BASE}/notes/preview", json=body)


async def _preview(client: AsyncClient, bed: Bed, **overrides: JsonValue) -> NotePreview:
    """A preview that answered, read back as the shape the contract promises."""
    answer = await _ask_preview(client, bed, **overrides)
    assert answer.status_code == httpx.codes.OK, answer.text
    return NotePreview.model_validate(answer.json())


def _keep_body(preview: NotePreview, bed: Bed, key: str, overrides: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """The body that keeps exactly what one preview answered, with any field of it overridden."""
    return {
        "idempotency_key": key,
        "origin": bed.origin,
        "draft": preview.draft.model_dump(mode="json"),
        "schema_digest": preview.schema_digest,
        "preview_digest": preview.preview_digest,
        **overrides,
    }


async def _ask_keep(client: AsyncClient, preview: NotePreview, bed: Bed, *, key: str = "key-1", **overrides: JsonValue) -> Response:
    """Ask to keep one preview, whatever the answer turns out to be."""
    return await client.post(f"/api/knowledge-bases/{KNOWLEDGE_BASE}/notes", json=_keep_body(preview, bed, key, overrides))


async def _keep(client: AsyncClient, preview: NotePreview, bed: Bed, *, key: str = "key-1", **overrides: JsonValue) -> RetainedNote:
    """A keep that answered, read back as the shape the contract promises."""
    answer = await _ask_keep(client, preview, bed, key=key, **overrides)
    assert answer.status_code == httpx.codes.OK, answer.text
    return RetainedNote.model_validate(answer.json())


class TestPreview:
    async def test_a_preview_renders_the_note_and_writes_nothing(self, bed: Bed) -> None:
        before = bed.tree()
        async with _client(bed) as client:
            preview = await _preview(client, bed)
        assert preview.note_id.startswith("c-")
        assert preview.path == f"Wiki/Concepts/{preview.note_id.removeprefix('c-')}.md"
        assert "## TL;DR" in preview.markdown
        assert preview.read_ref == f"page:{preview.note_id}"
        assert bed.tree() == before

    async def test_the_note_id_is_24_hex_after_the_page_types_prefix(self, bed: Bed) -> None:
        async with _client(bed) as client:
            preview = await _preview(client, bed)
        suffix = preview.note_id.removeprefix("c-")
        assert len(suffix) == NOTE_ID_HEX
        assert set(suffix) <= set("0123456789abcdef")

    async def test_editing_the_draft_never_moves_where_it_would_land(self, bed: Bed) -> None:
        """The id comes from where the value came from, so a person's edits cannot retarget it."""
        async with _client(bed) as client:
            first = await _preview(client, bed)
            edited = await _preview(client, bed, edited_draft={**NOTE_DRAFT, "title": "A different title entirely"})
        assert edited.note_id == first.note_id
        assert edited.path == first.path
        assert edited.preview_digest != first.preview_digest

    async def test_the_normalized_draft_carries_the_day_so_its_bytes_do_not_move(self, bed: Bed) -> None:
        async with _client(bed) as client:
            preview = await _preview(client, bed)
        assert preview.draft.frontmatter["created"] == STAMPED.isoformat()
        assert f"created: '{STAMPED.isoformat()}'" in preview.markdown


class TestTheBoundary:
    """Every refusal, and the proof that each one left the disk and the harness alone."""

    async def test_an_origin_carrying_a_url_field_is_not_a_reference(self, bed: Bed) -> None:
        before = bed.tree()
        async with _client(bed) as client:
            answer = await _ask_preview(client, bed, origin={**bed.origin, "url": "http://somewhere.else/value"})
        assert answer.status_code == 422
        assert bed.tree() == before
        assert bed.harness.asked == []

    async def test_an_artifact_this_run_never_fixed_is_refused(self, bed: Bed) -> None:
        before = bed.tree()
        async with _client(bed) as client:
            answer = await _ask_preview(client, bed, origin={**bed.origin, "id": "no_such_artifact"})
        assert answer.status_code == 409
        assert answer.json()["error"] == "origin_unavailable"
        assert bed.tree() == before

    async def test_a_reference_naming_another_run_reaches_that_run_and_no_other(self, bed: Bed) -> None:
        """A reference names its own run, so retargeting it asks that run rather than reading this one's value."""
        before = bed.tree()
        async with _client(bed) as client:
            answer = await _ask_preview(client, bed, origin={**bed.origin, "run_id": "run-2"})
        assert answer.status_code == 409
        assert answer.json()["error"] == "origin_unavailable"
        assert [asked.run_id for asked in bed.harness.asked] == ["run-2"]
        assert bed.tree() == before

    async def test_a_reference_whose_digest_disagrees_is_refused(self, bed: Bed) -> None:
        before = bed.tree()
        async with _client(bed) as client:
            answer = await _ask_preview(client, bed, origin={**bed.origin, "value_digest": "0" * 64})
        assert answer.status_code == 409
        assert bed.tree() == before

    async def test_an_answer_about_another_reference_is_refused(self, bed: Bed) -> None:
        """The harness is asked about one value, so an answer about a different one is nobody's preview."""
        before = bed.tree()
        other = bed.harness.ref(ARTIFACT).model_copy(update={"id": "something_else"})
        bed.harness.raw_body = json.dumps({"ref": other.model_dump(mode="json"), "value": dict(NOTE_DRAFT)}).encode()
        async with _client(bed) as client:
            answer = await _ask_preview(client, bed)
        assert answer.status_code == 409
        assert bed.tree() == before

    async def test_an_oversized_answer_is_refused_rather_than_held(self, bed: Bed) -> None:
        before = bed.tree()
        bed.harness.raw_body = b"[" + b"0," * 700_000 + b"0]"
        async with _client(bed) as client:
            answer = await _ask_preview(client, bed)
        assert answer.status_code == 409
        assert bed.tree() == before

    async def test_a_nonfinite_number_in_the_value_is_not_a_value_anything_addresses(self, bed: Bed) -> None:
        before = bed.tree()
        bed.harness.raw_body = json.dumps({"ref": bed.origin, "value": float("nan")}).encode()
        async with _client(bed) as client:
            answer = await _ask_preview(client, bed)
        assert answer.status_code == 409
        assert bed.tree() == before

    async def test_an_unregistered_destination_is_refused_before_the_harness_is_asked(self, bed: Bed) -> None:
        before = bed.tree()
        async with _client(bed) as client:
            answer = await client.post("/api/knowledge-bases/nowhere/notes/preview", json={"origin": bed.origin})
        assert answer.status_code == 404
        assert answer.json()["error"] == "unknown_knowledge_base"
        assert bed.harness.asked == []
        assert bed.tree() == before

    async def test_a_page_type_this_knowledge_base_does_not_declare_is_refused(self, bed: Bed) -> None:
        before = bed.tree()
        async with _client(bed) as client:
            answer = await _ask_preview(client, bed, edited_draft={**NOTE_DRAFT, "page_type": "recipe"})
        assert answer.status_code == 400
        assert answer.json()["error"] == "unknown_page_type"
        assert bed.tree() == before

    @pytest.mark.parametrize("assigned", sorted(ASSIGNED_FRONTMATTER))
    async def test_a_draft_may_not_assign_what_the_destination_assigns(self, bed: Bed, assigned: str) -> None:
        before = bed.tree()
        async with _client(bed) as client:
            answer = await _ask_preview(client, bed, edited_draft={**NOTE_DRAFT, "frontmatter": {"status": "draft", assigned: "mine"}})
        assert answer.status_code == 422
        assert bed.tree() == before

    async def test_a_value_that_is_not_a_proposed_note_is_refused(self, bed: Bed) -> None:
        before = bed.tree()
        bed.harness.values[ARTIFACT] = {"some": "other shape"}
        async with _client(bed) as client:
            answer = await _ask_preview(client, bed)
        assert answer.status_code == 422
        assert answer.json()["error"] == "not_a_note_draft"
        assert bed.tree() == before

    async def test_a_harness_that_cannot_be_reached_refuses_rather_than_guesses(self, bed: Bed) -> None:
        before = bed.tree()
        bed.harness.status = 503
        bed.harness.message = "this harness is restarting"
        async with _client(bed) as client:
            answer = await _ask_preview(client, bed)
        assert answer.status_code == 409
        assert "this harness is restarting" in answer.json()["detail"]
        assert bed.tree() == before

    async def test_a_schema_that_has_changed_since_the_preview_refuses_the_keep(self, bed: Bed) -> None:
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            before = bed.tree()
            answer = await _ask_keep(client, preview, bed, schema_digest="0" * 64)
        assert answer.status_code == 409
        assert answer.json()["error"] == "schema_changed"
        assert bed.tree() == before
        assert bed.reindex.knowledge_bases == []

    async def test_a_draft_edited_after_the_preview_refuses_the_keep(self, bed: Bed) -> None:
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            before = bed.tree()
            moved = {**preview.draft.model_dump(mode="json"), "title": "Something else now"}
            answer = await _ask_keep(client, preview, bed, draft=moved)
        assert answer.status_code == 409
        assert answer.json()["error"] == "preview_changed"
        assert bed.tree() == before
        assert bed.reindex.knowledge_bases == []

    async def test_one_key_used_for_different_content_is_refused(self, bed: Bed) -> None:
        async with _client(bed) as client:
            first = await _preview(client, bed)
            await _keep(client, first, bed, key="key-1")
            landed = bed.tree()
            second = await _preview(client, bed, edited_draft={**NOTE_DRAFT, "title": "A wholly different note"})
            answer = await _ask_keep(client, second, bed, key="key-1")
        assert answer.status_code == 409
        assert answer.json()["error"] == "key_reused"
        assert bed.tree() == landed


class TestTheKeepAsksToo:
    """Every request resolves the value, so a draft in the body excuses no check on either route.

    A person's own words replace the proposal. They never replace the question of whether the
    value they came from is one this host answers for, which is asked again on every request.
    """

    async def test_a_preview_carrying_an_edited_draft_is_refused_when_the_harness_will_not_answer(self, bed: Bed) -> None:
        """A draft in the body is the words, never the warrant. The reference is still resolved."""
        before = bed.tree()
        bed.harness.status = 503
        bed.harness.message = "this harness is restarting"
        async with _client(bed) as client:
            answer = await _ask_preview(client, bed, edited_draft={**NOTE_DRAFT, "title": "My own words"})
        assert answer.status_code == 409
        assert answer.json()["error"] == "origin_unavailable"
        assert len(bed.harness.asked) == 1
        assert bed.tree() == before

    async def test_a_keep_asks_the_harness_even_though_it_carries_the_draft(self, bed: Bed) -> None:
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            asked_by_preview = len(bed.harness.asked)
            await _keep(client, preview, bed)
        assert len(bed.harness.asked) == asked_by_preview + 1
        assert bed.harness.asked[-1] == bed.harness.ref(ARTIFACT)

    async def test_a_keep_is_refused_when_the_harness_will_not_answer(self, bed: Bed) -> None:
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            before = bed.tree()
            asked_by_preview = len(bed.harness.asked)
            bed.harness.status = 503
            bed.harness.message = "this harness is restarting"
            answer = await _ask_keep(client, preview, bed)
        assert answer.status_code == 409
        assert answer.json()["error"] == "origin_unavailable"
        assert len(bed.harness.asked) == asked_by_preview + 1
        assert bed.tree() == before
        assert bed.reindex.knowledge_bases == []

    async def test_a_keep_naming_an_artifact_no_run_fixed_writes_nothing(self, bed: Bed) -> None:
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            before = bed.tree()
            asked_by_preview = len(bed.harness.asked)
            answer = await _ask_keep(client, preview, bed, origin={**bed.origin, "id": "no_such_artifact"})
        assert answer.status_code == 409
        assert answer.json()["error"] == "origin_unavailable"
        assert len(bed.harness.asked) == asked_by_preview + 1
        assert bed.tree() == before

    async def test_a_keep_naming_a_run_nothing_archived_writes_nothing(self, bed: Bed) -> None:
        """An invented reference over an invented run reaches that run and comes back with nothing."""
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            before = bed.tree()
            invented = {**bed.origin, "run_id": "run-does-not-exist", "value_digest": "0" * 64}
            answer = await _ask_keep(client, preview, bed, origin=invented)
        assert answer.status_code == 409
        assert answer.json()["error"] == "origin_unavailable"
        assert [asked.run_id for asked in bed.harness.asked][-1] == "run-does-not-exist"
        assert bed.tree() == before

    async def test_a_keep_whose_reference_digest_disagrees_writes_nothing(self, bed: Bed) -> None:
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            before = bed.tree()
            asked_by_preview = len(bed.harness.asked)
            answer = await _ask_keep(client, preview, bed, origin={**bed.origin, "value_digest": "0" * 64})
        assert answer.status_code == 409
        assert len(bed.harness.asked) == asked_by_preview + 1
        assert bed.tree() == before

    async def test_a_keep_whose_value_stopped_being_a_proposed_note_writes_nothing(self, bed: Bed) -> None:
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            before = bed.tree()
            bed.harness.values[ARTIFACT] = {"some": "other shape"}
            answer = await _ask_keep(client, preview, bed, origin=bed.origin)
        assert answer.status_code == 422
        assert answer.json()["error"] == "not_a_note_draft"
        assert bed.tree() == before


class TestTheMintedId:
    async def test_what_the_artifact_is_called_never_changes_where_the_note_lands(self, bed: Bed) -> None:
        """The id is over what the reference addresses, not over how it is presented to a person."""
        async with _client(bed) as client:
            first = await _preview(client, bed)
            relabelled = {**bed.origin, "label": "A better name for this"}
            answer = await _ask_preview(client, bed, origin=relabelled)
        assert answer.status_code == httpx.codes.OK
        renamed = NotePreview.model_validate(answer.json())
        assert renamed.note_id == first.note_id
        assert renamed.path == first.path

    async def test_a_different_value_of_the_same_artifact_lands_somewhere_else(self, bed: Bed) -> None:
        async with _client(bed) as client:
            first = await _preview(client, bed)
            bed.harness.values[ARTIFACT] = {**NOTE_DRAFT, "title": "A different conclusion"}
            second = await _preview(client, bed, origin=bed.origin)
        assert second.note_id != first.note_id


class TestKeeping:
    async def test_a_confirmed_preview_lands_and_indexes(self, bed: Bed) -> None:
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            kept = await _keep(client, preview, bed)
        assert kept.outcome == "saved"
        assert bed.note_at(kept.note_id).read_text(encoding="utf-8") == preview.markdown
        assert [knowledge_base.name for knowledge_base in bed.reindex.knowledge_bases] == [KNOWLEDGE_BASE]
        assert bed.receipt_for("key-1").state is RetentionState.INDEXED

    async def test_the_identical_request_again_writes_nothing_and_indexes_nothing(self, bed: Bed) -> None:
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            await _keep(client, preview, bed)
            landed = bed.tree()
            again = await _keep(client, preview, bed)
        assert again.outcome == "already_indexed"
        assert bed.tree() == landed
        assert len(bed.reindex.knowledge_bases) == 1

    async def test_a_page_already_at_that_name_is_never_replaced(self, bed: Bed) -> None:
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            target = bed.note_at(preview.note_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(target, SOMEBODY_ELSE)
            answer = await _ask_keep(client, preview, bed)
        assert answer.status_code == 409
        assert answer.json()["outcome"] == "changed"
        assert answer.json()["error"] == "target_changed"
        assert target.read_text(encoding="utf-8") == SOMEBODY_ELSE
        assert bed.reindex.knowledge_bases == []

    async def test_a_crash_after_publication_recovers_by_comparing_the_bytes(self, bed: Bed) -> None:
        """The file is there and the record still says prepared, so the retry indexes and creates nothing."""
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            target = bed.note_at(preview.note_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(target, preview.markdown)
            published = target.stat().st_mtime_ns
            bed.put_receipt(_prepared_receipt(bed, preview))
            kept = await _keep(client, preview, bed)
        assert kept.outcome == "saved"
        assert target.stat().st_mtime_ns == published
        assert len(bed.reindex.knowledge_bases) == 1
        assert bed.receipt_for("key-1").state is RetentionState.INDEXED

    async def test_an_edit_between_publication_and_the_record_is_never_undone(self, bed: Bed) -> None:
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            target = bed.note_at(preview.note_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(target, preview.markdown)
            bed.put_receipt(_prepared_receipt(bed, preview))
            atomic_write(target, OWNER_REWROTE_IT)
            answer = await _ask_keep(client, preview, bed)
        assert answer.status_code == 409
        assert answer.json()["outcome"] == "changed"
        assert answer.json()["error"] == "target_changed"
        assert target.read_text(encoding="utf-8") == OWNER_REWROTE_IT
        assert bed.reindex.knowledge_bases == []

    async def test_an_edit_after_a_kept_note_is_reported_rather_than_replaced(self, bed: Bed) -> None:
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            await _keep(client, preview, bed)
            target = bed.note_at(preview.note_id)
            atomic_write(target, OWNER_REWROTE_IT)
            answer = await _ask_keep(client, preview, bed)
        assert answer.status_code == 409
        assert answer.json()["outcome"] == "changed"
        assert answer.json()["error"] == "target_changed"
        assert target.read_text(encoding="utf-8") == OWNER_REWROTE_IT

    async def test_a_failed_index_is_reported_as_written_but_not_indexed(self, bed: Bed) -> None:
        bed.reindex.outcome = "not_indexed"
        bed.reindex.detail = "the database was not reachable"
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            kept = await _keep(client, preview, bed)
        assert kept.outcome == "saved_not_indexed"
        assert kept.detail == "the database was not reachable"
        assert bed.note_at(kept.note_id).read_text(encoding="utf-8") == preview.markdown
        assert bed.receipt_for("key-1").state is RetentionState.SAVED

    async def test_the_retry_after_a_failed_index_only_indexes(self, bed: Bed) -> None:
        bed.reindex.outcome = "not_indexed"
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            await _keep(client, preview, bed)
            landed = bed.note_at(preview.note_id).read_bytes()
            bed.reindex.outcome = "clean"
            again = await _keep(client, preview, bed)
        assert again.outcome == "saved"
        assert bed.note_at(preview.note_id).read_bytes() == landed
        assert len(bed.reindex.knowledge_bases) == 2
        assert bed.receipt_for("key-1").state is RetentionState.INDEXED

    async def test_validation_errors_elsewhere_are_not_a_failed_index(self, bed: Bed) -> None:
        bed.reindex.outcome = "validation_errors"
        bed.reindex.findings_total = 2
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            kept = await _keep(client, preview, bed)
        assert kept.outcome == "saved"
        assert bed.receipt_for("key-1").state is RetentionState.INDEXED

    async def test_a_replayed_key_never_writes_back_a_note_somebody_removed(self, bed: Bed) -> None:
        """Removing a kept note is the owner's decision about their own knowledge base."""
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            kept = await _keep(client, preview, bed)
            bed.note_at(kept.note_id).unlink()
            runs = len(bed.reindex.knowledge_bases)
            answer = await _ask_keep(client, preview, bed)
        assert answer.status_code == 409
        assert answer.json()["outcome"] == "changed"
        assert "no longer there" in answer.json()["detail"]
        assert not bed.note_at(kept.note_id).exists()
        assert len(bed.reindex.knowledge_bases) == runs

    async def test_the_receipt_lives_where_nothing_indexes_it(self, bed: Bed) -> None:
        async with _client(bed) as client:
            preview = await _preview(client, bed)
            await _keep(client, preview, bed)
        kept = bed.receipt_for("key-1")
        assert kept.target == preview.path
        assert kept.read_ref == f"page:{kept.note_id}"
        assert not any(part.startswith(".") for part in preview.path.split("/"))
        assert bed.receipt_path("key-1").is_relative_to(bed.root / ".engram")


def _prepared_receipt(bed: Bed, preview: NotePreview) -> RetentionReceipt:
    """The record an attempt interrupted between publication and its own update would have left."""
    content = preview.markdown.encode("utf-8")
    return RetentionReceipt(
        idempotency_key="key-1",
        state=RetentionState.PREPARED,
        knowledge_base=KNOWLEDGE_BASE,
        note_id=preview.note_id,
        target=preview.path,
        origin=bed.harness.ref(ARTIFACT),
        schema_digest=preview.schema_digest,
        preview_digest=preview.preview_digest,
        content_digest=hashlib.sha256(content).hexdigest(),
        content_bytes=len(content),
    )
