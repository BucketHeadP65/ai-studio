"""The run history plugins: by workflow, across the fleet, and one run's whole record.

Every test drives the controller's own routes over ASGI, with a `FileRunArchive` and a
`FileSpendLog` seeded under the test's own directory: the archive and the ledger are what these
routes read, and reading them back through real file adapters is the round trip under test. The one
thing that is not a plain envelope round-trip is `GET /api/runs/history/{run_id}`: it answers the
full record unwrapped, with a `graph` built from the archived spec snapshot -- the SPA's `runRecord`
client is already built expecting exactly that shape.
"""

from __future__ import annotations

import asyncio

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import pytest

from core.ai.artifacts import ArtifactRef, artifact_digest
from core.ai.evaluation.contracts import FileOwnerReviewStore, OwnerReviewRequest
from core.ai.workflows.contracts import Outcome, ReviewedArtifactRef, canonical_reviewed_value, reviewed_value_digest

from harness.features.running.domain.ended_run import ReviewedValue, RunArchiveRecord
from harness.features.running.infrastructure.archive import FileRunArchive
from harness.features.running.infrastructure.spend_log import FileSpendLog
from harness.features.running.spend import RunSpendHeader, rows_for
from tests.support.controllers import a_client_of
from tests.support.running import a_pool, a_running_controller
from tests.support.state import KEPT_DAYS
from tests.unit.test_run_spend import _response

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

    from core.ai.workflows.contracts import StepRegistry, Workflow

    from harness.session import SessionPool


async def _forever() -> None:
    await asyncio.Event().wait()


def _archive_of(state: Path) -> FileRunArchive:
    """The archive a controller built over `state` reads and writes."""
    return FileRunArchive(state / "runs" / "archive", retention_days=KEPT_DAYS)


def _ledger_of(state: Path) -> FileSpendLog:
    """The ledger a controller built over `state` reads and writes."""
    return FileSpendLog(state / "runs" / "spend")


def _reviews_of(state: Path) -> FileOwnerReviewStore:
    """The owner review store a controller built over `state` reads and writes."""
    return FileOwnerReviewStore(state / "runs" / "reviews.jsonl")


def _seed(
    state: Path,
    *,
    run_id: str,
    workflow_id: str = "wf.report",
    run_title: str = "",
    outcome: Literal["completed", "failed", "stopped"] = "completed",
    ended_at: str | None = None,
    spec: Workflow | None = None,
    values: dict[str, object] | None = None,
    conversations: dict[str, list[dict[str, object]]] | None = None,
    pinned: bool = False,
) -> RunArchiveRecord:
    now = datetime.now(UTC).isoformat()
    record = RunArchiveRecord(
        run_id=run_id,
        workflow_id=workflow_id,
        run_title=run_title,
        outcome=outcome,
        result=Outcome(status="unknown", summary="no result declared"),
        started_at=now,
        ended_at=ended_at or now,
        runner="app:harness",
        session_id="sess-1",
        params={"quarter": "Q2"},
        spec=spec.model_dump(mode="json") if spec is not None else {},
        values=values or {},
        conversations=conversations or {},
        events=[],
        pinned=pinned,
    )
    _archive_of(state).put(record)
    return record


def _client_over(pool: SessionPool, state: Path) -> httpx.AsyncClient:
    """This test's own client over the running controller alone."""
    return a_client_of(a_running_controller(pool, state))


