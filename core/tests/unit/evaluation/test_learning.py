from __future__ import annotations

import asyncio
import json

from typing import TYPE_CHECKING

import pytest

from pydantic import ValidationError

from core import files
from core.ai.evaluation.contracts import (
    ComparisonPlan,
    ImprovementProposal,
    JsonLearningStore,
    LearningAttempt,
    LearningEvidence,
    LearningKey,
    LearningRecord,
    TaskDataset,
    compare_outcomes,
    content_fingerprint,
)
from core.ai.evaluation.infrastructure import file_learning
from tests.support.fakes.evaluation import PROVENANCE, FakeTaskGrader, FakeTaskRunner, identity, task_case

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
    return LearningRecord(key=LearningKey(problem="Instructions absent from evaluated evidence", surface="tool:read"))


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
    store = JsonLearningStore(tmp_path)
    initial = record().add_evidence(evidence()).add_attempt(await attempt("rejected"))
    await store.save(initial)
    loaded = await JsonLearningStore(tmp_path).load(initial.key)
    assert loaded == initial

    alternative = evidence("alternative").model_copy(
        update={"stance": "successful_alternative", "observation": "Valid repeated calls completed the task"}
    )
    contradiction = evidence("counterexample").model_copy(
        update={"stance": "contradicting", "observation": "The same behavior appears during healthy recovery"}
    )
    updated = initial.add_evidence(alternative).add_evidence(contradiction).add_attempt(await attempt("accepted", accepted=True))
    await store.save(updated)
    await store.save(initial)

    restored = await store.load(initial.key)
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
    store = JsonLearningStore(tmp_path)
    await store.save(initial)
    await store.save(initial)
    assert await store.load(initial.key) == initial


async def test_concurrent_stale_writers_merge_additions(tmp_path: Path) -> None:
    initial = record()
    stores = [JsonLearningStore(tmp_path) for _ in range(6)]
    await asyncio.gather(*(store.save(initial.add_evidence(evidence(str(index)))) for index, store in enumerate(stores)))
    loaded = await stores[0].load(initial.key)
    assert loaded is not None
    assert {row.evidence_id for row in loaded.evidence} == {str(index) for index in range(6)}


async def test_validated_load_rejects_corruption_and_identity_mismatch(tmp_path: Path) -> None:
    store = JsonLearningStore(tmp_path)
    initial = record()
    assert await store.load(initial.key) is None
    await store.save(initial)
    path = tmp_path / f"{initial.key.fingerprint}.json"
    payload = json.loads(path.read_text())
    payload["key"]["problem"] = "another problem"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="key"):
        await store.load(initial.key)
    path.write_text("{broken")
    with pytest.raises(ValidationError):
        await store.load(initial.key)


async def test_proposal_is_immutable_and_matches_evaluated_candidate() -> None:
    trial = await attempt("rejected")
    assert trial.proposal.fingerprint == content_fingerprint("candidate")
    with pytest.raises(ValidationError, match="frozen"):
        trial.proposal.content = "altered"
    with pytest.raises(ValidationError, match="fingerprint"):
        LearningAttempt(
            attempt_id="bad",
            proposal=ImprovementProposal(description="Different change", content="unevaluated"),
            evaluation=trial.evaluation,
            rationale="Wrong candidate",
        )


async def test_loading_cannot_turn_rejected_evidence_into_an_accepted_attempt() -> None:
    trial = await attempt("rejected")
    payload = trial.model_dump()
    payload["evaluation"]["verdict"] = "accepted"
    with pytest.raises(ValidationError, match="evaluation"):
        LearningAttempt.model_validate(payload)


async def test_record_constructor_refuses_duplicate_history_ids() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        LearningRecord(key=record().key, evidence=(evidence(), evidence()))
    trial = await attempt("same")
    with pytest.raises(ValidationError, match="duplicate"):
        LearningRecord(key=record().key, attempts=(trial, trial))


def test_the_learning_store_writes_through_the_shared_lock_and_replace() -> None:
    assert file_learning.exclusive_lock is files.exclusive_lock
    assert file_learning.atomic_write is files.atomic_write
    assert not hasattr(files, "atomic_replace")
    assert not hasattr(file_learning, "fcntl")
