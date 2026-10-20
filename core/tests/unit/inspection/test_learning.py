from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from pydantic import ValidationError

from core.ai.evaluation.contracts import (
    ComparisonPlan,
    ImprovementProposal,
    JsonLearningStore,
    LearningEvidence,
    LearningKey,
    TaskDataset,
    compare_outcomes,
)
from core.ai.inspection.domain.learning import LearningAttempt, LearningRecord
from tests.support.fakes.inspection_evaluation import PROVENANCE, FakeTaskGrader, FakeTaskRunner, identity, task_case

if TYPE_CHECKING:
    from pathlib import Path


def evidence(evidence_id: str = "finding-1") -> LearningEvidence:
    return LearningEvidence(
        evidence_id=evidence_id,
        source=f"record:{evidence_id}",
        observation="Repeated retry despite readable instructions",
        stance="supporting",
        provenance=PROVENANCE,
    )


def record() -> LearningRecord:
    return LearningRecord(key=LearningKey(problem="Instructions absent from inspector evidence", surface="tool:read"))


def store(directory: Path) -> JsonLearningStore:
    return JsonLearningStore(directory, record_type=LearningRecord)


async def attempt(attempt_id: str, *, accepted: bool = False) -> LearningAttempt:
    plan = ComparisonPlan(
        dataset=TaskDataset(name="learning", version="1", cases=(task_case("held-out"),)), evaluation_case_ids=("held-out",), repetitions=2
    )
    outputs = {("candidate", "held-out"): "success"} if accepted else {}
    evaluation = await compare_outcomes(
        plan, FakeTaskRunner(outputs), FakeTaskGrader(), baseline=identity("baseline"), candidate=identity("candidate")
    )
    return LearningAttempt(
        attempt_id=attempt_id,
        proposal=ImprovementProposal(description="Expose instructions", content="candidate"),
        evaluation=evaluation,
        rationale="Measured on independent task outcomes",
    )


async def test_roundtrip_accumulates_evidence_and_keeps_rejected_attempts(tmp_path: Path) -> None:
    initial = record().add_evidence(evidence()).add_attempt(await attempt("rejected"))
    await store(tmp_path).save(initial)
    loaded = await store(tmp_path).load(initial.key)
    assert loaded == initial

    alternative = evidence("alternative").model_copy(
        update={"stance": "successful_alternative", "observation": "Valid repeated calls completed the task"}
    )
    contradiction = evidence("counterexample").model_copy(
        update={"stance": "contradicting", "observation": "The same symptom appears during healthy recovery"}
    )
    updated = initial.add_evidence(alternative).add_evidence(contradiction).add_attempt(await attempt("accepted", accepted=True))
    await store(tmp_path).save(updated)
    await store(tmp_path).save(initial)

    restored = await store(tmp_path).load(initial.key)
    assert restored is not None
    assert restored == updated
    assert [row.evaluation.verdict for row in restored.attempts] == ["rejected", "accepted"]
    assert len(restored.evidence) == 3


async def test_repeated_evidence_is_idempotent_but_conflicting_history_is_refused(tmp_path: Path) -> None:
    initial = record().add_evidence(evidence())
    assert initial.add_evidence(evidence()) == initial
    with pytest.raises(ValueError, match="immutable"):
        initial.add_evidence(evidence().model_copy(update={"observation": "rewritten"}))
    trial = await attempt("attempt")
    assert initial.add_attempt(trial).add_attempt(trial).attempts == (trial,)
    with pytest.raises(ValueError, match="immutable"):
        initial.add_attempt(trial).add_attempt(trial.model_copy(update={"rationale": "changed"}))
    learning_store = store(tmp_path)
    await learning_store.save(initial)
    await learning_store.save(initial)
    assert await learning_store.load(initial.key) == initial


async def test_a_record_whose_execution_rows_lack_diagnostics_is_refused(tmp_path: Path) -> None:
    """Every execution row carries its diagnostics. A row without them is not a record this store reads."""
    trial = await attempt("without-diagnostics")
    base = record().add_attempt(trial)
    payload = base.model_dump(mode="json")
    for attempt_row in payload["attempts"]:
        for trial_row in attempt_row["evaluation"]["trials"]:
            execution = trial_row["execution"]
            if execution is not None:
                del execution["diagnostics"]
    (tmp_path / f"{base.key.fingerprint}.json").write_text(json.dumps(payload))

    with pytest.raises(ValidationError, match="diagnostics"):
        await store(tmp_path).load(base.key)
