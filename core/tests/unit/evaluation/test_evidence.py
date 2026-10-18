"""Unit specs for the immutable delivered-evidence contract and its file store.

A grader judges what execution saw. These specs pin that: the bytes a trial delivers are stored
once, read back byte-identical after a restart, refused when they would rewrite an earlier delivery
or cross a size bound, and refused again on read when the stored file no longer matches the
reference naming it. Nothing here consults a live source, because there is no source reader to
consult.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from typing import TYPE_CHECKING

import pytest

from core.ai.evaluation.contracts import DeliveredEvidenceRef, DeliveredSource, FileDeliveredEvidenceStore
from core.ai.evaluation.domain.evidence import DELIVERED_PAYLOAD_BYTES, TRIAL_DELIVERED_EVIDENCE_BYTES
from core.ai.evaluation.exceptions import (
    DeliveredEvidenceBoundError,
    DeliveredEvidenceConflictError,
    DeliveredEvidencePersistenceError,
    DeliveredEvidenceUnreadableError,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_EVIDENCE = "delivered-evidence"


def _payload(size: int, filler: str = "a") -> str:
    """A payload of exactly `size` single-byte characters."""
    return filler * size


def _trial_directory(root: Path, trial_id: str) -> Path:
    """The one directory a trial's evidence lives in."""
    return root / _EVIDENCE / hashlib.sha256(trial_id.encode("utf-8")).hexdigest()


