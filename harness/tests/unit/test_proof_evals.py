"""The referee in pydantic-evals' vocabulary: their nouns, groundtruth's arithmetic."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pytest

from pydantic_ai.models.test import TestModel
from pydantic_evals import Case, Dataset

from proof.evals import EvidenceRecall, SurfacedEvidence, evidence_dataset, replay, surfaced_from
from proof.groundtruth import ResearchQuestion
from proof.instrument import RecordingModel, ToolCall, ToolTrace
from proof.records import build_record

if TYPE_CHECKING:
    from proof.records import RunRecord


def _questions() -> list[ResearchQuestion]:
    return [ResearchQuestion(id=f"q.{n}", question=f"fork {n}?", sources=frozenset({f"paper.{n}a", f"paper.{n}b"})) for n in range(1, 7)]


def _record(question: ResearchQuestion, *, result: str, path: Literal["baseline", "reuse"] = "baseline") -> RunRecord:
    trace = ToolTrace(calls=[ToolCall(tool="search", args="{}", result=result)])
    return build_record(question, path=path, trace=trace, model=RecordingModel(TestModel()), seconds=1.0, failed=False, answer="")


def test_the_dataset_is_one_case_per_question_keyed_by_its_id() -> None:
    questions = _questions()
    dataset = evidence_dataset(questions)
    assert len(dataset.cases) == 6
    assert [case.name for case in dataset.cases] == [question.id for question in questions]
    assert [case.inputs for case in dataset.cases] == [question.question for question in questions]
    assert [case.metadata for case in dataset.cases] == questions
    assert dataset.cases[0].expected_output == SurfacedEvidence(surfaced=("paper.1a", "paper.1b"))
    assert [type(evaluator) for evaluator in dataset.evaluators] == [EvidenceRecall]


async def test_evidence_recall_scores_exactly_what_groundtruth_computes() -> None:
    questions = _questions()
    records = [_record(question, result=f'{{"id": "paper.{n}a"}}') for n, question in enumerate(questions, start=1)]
    task = replay(records, path="baseline", questions=questions)

    report = await evidence_dataset(questions).evaluate(task, progress=False)

    assert report.failures == []
    scored = {case.name: case for case in report.cases}
    for question, record in zip(questions, records, strict=True):
        case = scored[question.id]
        # one number, three readers: the evaluator delegates to the same arithmetic build_record used.
        assert case.scores["evidence_recall"].value == question.recall(record.surfaced) == record.recall == 0.5
        assert case.assertions["uncontaminated"].value is True
        assert case.scores["evidence_recall"].reason == f"missed: paper.{question.id.removeprefix('q.')}b"


async def test_a_withheld_question_id_disqualifies_the_run_as_build_record_marks_it() -> None:
    questions = _questions()
    records = [_record(question, result=f"q.leaked-synthesis | paper.{n}a | paper.{n}b") for n, question in enumerate(questions, start=1)]
    assert all(record.contaminated for record in records)
    assert all(record.recall == 1.0 for record in records)  # a perfect score on the arithmetic, and still disqualified
    task = replay(records, path="baseline", questions=questions)

    report = await evidence_dataset(questions).evaluate(task, progress=False)

    scored = {case.name: case for case in report.cases}
    for question in questions:
        case = scored[question.id]
        assert case.scores["evidence_recall"].value == 0.0
        assert case.assertions["uncontaminated"].value is False
        assert "q.leaked-synthesis" in (case.assertions["uncontaminated"].reason or "")


def test_the_adapter_reads_only_what_a_record_surfaced() -> None:
    question = _questions()[0]
    record = _record(question, result='{"id": "paper.1a"} -- see paper.1a#methods')
    assert surfaced_from(record) == SurfacedEvidence(surfaced=record.surfaced) == SurfacedEvidence(surfaced=("paper.1a",))
    # a producer that does not fold sections itself is still scoreable: recall folds them to their page
    assert question.recall(SurfacedEvidence(surfaced=("paper.1a#methods",)).surfaced) == 0.5


def test_replay_refuses_a_question_that_never_ran_rather_than_scoring_it_zero() -> None:
    questions = _questions()
    records = [_record(questions[0], result='{"id": "paper.1a"}'), _record(questions[1], result='{"id": "paper.2a"}', path="reuse")]
    task = replay(records, path="baseline", questions=questions)
    assert task(questions[0].question) == SurfacedEvidence(surfaced=("paper.1a",))
    # question 2 ran on the other path only; a baseline replay must refuse rather than answer with a reuse row
    with pytest.raises(LookupError, match="fork 2"):
        task(questions[1].question)


async def test_a_case_without_its_question_fails_loudly_instead_of_scoring() -> None:
    dataset = Dataset[str, SurfacedEvidence, ResearchQuestion](
        name="no-groundtruth", cases=[Case(name="q.1", inputs="fork 1?")], evaluators=[EvidenceRecall()]
    )

    report = await dataset.evaluate(lambda _question: SurfacedEvidence(surfaced=("paper.1a",)), progress=False)

    failures = report.cases[0].evaluator_failures
    assert [failure.error_type for failure in failures] == ["ValueError"]
    assert "groundtruth" in failures[0].error_message
