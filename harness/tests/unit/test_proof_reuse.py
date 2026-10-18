"""Path B's honesty rules: no fabricated approvals, grades keyed to what actually ran."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from core.ai.workflows.contracts import (
    FileEvidenceLog,
    FromNode,
    FromParameter,
    Grade,
    LibraryCollection,
    ParameterSpec,
    RunEvidence,
    RunNode,
    SavedWorkflow,
    StepRegistry,
    Workflow,
)
from pydantic_ai.models.test import TestModel

from proof.groundtruth import ResearchQuestion
from proof.instrument import RecordingModel, ToolCall, ToolTrace
from proof.reuse import RefusingApproval, hit_rank, run_reuse

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


async def test_the_harness_never_fabricates_a_human_approval() -> None:
    with pytest.raises(RuntimeError, match="fabricate"):
        await RefusingApproval().review("approve", value="anything")


def test_hit_rank_is_one_based_and_none_when_absent() -> None:
    hits = (SavedWorkflow(id="wf.other.v1", title="", summary=""), SavedWorkflow(id="wf.research.v1", title="", summary=""))
    assert hit_rank(hits, "wf.research.v1") == 2
    assert hit_rank(hits, "wf.absent.v1") is None


class _FakeRows:
    """The two asyncpg calls `LibraryCollection`'s read side needs, answered from a script."""

    def __init__(self, *, rows: Sequence[Mapping[str, Any]] = (), value: str | Mapping[str, Any] | None = None) -> None:
        self.rows = rows
        self.value = value

    async def fetch(self, sql: str, /, *args: object) -> Sequence[Mapping[str, Any]]:
        del sql, args
        return self.rows

    async def fetchval(self, sql: str, /, *args: object) -> str | Mapping[str, Any] | None:
        del sql, args
        return self.value


class _FakeIngest:
    """`run_reuse` never saves anything, so a call here is a bug -- not a fixture to script."""

    async def __call__(self, payload: Mapping[str, Any], *, collection: str) -> None:
        msg = f"run_reuse must never write ({collection!r}, {payload!r})"
        raise AssertionError(msg)


def _tiny_spec() -> Workflow:
    """One registered step, no gate -- exercises attest+grade without ever reaching approval."""
    return Workflow(
        name="tiny-reuse-probe",
        version=1,
        description="One registered step, no gate -- exercises attest and grade without touching approval.",
        parameters={"quarter": ParameterSpec(description="Fiscal quarter", json_schema={"type": "string"})},
        nodes=(RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),),
        output=FromNode(node="fetch_sales"),
    )


async def test_a_completed_reuse_writes_one_evidence_record_and_one_grade_sharing_spec_hash(
    tmp_path: Path, registry: StepRegistry
) -> None:
    """Copies `test_evidence.py`'s launched-run arrangement: a real run against a tmp ledger."""
    spec = _tiny_spec()
    workflow_id = "wf.tiny-reuse-probe.v1"
    frontmatter = {"id": workflow_id, "type": "workflow", "spec": spec.model_dump(mode="json")}
    rows = _FakeRows(
        rows=[
            {
                "id": workflow_id,
                "title": "Tiny reuse probe",
                "summary": "",
                "status": "verified",
                "superseded": False,
                "score": 1.0,
                "applicability": "",
                "presentation": None,
                "intent": None,
            }
        ],
        value=frontmatter,
    )
    store = LibraryCollection(rows, _FakeIngest())
    question = ResearchQuestion(id="q.x", question="Q4", sources=frozenset())
    ledger = FileEvidenceLog(tmp_path / "ledger.jsonl")

    record = await run_reuse(
        question,
        store=store,
        registry=registry,
        trace=ToolTrace(),
        recording=RecordingModel(TestModel()),
        custom=tmp_path / "custom",
        workflow_id=workflow_id,
        ledger=ledger,
    )

    assert not record.failed
    assert record.hit_rank == 1
    assert record.drift is None

    events = await ledger.events_for(workflow_id)
    evidence_records = [event for event in events if isinstance(event, RunEvidence)]
    grades = [event for event in events if isinstance(event, Grade)]
    assert [evidence_record.outcome for evidence_record in evidence_records] == ["completed"]
    assert len(grades) == 1
    assert grades[0].spec_hash == evidence_records[0].spec_hash
    assert grades[0].graded_run_at == evidence_records[0].at


def _probe_store(spec: Workflow, workflow_id: str) -> LibraryCollection:
    frontmatter = {"id": workflow_id, "type": "workflow", "spec": spec.model_dump(mode="json")}
    rows = _FakeRows(
        rows=[
            {
                "id": workflow_id,
                "title": "Tiny reuse probe",
                "summary": "",
                "status": "verified",
                "superseded": False,
                "score": 1.0,
                "applicability": "",
                "presentation": None,
                "intent": None,
            }
        ],
        value=frontmatter,
    )
    return LibraryCollection(rows, _FakeIngest())


async def test_a_failed_run_writes_a_failed_evidence_record_and_no_grade(tmp_path: Path) -> None:
    """The negative half of the Grade rule: a Grade is written only for a COMPLETED run.

    A failed run is a row and a failed evidence record -- never a score. Scoring what did not finish
    would let an artifact's mean climb on the back of runs that produced nothing.
    """
    broken = StepRegistry()

    async def fetch_sales(quarter: str) -> str:
        del quarter
        msg = "the knowledge base went away mid-run"
        raise RuntimeError(msg)

    broken.register(fetch_sales)
    workflow_id = "wf.tiny-reuse-probe.v1"
    ledger = FileEvidenceLog(tmp_path / "ledger.jsonl")

    record = await run_reuse(
        ResearchQuestion(id="q.x", question="Q4", sources=frozenset()),
        store=_probe_store(_tiny_spec(), workflow_id),
        registry=broken,
        trace=ToolTrace(),
        recording=RecordingModel(TestModel()),
        custom=tmp_path / "custom",
        workflow_id=workflow_id,
        ledger=ledger,
    )

    assert record.failed
    assert record.failure is not None
    assert "knowledge base went away" in record.failure, "the reason rides the record, not a log that may not exist"
    events = await ledger.events_for(workflow_id)
    assert [event.outcome for event in events if isinstance(event, RunEvidence)] == ["failed"]
    assert [event for event in events if isinstance(event, Grade)] == [], "failure is a row, never a score"


async def test_a_contaminated_run_completes_but_earns_no_grade(tmp_path: Path, registry: StepRegistry) -> None:
    """The other precondition: UNCONTAMINATED. A withheld id in the trace disqualifies the score.

    The run itself completed and its evidence record says so -- history is honest -- but a recall
    computed over a trace that touched a withheld question page would be a number earned by
    cheating, and the ledger must not hold it.
    """
    workflow_id = "wf.tiny-reuse-probe.v1"
    ledger = FileEvidenceLog(tmp_path / "ledger.jsonl")
    tainted = ToolTrace(calls=[ToolCall(tool="run_sql", args="{}", result="q.other-question | leaked")])

    record = await run_reuse(
        ResearchQuestion(id="q.x", question="Q4", sources=frozenset()),
        store=_probe_store(_tiny_spec(), workflow_id),
        registry=registry,
        trace=tainted,
        recording=RecordingModel(TestModel()),
        custom=tmp_path / "custom",
        workflow_id=workflow_id,
        ledger=ledger,
    )

    assert not record.failed
    assert record.contaminated
    events = await ledger.events_for(workflow_id)
    assert [event.outcome for event in events if isinstance(event, RunEvidence)] == ["completed"]
    assert [event for event in events if isinstance(event, Grade)] == [], "a contaminated run is attested, never graded"
