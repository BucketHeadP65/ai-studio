"""The evidence log file: a line it cannot read is skipped, the skip is logged with its place and reason, and one scan answers a set of content addresses."""

from __future__ import annotations

import logging

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import Approval, FileEvidenceLog, Outcome, RunEvidence

if TYPE_CHECKING:
    from pathlib import Path


async def test_a_line_missing_a_required_field_is_skipped_with_a_warning_naming_the_file_line_and_field(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "evidence.jsonl"
    path.write_text('{"kind":"run","workflow_id":"wf.report","spec_hash":"abc","outcome":"completed"}\n', encoding="utf-8")
    ledger = FileEvidenceLog(path)
    await ledger.append(Approval(workflow_id="wf.report", spec_hash="abc", by="human:alex"))

    with caplog.at_level(logging.WARNING):
        events = await ledger.events_for("wf.report")

    assert [type(event).__name__ for event in events] == ["Approval"]
    (warning,) = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert f"line 1 of {path}" in warning
    assert "result" in warning
    assert "Approval" not in warning, "only the record the line's kind names explains the skip"


async def test_a_line_that_is_not_json_is_skipped_with_a_warning_naming_its_line(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "evidence.jsonl"
    ledger = FileEvidenceLog(path)
    await ledger.append(Approval(workflow_id="wf.report", spec_hash="abc", by="human:alex"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")

    with caplog.at_level(logging.WARNING):
        events = await ledger.events_for("wf.report")

    assert len(events) == 1
    (warning,) = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert f"line 2 of {path}" in warning
    assert "not JSON" in warning


async def test_a_readable_log_logs_nothing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    ledger = FileEvidenceLog(tmp_path / "evidence.jsonl")
    await ledger.append(Approval(workflow_id="wf.report", spec_hash="abc", by="human:alex"))

    with caplog.at_level(logging.WARNING):
        await ledger.events_for("wf.report")

    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


async def test_a_record_whose_values_hold_unicode_line_separators_is_read_whole(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """JSON writes U+2028, U+2029 and U+0085 unescaped inside a string, so only a newline ends a record."""
    ledger = FileEvidenceLog(tmp_path / "evidence.jsonl")
    run = RunEvidence(
        workflow_id="wf.report",
        spec_hash="abc",
        outcome="completed",
        result=Outcome(status="unknown", summary="not checked"),
        gates_auto=(),
        gates_orchestrated=(),
        params={"topic": "one\u2028two\u2029three\x85four"},
    )
    await ledger.append(run)
    await ledger.append(Approval(workflow_id="wf.report", spec_hash="abc", by="human:alex"))

    with caplog.at_level(logging.WARNING):
        events = await ledger.history_for(spec_hash="abc")

    assert events[0] == run
    assert [type(event).__name__ for event in events] == ["RunEvidence", "Approval"]
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


async def test_history_for_any_reads_the_file_once_for_two_content_addresses_in_written_order(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The unreadable line is logged once, so one scan answered both content addresses."""
    path = tmp_path / "evidence.jsonl"
    ledger = FileEvidenceLog(path)
    await ledger.append(Approval(workflow_id="wf.report.v1", spec_hash="v1", by="human:alex"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    await ledger.append(Approval(workflow_id="wf.report.v3", spec_hash="v3", by="human:alex"))
    await ledger.append(Approval(workflow_id="wf.report.v2", spec_hash="v2", by="human:sam"))
    await ledger.append(Approval(workflow_id="wf.report.v1", spec_hash="v1", by="human:sam"))

    with caplog.at_level(logging.WARNING):
        events = await ledger.history_for_any(spec_hashes={"v1", "v2"})

    assert [(event.spec_hash, event.by) for event in events if isinstance(event, Approval)] == [
        ("v1", "human:alex"),
        ("v2", "human:sam"),
        ("v1", "human:sam"),
    ]
    assert len([record for record in caplog.records if record.levelno == logging.WARNING]) == 1, "one scan, one warning"


async def test_history_for_any_of_no_content_address_is_empty_and_an_empty_one_is_refused(tmp_path: Path) -> None:
    ledger = FileEvidenceLog(tmp_path / "evidence.jsonl")
    await ledger.append(Approval(workflow_id="wf.report", spec_hash="abc", by="human:alex"))

    assert await ledger.history_for_any(spec_hashes=()) == ()
    with pytest.raises(ValueError, match="empty spec hash"):
        await ledger.history_for_any(spec_hashes=("abc", ""))
