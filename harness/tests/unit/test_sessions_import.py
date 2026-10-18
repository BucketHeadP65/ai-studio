"""Copying a file-store transcript into the record: what lands, and what a second pass does."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai_harness.step_persistence import (
    ContinuableSnapshot,
    FileStepStore,
    InMemoryStepStore,
    RunRecord,
    StepEvent,
    ToolEffectRecord,
)

from harness.features.sessions.importing import import_file_store

if TYPE_CHECKING:
    from pathlib import Path


async def _a_run(store: FileStepStore, *, run_id: str, conversation_id: str | None) -> None:
    """One run with an event and a snapshot, the shape the file store holds today."""
    await store.register_run(RunRecord(run_id=run_id, conversation_id=conversation_id))
    await store.append_event(StepEvent(run_id=run_id, kind="run_started", step_index=0))
    await store.save_snapshot(
        ContinuableSnapshot(
            run_id=run_id,
            step_index=0,
            messages=[ModelRequest(parts=[UserPromptPart(content="what the file store held")])],
            conversation_id=conversation_id,
        )
    )


async def test_every_run_event_and_snapshot_lands_in_the_record(tmp_path: Path) -> None:
    """What the file store held is what the record answers with afterwards."""
    conversation_id = str(uuid4())
    source = FileStepStore(tmp_path / "steps")
    await _a_run(source, run_id="r-1", conversation_id=conversation_id)
    record = InMemoryStepStore()

    report = await import_file_store(source, into=record)

    assert (report.runs, report.snapshots, report.events) == (1, 1, 1)
    snapshot = await record.latest_snapshot(run_id="r-1")
    assert snapshot is not None
    contents = [
        part.content
        for message in snapshot.messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]
    assert contents == ["what the file store held"]


async def test_a_second_pass_writes_nothing(tmp_path: Path) -> None:
    """The verb is run again after a crash, so a run already in the record is skipped by id."""
    conversation_id = str(uuid4())
    source = FileStepStore(tmp_path / "steps")
    await _a_run(source, run_id="r-1", conversation_id=conversation_id)
    record = InMemoryStepStore()
    await import_file_store(source, into=record)

    report = await import_file_store(source, into=record)

    assert (report.runs, report.snapshots, report.skipped) == (0, 0, 1)


async def test_a_run_with_no_conversation_is_left_behind(tmp_path: Path) -> None:
    """Every run in the record belongs to a conversation, so one without an id cannot enter it."""
    source = FileStepStore(tmp_path / "steps")
    await _a_run(source, run_id="r-old", conversation_id=None)
    record = InMemoryStepStore()

    report = await import_file_store(source, into=record)

    assert (report.runs, report.unnamed) == (0, 1)
    assert await record.get_run(run_id="r-old") is None


async def test_a_half_imported_run_is_resumed_not_skipped(tmp_path: Path) -> None:
    """A run the record holds with no snapshot yet stopped between registering and its first snapshot."""
    conversation_id = str(uuid4())
    source = FileStepStore(tmp_path / "steps")
    run = RunRecord(run_id="r-1", conversation_id=conversation_id)
    await source.register_run(run)
    for step_index in range(4):
        await source.append_event(
            StepEvent(run_id="r-1", kind="run_started", step_index=step_index, tool_call_id="call-1" if step_index == 1 else None)
        )
    await source.save_snapshot(
        ContinuableSnapshot(
            run_id="r-1",
            step_index=0,
            messages=[ModelRequest(parts=[UserPromptPart(content="resumed")])],
            conversation_id=conversation_id,
        )
    )
    await source.record_tool_effect(ToolEffectRecord(tool_call_id="call-1", tool_name="a_tool", run_id="r-1", status="started"))

    record = InMemoryStepStore()
    await record.register_run(run)
    for event in (await source.list_events(run_id="r-1"))[:2]:
        await record.append_event(event)

    report = await import_file_store(source, into=record)

    assert (report.resumed, report.runs, report.skipped) == (1, 0, 0)
    assert (report.events, report.snapshots, report.effects) == (2, 1, 1)
    assert len(await record.list_events(run_id="r-1")) == 4
    assert await record.latest_snapshot(run_id="r-1", include_interrupted=True) is not None

    second = await import_file_store(source, into=record)

    assert (second.skipped, second.resumed) == (1, 0)
    assert len(await record.list_events(run_id="r-1")) == 4


async def test_a_snapshot_spelled_without_hyphens_is_reconciled_to_the_runs_own_spelling(tmp_path: Path) -> None:
    """A snapshot naming the run's own conversation under a different spelling is corrected before it lands."""
    conversation_id = str(uuid4())
    source = FileStepStore(tmp_path / "steps")
    await source.register_run(RunRecord(run_id="r-1", conversation_id=conversation_id))
    await source.append_event(StepEvent(run_id="r-1", kind="run_started", step_index=0))
    await source.save_snapshot(
        ContinuableSnapshot(
            run_id="r-1",
            step_index=0,
            messages=[ModelRequest(parts=[UserPromptPart(content="spelled differently")])],
            conversation_id=conversation_id.replace("-", ""),
        )
    )
    record = InMemoryStepStore()

    report = await import_file_store(source, into=record)

    assert (report.runs, report.reconciled, report.malformed) == (1, 1, 0)
    snapshot = await record.latest_snapshot(run_id="r-1")
    assert snapshot is not None
    assert snapshot.conversation_id == conversation_id


async def test_a_snapshot_naming_a_different_conversation_leaves_the_run_behind(tmp_path: Path) -> None:
    """A snapshot naming a conversation other than its own run's is a source defect: nothing of the run is written."""
    conversation_id = str(uuid4())
    other_conversation = str(uuid4())
    source = FileStepStore(tmp_path / "steps")
    await source.register_run(RunRecord(run_id="r-1", conversation_id=conversation_id))
    await source.append_event(StepEvent(run_id="r-1", kind="run_started", step_index=0))
    await source.save_snapshot(
        ContinuableSnapshot(
            run_id="r-1",
            step_index=0,
            messages=[ModelRequest(parts=[UserPromptPart(content="wrong conversation")])],
            conversation_id=other_conversation,
        )
    )
    record = InMemoryStepStore()

    report = await import_file_store(source, into=record)

    assert (report.runs, report.malformed, report.events, report.snapshots) == (0, 1, 0, 0)
    assert await record.get_run(run_id="r-1") is None


async def test_a_run_whose_conversation_id_does_not_parse_is_left_behind(tmp_path: Path) -> None:
    """A conversation id that is not a UUID cannot enter a record whose runs table is uuid-typed."""
    source = FileStepStore(tmp_path / "steps")
    await source.register_run(RunRecord(run_id="r-1", conversation_id="not-a-uuid"))
    await source.append_event(StepEvent(run_id="r-1", kind="run_started", step_index=0))
    record = InMemoryStepStore()

    report = await import_file_store(source, into=record)

    assert (report.runs, report.malformed) == (0, 1)
    assert await record.get_run(run_id="r-1") is None
