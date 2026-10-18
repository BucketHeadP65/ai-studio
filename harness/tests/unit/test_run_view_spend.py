"""The live run card: its spend read off the session's own active run id.

Reuses `test_run_history_routes.py`'s `_forever` and its client helper -- the same harness the
history plugins are tested against -- rather than a second copy of either.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from harness.features.running.spend import RunSpendHeader, rows_for
from tests.support.running import a_pool
from tests.unit.test_run_history_routes import _client_over, _forever, _ledger_of
from tests.unit.test_run_spend import _response

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.workflows.contracts import StepRegistry


async def test_a_live_run_card_reads_its_ledger_by_the_active_run_id(registry: StepRegistry, tmp_path: Path) -> None:
    ledger = _ledger_of(tmp_path)
    ledger.begin(
        RunSpendHeader(
            run_id="live-1", workflow_id="wf.report", runner="app:harness", session_id="s", started_at="2026-08-26T10:00:00+00:00"
        )
    )
    ledger.record("live-1", "wf.report", rows_for("draft", [_response()]))
    pool = a_pool(registry)

    async with _client_over(pool, tmp_path) as client:
        pool.default.active.workflow_id = "wf.report"
        pool.default.active.run_id = "live-1"
        pool.default.active.task = asyncio.create_task(_forever())
        try:
            runs = (await client.get("/api/runs")).json()["runs"]
        finally:
            pool.default.active.task.cancel()
            await asyncio.gather(pool.default.active.task, return_exceptions=True)

    assert runs[0]["spend"]["usage"]["input_tokens"] == 1_000
