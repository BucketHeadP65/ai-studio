"""The run record: one assembler for both paths, so neither can get private accounting."""

from __future__ import annotations

from pydantic_ai.models.test import TestModel

from proof.groundtruth import ResearchQuestion
from proof.instrument import RecordingModel, ToolCall, ToolTrace
from proof.records import RunRecord, build_record, write_record


def _question() -> ResearchQuestion:
    return ResearchQuestion(id="q.x", question="which fork?", sources=frozenset({"paper.a", "paper.b"}))


def _trace() -> ToolTrace:
    return ToolTrace(calls=[ToolCall(tool="search", args="{}", result='{"id": "paper.a"}')])


def test_build_record_scores_against_groundtruth(tmp_path) -> None:
    model = RecordingModel(TestModel())
    record = build_record(_question(), path="baseline", trace=_trace(), model=model, seconds=1.5, failed=False, answer="A.")
    assert record.recall == 0.5
    assert record.missed == ("paper.b",)
    assert record.surfaced == ("paper.a",)
    assert not record.contaminated
    assert record.at is not None, "sweeps have to be orderable"
    path = write_record(record, tmp_path)
    assert path.name == "baseline-q.x.json"
    assert RunRecord.model_validate_json(path.read_text()) == record


def test_a_reuse_record_lands_beside_the_version_it_challenges_not_over_it(tmp_path) -> None:
    """The harness is the store's standing challenger; a rematch must not erase its own evidence.

    Before the discriminator, `reuse-{qid}.json` was one slot per question: re-running the sweep
    against a new artifact version (or a new model stamp) overwrote the rows the new numbers were
    meant to be compared against -- observed live when v1's six failed rows vanished under v5's.
    """
    model = RecordingModel(TestModel())
    old = build_record(
        _question(), path="reuse", trace=_trace(), model=model, seconds=1.0, failed=True, answer="", workflow_id="wf.x.v1"
    )
    new = build_record(
        _question(), path="reuse", trace=_trace(), model=model, seconds=1.0, failed=False, answer="done", workflow_id="wf.x.v2"
    )
    first, second = write_record(old, tmp_path), write_record(new, tmp_path)
    assert first.name == "reuse-q.x--wf.x.v1.json"
    assert second.name == "reuse-q.x--wf.x.v2.json"
    assert first.exists(), "the challenged evidence survives the challenger"


def test_a_withheld_id_in_the_trace_marks_contamination() -> None:
    trace = ToolTrace(calls=[ToolCall(tool="run_sql", args="{}", result="q.other-question | leaked")])
    record = build_record(_question(), path="baseline", trace=trace, model=RecordingModel(TestModel()), seconds=1.0, failed=False, answer="")
    assert record.contaminated


def test_a_failed_run_carries_its_reason() -> None:
    """A failed row that says only `failed: true` sends the reader to a log that may not exist.

    The v4 sweep failed all six questions and left nothing behind but zeros -- logfire was not
    configured, so the tracebacks went nowhere. The reason belongs in the record itself.
    """
    model = RecordingModel(TestModel())
    record = build_record(
        _question(), path="reuse", trace=_trace(), model=model, seconds=0.1, failed=True, answer="", failure="ValidationError: 1 error"
    )
    assert record.failure == "ValidationError: 1 error"
    clean = build_record(_question(), path="reuse", trace=_trace(), model=model, seconds=0.1, failed=False, answer="done")
    assert clean.failure is None