async def test_workflow_history_lists_newest_first_and_prepends_the_live_run(registry: StepRegistry, tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _seed(tmp_path, run_id="older", ended_at=(now - timedelta(hours=2)).isoformat())
    _seed(tmp_path, run_id="newer", ended_at=(now - timedelta(hours=1)).isoformat())
    pool = a_pool(registry)

    async with _client_over(pool, tmp_path) as client:
        pool.default.active.workflow_id = "wf.report"
        pool.default.active.task = asyncio.create_task(_forever())
        try:
            answered = await client.get("/api/workflows/wf.report/history")
        finally:
            pool.default.active.task.cancel()
            await asyncio.gather(pool.default.active.task, return_exceptions=True)

        assert answered.status_code == 200
        runs = answered.json()["runs"]
        assert [run["outcome"] for run in runs[:1]] == ["live"], "the live run leads its own workflow's history"
        assert [run["run_id"] for run in runs[1:]] == ["newer", "older"], "ended runs are newest first after it"


async def test_workflow_history_on_an_unknown_workflow_is_empty(registry: StepRegistry, tmp_path: Path) -> None:
    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await client.get("/api/workflows/no-such-workflow/history")

        assert answered.status_code == 200
        assert answered.json() == {"runs": []}


async def test_workflow_history_respects_the_limit(registry: StepRegistry, tmp_path: Path) -> None:
    now = datetime.now(UTC)
    for offset, run_id in enumerate(("a", "b", "c")):
        _seed(tmp_path, run_id=run_id, ended_at=(now - timedelta(hours=offset)).isoformat())

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await client.get("/api/workflows/wf.report/history?limit=2")

        assert answered.status_code == 200
        assert len(answered.json()["runs"]) == 2


async def test_recent_runs_joins_every_workflows_archive_newest_first(registry: StepRegistry, tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _seed(tmp_path, run_id="one", workflow_id="wf.one", ended_at=(now - timedelta(hours=2)).isoformat())
    _seed(tmp_path, run_id="two", workflow_id="wf.two", ended_at=(now - timedelta(hours=1)).isoformat())

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await client.get("/api/runs/recent?limit=10")

        assert answered.status_code == 200
        assert [run["run_id"] for run in answered.json()["runs"]] == ["two", "one"]


async def test_recent_runs_envelope_never_carries_the_heavy_fields(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(
        tmp_path,
        run_id="one",
        values={"reconcile": {"net": 400}},
        conversations={"reconcile": [{"kind": "response", "parts": [{"part_kind": "text", "content": "done"}]}]},
    )

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await client.get("/api/runs/recent")

        run = answered.json()["runs"][0]
        assert "spec" not in run
        assert "values" not in run
        assert "events" not in run
        assert "conversations" not in run, "a listing row stays light -- worker conversations are read from the full record"


async def test_run_history_record_answers_the_full_record_unwrapped_with_a_graph(
    quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path
) -> None:
    _seed(
        tmp_path,
        run_id="run-1",
        spec=quarterly_report,
        values={"fetch_sales": {"quarter": "Q2", "total": 1000}, "reconcile": {"net": 400}},
        conversations={"reconcile": [{"kind": "response", "parts": [{"part_kind": "text", "content": "net is 400"}]}]},
    )

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await client.get("/api/runs/history/run-1")

        assert answered.status_code == 200
        body = answered.json()
        assert "runs" not in body, "the single-run entry answers the record unwrapped, not a listing envelope"
        assert body["run_id"] == "run-1"
        assert body["workflow_id"] == "wf.report"
        assert isinstance(body["values"]["reconcile"], str), "each node's value is rendered for reading"
        assert body["conversations"]["reconcile"][0]["parts"][0]["content"] == "net is 400", (
            "the single-run entry keeps the full worker conversation the listing rows leave out"
        )
        assert body["graph"] is not None
        node_ids = {node["id"] for node in body["graph"]["nodes"]}
        assert "fetch_sales" in node_ids, "the graph is drawn from the archived spec snapshot"
        assert body["graph"]["nodes"]  # sanity: something is actually there to draw


async def test_run_history_record_of_an_unknown_run_is_a_404(registry: StepRegistry, tmp_path: Path) -> None:
    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await client.get("/api/runs/history/no-such-run")

        assert answered.status_code == 404
        assert answered.json()["ok"] is False


async def test_pin_run_round_trips_and_an_unknown_run_is_a_404(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path, run_id="run-1")

    async with _client_over(a_pool(registry), tmp_path) as client:
        pinned = await client.patch("/api/runs/history/run-1", json={"pinned": True})
        assert pinned.status_code == 200
        assert pinned.json() == {"ok": True, "pinned": True}
        record = _archive_of(tmp_path).get("run-1")
        assert record is not None
        assert record.pinned is True

        unpinned = await client.patch("/api/runs/history/run-1", json={"pinned": False})
        assert unpinned.json() == {"ok": True, "pinned": False}

        refused = await client.patch("/api/runs/history/no-such-run", json={"pinned": True})
        assert refused.status_code == 404
        assert refused.json()["ok"] is False


async def test_delete_run_removes_it_and_an_unknown_run_is_a_404(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path, run_id="run-1")

    async with _client_over(a_pool(registry), tmp_path) as client:
        deleted = await client.delete("/api/runs/history/run-1")
        assert deleted.status_code == 200
        assert deleted.json() == {"ok": True}
        assert _archive_of(tmp_path).get("run-1") is None

        refused = await client.delete("/api/runs/history/run-1")
        assert refused.status_code == 404
        assert refused.json()["ok"] is False


async def test_an_archived_run_carries_its_spend_and_a_run_with_no_ledger_carries_none(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path, run_id="priced")
    ledger = _ledger_of(tmp_path)
    ledger.begin(
        RunSpendHeader(
            run_id="priced", workflow_id="wf.report", runner="app:harness", session_id="sess-1", started_at="2026-08-26T10:00:00+00:00"
        )
    )
    ledger.record("priced", "wf.report", rows_for("draft", [_response()]))
    _seed(tmp_path, run_id="unpriced")

    async with _client_over(a_pool(registry), tmp_path) as client:
        runs = (await client.get("/api/workflows/wf.report/history")).json()["runs"]
        record = (await client.get("/api/runs/history/priced")).json()

    by_id = {run["run_id"]: run for run in runs}
    assert by_id["priced"]["spend"]["usage"]["input_tokens"] == 1_000
    assert set(by_id["priced"]["spend"]["steps"]) == {"draft"}
    assert by_id["unpriced"]["spend"] is None
    assert record["spend"]["steps"]["draft"]["usage"]["input_tokens"] == 1_000


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])


# --- the checked outcome on the wire -----------------------------------------------------------


async def test_a_listing_row_carries_the_runs_checked_outcome(registry: StepRegistry, tmp_path: Path) -> None:
    record = _seed(tmp_path, run_id="run-1")
    _archive_of(tmp_path).put(
        record.model_copy(update={"result": Outcome(status="partial", summary="two of three landed", attempted=3, succeeded=2, skipped=1)})
    )

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await client.get("/api/runs/recent")

        (row,) = answered.json()["runs"]
        assert row["outcome"] == "completed", "how the execution ended"
        assert row["result"]["status"] == "partial", "and what the work adds up to, said apart"
        assert (row["result"]["attempted"], row["result"]["succeeded"], row["result"]["skipped"]) == (3, 2, 1)


async def test_a_live_run_reads_as_not_checked_yet_rather_than_as_missing(registry: StepRegistry, tmp_path: Path) -> None:
    pool = a_pool(registry)

    async with _client_over(pool, tmp_path) as client:
        pool.default.active.workflow_id = "wf.report"
        pool.default.active.task = asyncio.create_task(_forever())
        try:
            answered = await client.get("/api/workflows/wf.report/history")
        finally:
            pool.default.active.task.cancel()
            await asyncio.gather(pool.default.active.task, return_exceptions=True)

        (live,) = answered.json()["runs"]
        assert live["outcome"] == "live"
        assert live["result"]["status"] == "unknown"
        assert "not ended" in live["result"]["summary"]


async def test_the_full_record_carries_its_checked_outcome_and_leaves_the_reviewed_values_out(
    quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path
) -> None:
    """A reviewed value is reached by resolving a reference, never by reading the whole record."""
    record = _seed(tmp_path, run_id="run-1", spec=quarterly_report, values={"reconcile": {"net": 400}})
    canonical = canonical_reviewed_value({"net": 400})
    kept = ReviewedValue(
        ref=ReviewedArtifactRef(run_id="run-1", node_id="reconcile", turn=None, value_digest=reviewed_value_digest(canonical)),
        canonical=canonical,
    )
    _archive_of(tmp_path).put(
        record.model_copy(update={"result": Outcome(status="succeeded", summary="it published"), "reviewed": (kept,)})
    )

    async with _client_over(a_pool(registry), tmp_path) as client:
        body = (await client.get("/api/runs/history/run-1")).json()

        assert body["result"]["status"] == "succeeded"
        assert "reviewed" not in body


# --- a run's own title on the wire -------------------------------------------------------------


async def test_a_listing_row_and_the_full_record_carry_the_runs_own_title(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path, run_id="run-1", run_title="Which cache do we use?")

    async with _client_over(a_pool(registry), tmp_path) as client:
        (row,) = (await client.get("/api/runs/recent")).json()["runs"]
        (of_workflow,) = (await client.get("/api/workflows/wf.report/history")).json()["runs"]
        whole = (await client.get("/api/runs/history/run-1")).json()

        assert row["run_title"] == "Which cache do we use?"
        assert of_workflow["run_title"] == "Which cache do we use?"
        assert whole["run_title"] == "Which cache do we use?"
        assert row["session_id"] == "sess-1", "the container is still named, and it is not the title"


async def test_a_live_row_carries_the_title_the_launch_named(registry: StepRegistry, tmp_path: Path) -> None:
    pool = a_pool(registry)

    async with _client_over(pool, tmp_path) as client:
        pool.default.active.workflow_id = "wf.report"
        pool.default.active.run_title = "Which cache do we use?"
        pool.default.active.task = asyncio.create_task(_forever())
        try:
            fleet = (await client.get("/api/runs")).json()["runs"]
            (live,) = (await client.get("/api/workflows/wf.report/history")).json()["runs"]
        finally:
            pool.default.active.task.cancel()
            await asyncio.gather(pool.default.active.task, return_exceptions=True)

        assert [run["run_title"] for run in fleet] == ["Which cache do we use?"]
        assert live["run_title"] == "Which cache do we use?"


async def test_a_run_nobody_titled_carries_an_empty_title_rather_than_a_name_from_elsewhere(
    registry: StepRegistry, tmp_path: Path
) -> None:
    """Nothing here joins a session's own name onto a run: an untitled run says so and the reader decides."""
    _seed(tmp_path, run_id="run-1")

    async with _client_over(a_pool(registry), tmp_path) as client:
        (row,) = (await client.get("/api/runs/recent")).json()["runs"]

        assert row["run_title"] == ""


async def test_recent_carries_the_checked_outcome_and_its_references_and_none_of_the_heavy_fields(
    registry: StepRegistry, tmp_path: Path
) -> None:
    """The fleet's own row: enough to say what the run did and what it fixed, and nothing bulky."""
    record = _seed(
        tmp_path,
        run_id="run-1",
        values={"draft_report": {"net": 400}},
        conversations={"draft_report": [{"kind": "response", "parts": [{"part_kind": "text", "content": "done"}]}]},
    )
    reference = ArtifactRef(
        id="draft",
        label="The draft",
        run_id="run-1",
        node_id="draft_report",
        value_digest=artifact_digest({"net": 400}),
        media_type="application/json",
    )
    _archive_of(tmp_path).put(
        record.model_copy(
            update={"result": Outcome(status="succeeded", summary="it published", attempted=1, succeeded=1, artifacts=(reference,))}
        )
    )

    async with _client_over(a_pool(registry), tmp_path) as client:
        (row,) = (await client.get("/api/runs/recent")).json()["runs"]

        assert row["result"]["status"] == "succeeded"
        (artifact,) = row["result"]["artifacts"]
        assert artifact["id"] == "draft"
        assert artifact["value_digest"] == artifact_digest({"net": 400})
        assert artifact["node_id"] == "draft_report"
        assert "url" not in artifact, "a reference is an address and a digest, never a place to fetch from"
        for heavy in ("spec", "values", "events", "conversations", "reviewed"):
            assert heavy not in row


# --- owner reviews on the single-run response -------------------------------------------------


async def test_a_completed_run_nobody_reviewed_carries_an_empty_owner_reviews_list(registry: StepRegistry, tmp_path: Path) -> None:
    """No reviews reads as owner acceptance status unknown, never as rejection or silent success."""
    _seed(tmp_path, run_id="run-1")

    async with _client_over(a_pool(registry), tmp_path) as client:
        whole = (await client.get("/api/runs/history/run-1")).json()

        assert whole["owner_reviews"] == []


async def test_the_full_record_carries_every_owner_review_committed_against_it(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path, run_id="run-1")
    ref = ArtifactRef(
        id="result", label="Result", run_id="run-1", node_id="render", turn=None,
        value_digest=artifact_digest("net: 400"), media_type="text/markdown",
    )
    request = OwnerReviewRequest(review_id="r1", actor="owner", outcome="accepted", reason="Matches the criterion.", artifact_refs=(ref,))
    await _reviews_of(tmp_path).append_or_return("run-1", "app:harness", request, clock=lambda: datetime.now(UTC))

    async with _client_over(a_pool(registry), tmp_path) as client:
        whole = (await client.get("/api/runs/history/run-1")).json()

        (kept,) = whole["owner_reviews"]
        assert kept["review_id"] == "r1"
        assert kept["outcome"] == "accepted"
        assert kept["actor"] == "owner"


async def test_the_full_record_carries_every_review_in_order_and_neither_touches_the_run_s_own_facts(
    registry: StepRegistry, tmp_path: Path
) -> None:
    """Owner acceptance and checked execution are separate answers: neither is read off the other."""
    record = _seed(tmp_path, run_id="run-1")
    _archive_of(tmp_path).put(
        record.model_copy(update={"result": Outcome(status="succeeded", summary="it published", attempted=1, succeeded=1)})
    )
    ref = ArtifactRef(
        id="result", label="Result", run_id="run-1", node_id="render", turn=None,
        value_digest=artifact_digest("net: 400"), media_type="text/markdown",
    )
    store = _reviews_of(tmp_path)
    await store.append_or_return(
        "run-1",
        "app:harness",
        OwnerReviewRequest(review_id="r1", actor="owner", outcome="accepted", reason="It answers the question.", artifact_refs=(ref,)),
        clock=lambda: datetime(2026, 9, 16, 9, tzinfo=UTC),
    )
    await store.append_or_return(
        "run-1",
        "app:harness",
        OwnerReviewRequest(
            review_id="r2", actor="somebody else", outcome="rejected", reason="It drops the second figure.", artifact_refs=(ref,)
        ),
        clock=lambda: datetime(2026, 9, 16, 10, tzinfo=UTC),
    )

    async with _client_over(a_pool(registry), tmp_path) as client:
        whole = (await client.get("/api/runs/history/run-1")).json()

        assert [review["review_id"] for review in whole["owner_reviews"]] == ["r1", "r2"]
        assert [review["outcome"] for review in whole["owner_reviews"]] == ["accepted", "rejected"]
        assert [review["actor"] for review in whole["owner_reviews"]] == ["owner", "somebody else"]
        assert whole["outcome"] == "completed", "a rejection is a reading of the result, never a rewrite of how the run ended"
        assert whole["result"]["status"] == "succeeded", "nor of what the run's own facts were checked to say"


async def test_owner_reviews_never_reach_the_recent_or_workflow_listing_rows(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path, run_id="run-1")
    ref = ArtifactRef(
        id="result", label="Result", run_id="run-1", node_id="render", turn=None,
        value_digest=artifact_digest("net: 400"), media_type="text/markdown",
    )
    request = OwnerReviewRequest(review_id="r1", actor="owner", outcome="accepted", reason="Matches the criterion.", artifact_refs=(ref,))
    await _reviews_of(tmp_path).append_or_return("run-1", "app:harness", request, clock=lambda: datetime.now(UTC))

    async with _client_over(a_pool(registry), tmp_path) as client:
        (row,) = (await client.get("/api/runs/recent")).json()["runs"]
        (of_workflow,) = (await client.get("/api/workflows/wf.report/history")).json()["runs"]

        assert "owner_reviews" not in row, "the recent projection stays bounded"
        assert "owner_reviews" not in of_workflow
