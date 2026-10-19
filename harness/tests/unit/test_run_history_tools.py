"""`recent_runs` and `run_values`: the run archive as the chat reads it."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import Outcome

from harness.features.running.domain.ended_run import RunArchiveRecord
from harness.features.running.infrastructure.archive import FileRunArchive
from harness.features.running.tools import RECENT_RUNS_LIMIT, recent_runs, run_values
from tests.support.state import KEPT_DAYS

if TYPE_CHECKING:
    from pathlib import Path


def _record(run_id: str, started_at: str, values: dict[str, object], *, result: Outcome | None = None) -> RunArchiveRecord:
    return RunArchiveRecord(
        run_title="",
        result=result if result is not None else Outcome(status="unknown", summary="no result declared"),
        run_id=run_id,
        workflow_id="wf.research-digest.v7",
        outcome="completed",
        started_at=started_at,
        ended_at=started_at,
        runner="in_process",
        session_id="s1",
        params={"collection": "research"},
        spec={},
        values=values,
        events=[],
    )


async def test_recent_runs_lists_newest_first_with_how_it_ran_and_what_it_checked_out_as(tmp_path: Path) -> None:
    archive = FileRunArchive(tmp_path, retention_days=KEPT_DAYS)
    digested = Outcome(status="partial", summary="two of three landed", attempted=3, succeeded=2, skipped=1)
    archive.put(_record("run-a", "2026-08-26T09:00:00Z", {}, result=digested))
    archive.put(_record("run-b", "2026-08-26T10:00:00Z", {}, result=digested))

    said = await recent_runs(archive, limit=10)

    assert said.index("run-b") < said.index("run-a")
    assert "'ran' is how the execution ended, 'checked outcome' is what the work adds up to" in said
    assert (
        "wf.research-digest.v7 ran completed, checked outcome partial (2/3 succeeded, 1 skipped, 0 failed) "
        'ended 2026-08-26T10:00:00Z params {"collection": "research"}'
    ) in said


async def test_a_run_whose_outcome_is_unknown_says_so_rather_than_reading_as_a_success(tmp_path: Path) -> None:
    archive = FileRunArchive(tmp_path, retention_days=KEPT_DAYS)
    archive.put(_record("run-old", "2026-08-26T09:00:00Z", {"references": [1, 2, 3]}))

    said = await recent_runs(archive, limit=10)

    assert "ran completed, checked outcome unknown ended" in said


async def test_the_recorded_output_and_the_checked_outcome_are_two_separate_answers(tmp_path: Path) -> None:
    """A run can record a full list of values and still have done none of the work it promised."""
    archive = FileRunArchive(tmp_path, retention_days=KEPT_DAYS)
    nothing_landed = Outcome(status="failed", summary="every item failed", attempted=3, failed=3)
    archive.put(_record("run-a", "2026-08-26T09:00:00Z", {"references": [1, 2, 3]}, result=nothing_landed))

    listed = await recent_runs(archive, limit=10)
    recorded = await run_values(archive, "run-a", node="references")

    assert "ran completed, checked outcome failed (0/3 succeeded, 0 skipped, 3 failed)" in listed
    assert json.loads(recorded) == [1, 2, 3], "the output is offered whole, and is no evidence about the work"


async def test_run_values_answers_the_nodes_values_or_one_node(tmp_path: Path) -> None:
    archive = FileRunArchive(tmp_path, retention_days=KEPT_DAYS)
    references = [[{"url": "https://arxiv.org/abs/2401.00001", "why": "builds on it", "decision": "requested", "reason": "builds on it"}]]
    archive.put(_record("run-a", "2026-08-26T09:00:00Z", {"undigested": [], "references": references}))

    whole = await run_values(archive, "run-a")
    one = await run_values(archive, "run-a", node="references")
    missing = await run_values(archive, "nope")

    assert json.loads(whole) == {"undigested": [], "references": references}
    assert json.loads(one) == references
    assert missing == "No archived run 'nope', recent_runs lists the ones on file."
    assert await run_values(archive, "run-a", node="nothing") == "run-a has no node 'nothing', its nodes are: references, undigested"


async def test_a_wide_run_comes_back_whole_and_parseable(tmp_path: Path) -> None:
    """No cap of our own: the program that reads this parses it, and the spill bounds the program's output."""
    archive = FileRunArchive(tmp_path, retention_days=KEPT_DAYS)
    wide = [{"url": f"https://arxiv.org/abs/2401.{index:05d}", "why": "builds on it"} for index in range(400)]
    archive.put(_record("run-wide", "2026-08-26T09:00:00Z", {"references": wide}))

    one = await run_values(archive, "run-wide", node="references")
    whole = await run_values(archive, "run-wide")

    assert len(whole) > 12_000
    assert json.loads(one) == wide
    assert json.loads(whole) == {"references": wide}


async def test_recent_runs_lists_at_least_one_run_and_never_more_than_its_ceiling(tmp_path: Path) -> None:
    archive = FileRunArchive(tmp_path, retention_days=KEPT_DAYS)
    for index in range(RECENT_RUNS_LIMIT + 1):
        archive.put(_record(f"run-{index:03d}", f"2026-08-26T09:{index // 60:02d}:{index % 60:02d}Z", {}))

    def listed(said: str) -> int:
        return sum(line.startswith("- run-") for line in said.splitlines())

    assert listed(await recent_runs(archive, limit=0)) == 1
    assert listed(await recent_runs(archive, limit=-3)) == 1
    assert listed(await recent_runs(archive, limit=10_000)) == RECENT_RUNS_LIMIT


async def test_an_empty_archive_says_so(tmp_path: Path) -> None:
    assert await recent_runs(FileRunArchive(tmp_path, retention_days=KEPT_DAYS), limit=5) == "No archived runs yet."
