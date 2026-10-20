"""The evidence log file: a line it cannot read is skipped, and the skip is logged with its place and reason."""

from __future__ import annotations

import logging

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import Approval, FileEvidenceLog

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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