def _manifest(root: Path, trial_id: str) -> dict[str, object]:
    """One trial's manifest as stored."""
    parsed = json.loads((_trial_directory(root, trial_id) / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


async def _fill_to_the_trial_bound(store: FileDeliveredEvidenceStore, trial_id: str) -> None:
    """Deliver four max-size payloads, which is exactly the trial bound."""
    for ordinal in range(4):
        await store.append(trial_id, ordinal, _payload(DELIVERED_PAYLOAD_BYTES - 1) + str(ordinal), sources=())


# -- the contract shapes -----------------------------------------------------


class TestDeliveredEvidenceShapes:
    def test_a_source_with_no_observed_integrity_keeps_both_fields_unknown(self):
        source = DeliveredSource(source_ref="page:research/lightrag")

        assert source.source_content_sha256 is None
        assert source.source_version is None

    def test_a_reference_cannot_name_an_empty_payload(self):
        with pytest.raises(ValueError, match="payload_bytes"):
            DeliveredEvidenceRef(evidence_id="e", ordinal=0, payload_sha256="0" * 64, payload_bytes=0)

    def test_a_reference_cannot_name_a_payload_over_the_payload_bound(self):
        with pytest.raises(ValueError, match="payload_bytes"):
            DeliveredEvidenceRef(evidence_id="e", ordinal=0, payload_sha256="0" * 64, payload_bytes=DELIVERED_PAYLOAD_BYTES + 1)

    def test_the_bounds_are_the_declared_numbers(self):
        assert DELIVERED_PAYLOAD_BYTES == 262_144
        assert TRIAL_DELIVERED_EVIDENCE_BYTES == 1_048_576


# -- storing and reading back ------------------------------------------------


class TestAppendAndRead:
    async def test_the_reference_names_the_exact_bytes_stored(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)
        payload = json.dumps({"hits": [{"ref": "page:research/a"}]})

        ref = await store.append("trial-1", 0, payload, sources=(DeliveredSource(source_ref="page:research/a"),))

        assert ref.ordinal == 0
        assert ref.payload_bytes == len(payload.encode("utf-8"))
        assert ref.payload_sha256 == hashlib.sha256(payload.encode("utf-8")).hexdigest()
        assert (await store.read(ref)).payload_json == payload

    async def test_a_payload_is_filed_under_its_own_hash_inside_its_own_trial(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)

        ref = await store.append("trial-1", 0, "delivered", sources=())

        assert (_trial_directory(tmp_path, "trial-1") / f"{ref.payload_sha256}.json").read_bytes() == b"delivered"

    async def test_sources_keep_payload_order_and_each_observed_hash_and_version(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)
        sources = (
            DeliveredSource(source_ref="page:research/first"),
            DeliveredSource(source_ref="page:research/second", source_content_sha256="b" * 64, source_version="v3"),
            DeliveredSource(source_ref="page:research/third"),
        )

        ref = await store.append("trial-1", 0, "three refs", sources=sources)

        assert (await store.read(ref)).ref.sources == sources
        assert [source.source_ref for source in ref.sources] == ["page:research/first", "page:research/second", "page:research/third"]
        assert ref.sources[0].source_content_sha256 is None
        assert ref.sources[1].source_version == "v3"

    async def test_a_restart_reads_byte_identical_evidence(self, tmp_path: Path):
        payload = json.dumps({"body": "the text the model actually read"})
        ref = await FileDeliveredEvidenceStore(tmp_path).append("trial-1", 0, payload, sources=())

        restarted = FileDeliveredEvidenceStore(tmp_path)

        read = await restarted.read(ref)
        assert read.payload_json == payload
        assert read.ref == ref

    async def test_the_stored_payload_does_not_follow_a_source_that_changed_afterwards(self, tmp_path: Path):
        # The store holds bytes, not a pointer to a source, so there is nothing for a later edit to
        # change. A grader reading this reference reads the delivery, whatever the source says now.
        store = FileDeliveredEvidenceStore(tmp_path)
        ref = await store.append("trial-1", 0, json.dumps({"body": "original"}), sources=(DeliveredSource(source_ref="page:research/a"),))

        later = await store.append("trial-1", 1, json.dumps({"body": "rewritten"}), sources=(DeliveredSource(source_ref="page:research/a"),))

        assert (await store.read(ref)).payload_json == json.dumps({"body": "original"})
        assert (await store.read(later)).payload_json == json.dumps({"body": "rewritten"})


# -- idempotency and immutability --------------------------------------------


class TestImmutability:
    async def test_repeating_the_same_delivery_returns_the_same_reference(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)
        sources = (DeliveredSource(source_ref="page:research/a"),)

        first = await store.append("trial-1", 0, "identical", sources=sources)
        second = await store.append("trial-1", 0, "identical", sources=sources)

        assert second == first

    async def test_repeating_a_delivery_does_not_count_its_bytes_twice(self, tmp_path: Path):
        # The trial is already exactly at its bound, so a repeat that re-admitted its bytes would
        # cross it. It does not: the repeat is a read of what was already filed.
        store = FileDeliveredEvidenceStore(tmp_path)
        await _fill_to_the_trial_bound(store, "trial-1")

        repeated = await store.append("trial-1", 0, _payload(DELIVERED_PAYLOAD_BYTES - 1) + "0", sources=())

        assert repeated.ordinal == 0
        assert _manifest(tmp_path, "trial-1")["total_bytes"] == TRIAL_DELIVERED_EVIDENCE_BYTES

    async def test_the_same_identity_with_different_bytes_is_refused_and_says_so(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)
        await store.append("trial-1", 0, "first", sources=())

        with pytest.raises(DeliveredEvidenceConflictError, match="already names different bytes"):
            await store.append("trial-1", 0, "second", sources=())

    async def test_the_same_bytes_with_different_sources_is_refused_over_the_sources(self, tmp_path: Path):
        # The refusal is right and the reason has to be too: nothing about the content differs.
        store = FileDeliveredEvidenceStore(tmp_path)
        await store.append("trial-1", 0, "same bytes", sources=(DeliveredSource(source_ref="page:research/a"),))

        with pytest.raises(DeliveredEvidenceConflictError, match="the same bytes with different sources"):
            await store.append("trial-1", 0, "same bytes", sources=(DeliveredSource(source_ref="page:research/b"),))

    async def test_a_crash_part_way_through_a_write_leaves_the_content_address_free(self, tmp_path: Path):
        # A write that died before every byte was on disk leaves a temporary behind, never a short
        # file under the hash. The next append of those exact bytes has to succeed.
        store = FileDeliveredEvidenceStore(tmp_path)
        await store.append("trial-1", 0, "first payload", sources=())
        directory = _trial_directory(tmp_path, "trial-1")
        (directory / "crashed-part-way.tmp").write_bytes(b"deliv")

        ref = await store.append("trial-1", 1, "delivered whole", sources=())

        assert (await store.read(ref)).payload_json == "delivered whole"
        assert (directory / f"{ref.payload_sha256}.json").read_bytes() == b"delivered whole"

    async def test_a_finished_append_leaves_no_temporary_behind(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)

        await store.append("trial-1", 0, "delivered whole", sources=())

        assert list(_trial_directory(tmp_path, "trial-1").glob("*.tmp")) == []

    async def test_two_positions_delivering_the_same_bytes_share_one_file(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)

        first = await store.append("trial-1", 0, "repeated result", sources=())
        second = await store.append("trial-1", 1, "repeated result", sources=())

        assert first.payload_sha256 == second.payload_sha256
        assert first.evidence_id != second.evidence_id
        assert len(list(_trial_directory(tmp_path, "trial-1").glob("*.json"))) == 2  # the payload plus the manifest


# -- size bounds --------------------------------------------------------------


class TestSizeBounds:
    async def test_a_payload_at_the_payload_bound_is_admitted_whole(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)

        ref = await store.append("trial-1", 0, _payload(DELIVERED_PAYLOAD_BYTES), sources=())

        assert ref.payload_bytes == 262_144

    async def test_a_payload_over_the_payload_bound_invalidates_the_trial(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)

        with pytest.raises(DeliveredEvidenceBoundError, match="262144 byte payload bound"):
            await store.append("trial-1", 0, _payload(DELIVERED_PAYLOAD_BYTES + 1), sources=())

    async def test_an_over_bound_payload_is_never_truncated_into_the_store(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)

        with pytest.raises(DeliveredEvidenceBoundError):
            await store.append("trial-1", 0, _payload(DELIVERED_PAYLOAD_BYTES + 1), sources=())

        assert list(_trial_directory(tmp_path, "trial-1").glob("*.json")) == []

    async def test_a_trial_may_accumulate_up_to_the_trial_bound(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)

        await _fill_to_the_trial_bound(store, "trial-1")

        assert _manifest(tmp_path, "trial-1")["total_bytes"] == 1_048_576

    async def test_crossing_the_trial_bound_is_refused_before_more_evidence_is_admitted(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)
        await _fill_to_the_trial_bound(store, "trial-1")
        directory = _trial_directory(tmp_path, "trial-1")
        before = (directory / "manifest.json").read_bytes()

        with pytest.raises(DeliveredEvidenceBoundError, match="1048576 byte trial bound"):
            await store.append("trial-1", 4, "x", sources=())

        assert len(list(directory.glob("*.json"))) == 5  # four payloads plus the manifest
        assert (directory / "manifest.json").read_bytes() == before


# -- refusing a read that does not match its reference -----------------------


def _delete(path: Path) -> None:
    path.unlink()


def _truncate(path: Path) -> None:
    path.write_bytes(path.read_bytes()[:-1])


def _rewrite_same_length(path: Path) -> None:
    payload = path.read_bytes()
    path.write_bytes(b"x" * len(payload))


class TestUnreadableEvidence:
    @pytest.mark.parametrize(
        "corrupt", [_delete, _truncate, _rewrite_same_length], ids=["missing payload", "byte count differs", "payload hash differs"]
    )
    async def test_a_stored_payload_that_stopped_matching_its_reference_is_refused(
        self, tmp_path: Path, corrupt: Callable[[Path], None]
    ):
        store = FileDeliveredEvidenceStore(tmp_path)
        ref = await store.append("trial-1", 0, "delivered result", sources=())
        corrupt(_trial_directory(tmp_path, "trial-1") / f"{ref.payload_sha256}.json")

        with pytest.raises(DeliveredEvidenceUnreadableError):
            await store.read(ref)

    async def test_a_reference_the_manifest_does_not_file_is_refused(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)
        ref = await store.append("trial-1", 0, "delivered result", sources=())

        claimed = ref.model_copy(update={"sources": (DeliveredSource(source_ref="page:research/invented"),)})

        with pytest.raises(DeliveredEvidenceUnreadableError, match="does not file"):
            await store.read(claimed)

    async def test_a_reference_to_a_trial_that_stored_nothing_is_refused(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)
        ref = await store.append("trial-1", 0, "delivered result", sources=())

        elsewhere = FileDeliveredEvidenceStore(tmp_path / "other-root")

        with pytest.raises(DeliveredEvidenceUnreadableError, match="no stored payload"):
            await elsewhere.read(ref)

    async def test_an_evidence_id_that_does_not_name_its_position_is_refused(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)
        ref = await store.append("trial-1", 0, "delivered result", sources=())

        with pytest.raises(DeliveredEvidenceUnreadableError, match="does not name position"):
            await store.read(ref.model_copy(update={"ordinal": 1}))


# -- a record that stopped being a record -------------------------------------


class TestUnreadableRecord:
    """A caller folding one trial's evidence to unknown catches the evidence error and nothing else."""

    async def test_a_corrupt_manifest_refuses_a_read_as_unreadable(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)
        ref = await store.append("trial-1", 0, "delivered result", sources=())
        (_trial_directory(tmp_path, "trial-1") / "manifest.json").write_text("{not json at all", encoding="utf-8")

        with pytest.raises(DeliveredEvidenceUnreadableError, match="not a readable evidence record"):
            await store.read(ref)

    async def test_a_corrupt_manifest_refuses_an_append_as_unreadable(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)
        await store.append("trial-1", 0, "delivered result", sources=())
        (_trial_directory(tmp_path, "trial-1") / "manifest.json").write_text("{not json at all", encoding="utf-8")

        with pytest.raises(DeliveredEvidenceUnreadableError, match="not a readable evidence record"):
            await store.append("trial-1", 1, "another result", sources=())

    async def test_a_manifest_claiming_more_than_a_trial_may_hold_is_unreadable(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)
        ref = await store.append("trial-1", 0, "delivered result", sources=())
        manifest = _trial_directory(tmp_path, "trial-1") / "manifest.json"
        manifest.write_text(
            json.dumps({"trial_id": "trial-1", "total_bytes": TRIAL_DELIVERED_EVIDENCE_BYTES + 1, "entries": []}), encoding="utf-8"
        )

        with pytest.raises(DeliveredEvidenceUnreadableError, match="not a readable evidence record"):
            await store.read(ref)


class TestPersistenceFailures:
    async def test_a_state_root_that_is_a_file_refuses_an_append_as_a_persistence_failure(self, tmp_path: Path):
        occupied = tmp_path / "root"
        occupied.write_text("this is a file, not a state root", encoding="utf-8")

        with pytest.raises(DeliveredEvidencePersistenceError, match="could not be written"):
            await FileDeliveredEvidenceStore(occupied).append("trial-1", 0, "delivered result", sources=())

    async def test_a_read_only_state_root_refuses_an_append_as_a_persistence_failure(self, tmp_path: Path):
        root = tmp_path / "root"
        root.mkdir()
        root.chmod(0o500)
        try:
            with pytest.raises(DeliveredEvidencePersistenceError, match="could not be written"):
                await FileDeliveredEvidenceStore(root).append("trial-1", 0, "delivered result", sources=())
        finally:
            root.chmod(0o700)

    async def test_nothing_is_written_when_the_root_refuses_the_write(self, tmp_path: Path):
        occupied = tmp_path / "root"
        occupied.write_text("this is a file, not a state root", encoding="utf-8")

        with pytest.raises(DeliveredEvidencePersistenceError):
            await FileDeliveredEvidenceStore(occupied).append("trial-1", 0, "delivered result", sources=())

        assert occupied.read_text(encoding="utf-8") == "this is a file, not a state root"


# -- concurrent trials --------------------------------------------------------


class TestConcurrentTrials:
    async def test_two_trials_under_one_root_keep_separate_directories_and_ordinals(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)

        async def deliver(trial_id: str, count: int) -> list[int]:
            return [(await store.append(trial_id, ordinal, f"{trial_id}-{ordinal}", sources=())).ordinal for ordinal in range(count)]

        first, second = await asyncio.gather(deliver("trial-a", 3), deliver("trial-b", 3))

        assert first == [0, 1, 2]
        assert second == [0, 1, 2]
        assert len(list((tmp_path / _EVIDENCE).iterdir())) == 2
        assert len(list(_trial_directory(tmp_path, "trial-a").glob("*.json"))) == 4  # three payloads plus the manifest

    async def test_concurrent_appends_to_one_trial_lose_nothing(self, tmp_path: Path):
        # Every append reloads, checks and swaps the manifest under the trial's own lock, so twenty
        # at once still leave twenty entries and one running total rather than the last writer's copy.
        store = FileDeliveredEvidenceStore(tmp_path)

        refs = await asyncio.gather(
            *(store.append("trial-a", ordinal, f"delivered {ordinal}", sources=()) for ordinal in range(20))
        )

        manifest = _manifest(tmp_path, "trial-a")
        entries = manifest["entries"]
        assert isinstance(entries, list)
        assert len(entries) == 20
        assert sorted(ref.ordinal for ref in refs) == list(range(20))
        assert manifest["total_bytes"] == sum(ref.payload_bytes for ref in refs)
        for ref in refs:
            assert (await store.read(ref)).payload_json == f"delivered {ref.ordinal}"

    async def test_concurrent_appends_to_one_position_admit_exactly_one(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)

        outcomes = await asyncio.gather(
            *(store.append("trial-a", 0, f"delivered {index}", sources=()) for index in range(3)), return_exceptions=True
        )

        admitted = [row for row in outcomes if isinstance(row, DeliveredEvidenceRef)]
        refused = [row for row in outcomes if isinstance(row, DeliveredEvidenceConflictError)]
        assert len(admitted) == 1
        assert len(refused) == 2
        entries = _manifest(tmp_path, "trial-a")["entries"]
        assert isinstance(entries, list)
        assert len(entries) == 1

    async def test_each_trial_accumulates_against_its_own_bound(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)

        await asyncio.gather(_fill_to_the_trial_bound(store, "trial-a"), _fill_to_the_trial_bound(store, "trial-b"))

        assert _manifest(tmp_path, "trial-a")["total_bytes"] == TRIAL_DELIVERED_EVIDENCE_BYTES
        assert _manifest(tmp_path, "trial-b")["total_bytes"] == TRIAL_DELIVERED_EVIDENCE_BYTES
        for trial_id in ("trial-a", "trial-b"):
            with pytest.raises(DeliveredEvidenceBoundError):
                await store.append(trial_id, 4, "x", sources=())
