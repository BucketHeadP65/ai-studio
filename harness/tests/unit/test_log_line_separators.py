"""Every append-only log reads a line whose JSON string holds U+2028, U+2029 or U+0085 as one record.

JSON leaves those characters unescaped inside a string, so a reader that splits at them, as `str.splitlines` does,
reads one record as two broken lines. Each case writes two records through the log's own writer, rewrites the file
with those characters unescaped the way any JSON writer may leave them, and reads it back.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import RunLaunched

from harness.adapters.session_log import SessionLog
from harness.domain.events import SessionRenamed
from harness.features.running.infrastructure.run_events import RunEventLog
from harness.features.selfauthoring.domain import CapabilityActivated
from harness.features.selfauthoring.infrastructure.activation_log import FileActivationLog
from harness.features.sessions.domain import ProjectRegistered
from harness.features.sessions.infrastructure.projects_log import ProjectsLog

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

SEPARATED = "one\u2028two\u2029three\x85four"
"""A value holding all three characters `str.splitlines` splits at and JSON leaves unescaped."""


def _unescaped(path: Path) -> None:
    """Rewrite every line of `path` with non-ASCII characters left unescaped, and check the separators are now raw."""
    lines = [json.dumps(json.loads(line), ensure_ascii=False) for line in path.read_text(encoding="utf-8").split("\n") if line]
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    assert "\u2028" in path.read_text(encoding="utf-8")


def _run_events(path: Path) -> list[str]:
    log = RunEventLog(path)
    log.record(RunLaunched(run_id=SEPARATED, workflow_id="wf.report.v1"))
    log.record(RunLaunched(run_id="plain", workflow_id="wf.report.v1"))
    _unescaped(path)
    return [entry.event.run_id for entry in log.events()]


def _projects(path: Path) -> list[str]:
    log = ProjectsLog(path)
    log.record(ProjectRegistered(project_id="p1", root_path=SEPARATED))
    log.record(ProjectRegistered(project_id="p2", root_path="plain"))
    _unescaped(path)
    return [event.root_path for event in log.events() if isinstance(event, ProjectRegistered)]


def _activations(path: Path) -> list[str]:
    log = FileActivationLog(path)
    log.record(CapabilityActivated(name=SEPARATED, action="activate", via="cli"))
    log.record(CapabilityActivated(name="plain", action="activate", via="cli"))
    _unescaped(path)
    return [event.name for event in log.events()]


def _sessions(path: Path) -> list[str]:
    log = SessionLog(path, topics=None)
    log.record(SessionRenamed(session_id="s1", title=SEPARATED))
    log.record(SessionRenamed(session_id="s1", title="plain"))
    _unescaped(path)
    return [entry.event.title for entry in log.events() if isinstance(entry.event, SessionRenamed)]


@pytest.mark.parametrize("read_back", [_run_events, _projects, _activations, _sessions], ids=["run events", "projects", "activations", "sessions"])
def test_a_record_holding_unicode_line_separators_is_read_whole(tmp_path: Path, read_back: Callable[[Path], list[str]]) -> None:
    assert read_back(tmp_path / "log.jsonl") == [SEPARATED, "plain"]


def test_the_session_log_counts_on_past_a_last_record_holding_a_line_separator(tmp_path: Path) -> None:
    """The next number is read off the log's tail, so a split last record would hand out a number already taken."""
    path = tmp_path / "session-log.jsonl"
    log = SessionLog(path, topics=None)
    log.record(SessionRenamed(session_id="s1", title="plain"))
    log.record(SessionRenamed(session_id="s1", title=SEPARATED))
    _unescaped(path)

    third = log.record(SessionRenamed(session_id="s1", title="third"))

    assert third is not None
    assert third.seq == 3
