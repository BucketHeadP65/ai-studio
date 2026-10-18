"""Copying a file-store transcript into the record, once, through the port both stores share."""

from __future__ import annotations

import dataclasses

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai_harness.step_persistence import ContinuableSnapshot, FileStepStore, RunRecord

    from harness.domain.contracts import SessionTranscript


@dataclass(frozen=True)
class ImportReport:
    """What one pass over one directory did."""

    runs: int
    """Runs the record held nothing of before this pass, imported whole."""
    events: int
    snapshots: int
    effects: int
    skipped: int
    """Runs the record already held a snapshot for, left exactly as they were."""
    resumed: int
    """Runs the record held with no snapshot yet, given whatever their source still holds this pass.

    A run whose source holds no snapshot stays counted here on every pass.
    """
    unnamed: int
    """Runs carrying no conversation id, which the record cannot hold."""
    reconciled: int
    """Runs whose snapshots named their own conversation under a different spelling, corrected to the run's own."""
    malformed: int
    """Runs whose conversation id does not parse as a UUID, or whose snapshot names a different one. Left behind."""


async def import_file_store(source: FileStepStore, *, into: SessionTranscript) -> ImportReport:
    """Copy every run of `source` into `into`, resuming what it half holds, and say what landed.

    A run `into` holds no row for is imported whole. A run `into` holds a row for but no snapshot is
    half-imported, because an earlier pass stopped between registering it and saving its first snapshot,
    and is resumed: only the events and snapshots `into` still lacks are written. A run `into` already
    holds a snapshot for is skipped whole, so a finished pass writes nothing twice. A run whose
    conversation id does not parse as a UUID, or whose own snapshot names a different one, is a
    source defect: it is left behind under `malformed`, nothing of it written, and the pass continues.
    A snapshot that names the run's own conversation under a different spelling (with or without
    hyphens) is corrected to the run's canonical spelling before it is ever written.

    Args:
        source: The file store the transcripts were written to.
        into: The record the runs are copied into.

    Returns:
        What the pass imported, resumed, skipped, corrected and left behind.
    """
    runs = events = snapshots = effects = skipped = unnamed = resumed = reconciled = malformed = 0
    for record in await source.list_runs():
        if record.conversation_id is None:
            unnamed += 1
            continue
        try:
            canonical = str(UUID(record.conversation_id))
        except ValueError:
            malformed += 1
            continue

        source_snapshots = await source.list_snapshots(run_id=record.run_id, include_interrupted=True)
        reconciliation = _reconcile(source_snapshots, canonical=canonical)
        if reconciliation is None:
            malformed += 1
            continue
        run_snapshots, this_run_reconciled = reconciliation

        placement = await _place(record, into=into, canonical=canonical)
        if placement is None:
            skipped += 1
            continue
        held_events, held_step_indices, is_new = placement
        runs += is_new
        resumed += not is_new

        if this_run_reconciled:
            reconciled += 1

        for event in (await source.list_events(run_id=record.run_id))[held_events:]:
            await into.append_event(event)
            events += 1
        for snapshot in run_snapshots:
            if snapshot.step_index in held_step_indices:
                continue
            await into.save_snapshot(snapshot)
            snapshots += 1
        effects += await _copy_effects(source, into=into, run_id=record.run_id)
    return ImportReport(
        runs=runs,
        events=events,
        snapshots=snapshots,
        effects=effects,
        skipped=skipped,
        resumed=resumed,
        unnamed=unnamed,
        reconciled=reconciled,
        malformed=malformed,
    )


async def _place(record: RunRecord, *, into: SessionTranscript, canonical: str) -> tuple[int, set[int], bool] | None:
    """The events and step indices `into` holds of a run and whether it was registered just now, `None` when it is finished."""
    held = await into.get_run(run_id=record.run_id)
    if held is None:
        canonical_record = record if record.conversation_id == canonical else dataclasses.replace(record, conversation_id=canonical)
        await into.register_run(canonical_record)
        return 0, set(), True
    if await into.latest_snapshot(run_id=record.run_id, include_interrupted=True) is not None:
        return None
    held_events = len(await into.list_events(run_id=record.run_id))
    held_step_indices = {snapshot.step_index for snapshot in await into.list_snapshots(run_id=record.run_id)}
    return held_events, held_step_indices, False


def _reconcile(snapshots: Sequence[ContinuableSnapshot], *, canonical: str) -> tuple[list[ContinuableSnapshot], bool] | None:
    """Every snapshot spelled the run's canonical way and whether any needed correcting, `None` when one names another conversation."""
    corrected: list[ContinuableSnapshot] = []
    needed_correcting = False
    for snapshot in snapshots:
        if snapshot.conversation_id is None or snapshot.conversation_id == canonical:
            corrected.append(snapshot)
            continue
        try:
            same_conversation = UUID(snapshot.conversation_id) == UUID(canonical)
        except ValueError:
            return None
        if not same_conversation:
            return None
        needed_correcting = True
        corrected.append(dataclasses.replace(snapshot, conversation_id=canonical))
    return corrected, needed_correcting


async def _copy_effects(source: FileStepStore, *, into: SessionTranscript, run_id: str) -> int:
    """Copy every tool effect the run's own events name, plus the ones it left in flight, and count them."""
    seen: set[str] = set()
    copied = 0
    # Recording is an upsert, so an effect a resumed run already holds is written again harmlessly.
    for effect in await source.list_unresolved_tool_effects(run_id=run_id):
        await into.record_tool_effect(effect)
        seen.add(effect.tool_call_id)
        copied += 1
    for event in await source.list_events(run_id=run_id):
        call_id = event.tool_call_id
        if call_id is None or call_id in seen:
            continue
        effect = await source.get_tool_effect(run_id=run_id, tool_call_id=call_id)
        if effect is None:
            continue
        await into.record_tool_effect(effect)
        seen.add(call_id)
        copied += 1
    return copied
