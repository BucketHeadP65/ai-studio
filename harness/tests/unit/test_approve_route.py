"""The approve route: a person's deliberate approval of a saved workflow, over HTTP.

Pinned here rather than left to `test_evidence.py`'s coverage of `trust_level` itself: this
is the one place an `Approval` gets appended by a caller outside the ledger's own tests, so it is
the place a wiring mistake -- the wrong spec hash, the wrong version, a request that reaches the
ledger despite an empty name -- would first show up.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING, Any

import pytest

from core.ai.workflows.contracts import Approval, FileEvidenceLog, LibraryError, Workflow, spec_identity

from harness.features.running.api_models import ApproveRequest
from harness.session import SessionPool
from tests.support.running import a_running_controller
from tests.support.runtime import EmptyLibrary, a_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.workflows.contracts import StepRegistry


class FakeStore(EmptyLibrary):
    """Answers `fetch_workflow` for whatever this test stocked it with, `LibraryError` for anything else."""

    def __init__(self, specs: dict[str, Workflow]) -> None:
        super().__init__()
        self._specs = specs

    async def fetch_workflow(self, workflow_id: str) -> Workflow:
        """The stocked spec.

        Raises:
            LibraryError: nothing was stocked under that id.
        """
        try:
            return self._specs[workflow_id]
        except KeyError as exc:
            msg = f"no such workflow: {workflow_id}"
            raise LibraryError(msg) from exc


async def _approve(specs: dict[str, Workflow], registry: StepRegistry, state: Path, by: str, workflow_id: str = "wf.report", note: str = "") -> tuple[int, dict[str, Any]]:
    """The route's own answer, over a session whose library holds `specs`."""
    pool = SessionPool(a_runtime(registry=registry, library=FakeStore(specs)))
    response = await a_running_controller(pool, state).approve_workflow(workflow_id, ApproveRequest(by=by, note=note))
    return response.status_code, json.loads(bytes(response.body))


def _ledger(state: Path) -> FileEvidenceLog:
    """The evidence log a controller built over `state` appends to."""
    return FileEvidenceLog(state / "evidence.jsonl")


async def test_an_approval_lands_in_the_ledger_the_trust_read_folds_in(
    quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path
) -> None:
    status, body = await _approve({"wf.report": quarterly_report}, registry, tmp_path, "alex")

    assert status == 200
    assert body["ok"] is True
    assert body["trust"]["level"] == "approved"
    assert body["trust"]["approved_by"] == ["alex"]

    events = await _ledger(tmp_path).events_for("wf.report")
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, Approval)
    assert event.spec_hash == spec_identity(quarterly_report)
    assert event.workflow_version == quarterly_report.version
    assert event.by == "alex"


async def test_a_second_approval_carries_both_names(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    specs = {"wf.report": quarterly_report}

    await _approve(specs, registry, tmp_path, "alex")
    status, body = await _approve(specs, registry, tmp_path, "someone-else")

    assert status == 200
    assert body["trust"]["approved_by"] == ["alex", "someone-else"]


@pytest.mark.parametrize("by", ["", "   "])
async def test_an_empty_name_is_refused_in_words(by: str, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    status, body = await _approve({"wf.report": quarterly_report}, registry, tmp_path, by)

    assert status == 400
    assert body["ok"] is False
    assert "name" in body["message"]

    events = await _ledger(tmp_path).events_for("wf.report")
    assert events == (), "a refused approval writes nothing"


async def test_an_unknown_workflow_404s(registry: StepRegistry, tmp_path: Path) -> None:
    status, body = await _approve({}, registry, tmp_path, "alex", workflow_id="wf.nowhere")

    assert status == 404
    assert body["ok"] is False
