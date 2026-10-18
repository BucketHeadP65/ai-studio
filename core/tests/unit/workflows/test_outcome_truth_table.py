"""The outcome classifier's release-gate truth table: one canonical case per row.

`test_outcome.py` is the classifier's exhaustive coverage; this file is the short list a release
checks -- explicit zero-input no-op, all-failed, partial, retry success, and the four ways a
required gate's provenance can fail (auto approval, a cannot-judge escalation, unknown source, a
stale digest). Every case calls `observed_outcome` directly, the same classifier the rest of the
suite exercises, so a regression here means the classifier itself moved.
"""

from __future__ import annotations

from core.ai.workflows.contracts import (
    GateDecisionFact,
    GateReview,
    MapBatch,
    ReviewedArtifactRef,
    WorkflowResultSpec,
    WorkItemFact,
    observed_outcome,
)
from core.ai.workflows.domain.vocabulary import RequiredGateCheck

RUN = "20260915T130000000000-abcdef12"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _result(**overrides: object) -> WorkflowResultSpec:
    fields: dict[str, object] = {"summary": "the digested sources", "primary_work": "digest"}
    return WorkflowResultSpec.model_validate(fields | overrides)


def _batch(*keys: str, generation: int = 0) -> MapBatch:
    return MapBatch(generation=generation, ordered_digest=DIGEST_A, items=tuple(keys))


def _fact(key: str, disposition: str, *, retry_attempts: int = 0) -> WorkItemFact:
    return WorkItemFact.model_validate(
        {"node_id": "digest", "generation": 0, "item_key": key, "disposition": disposition, "retry_attempts": retry_attempts}
    )


def _decision(*, verdict: str, source: str, digest: str = DIGEST_A, generation: int = 1) -> GateDecisionFact:
    return GateDecisionFact.model_validate(
        {"gate_id": "approve", "attempt": generation, "verdict": verdict, "source": source, "review_generation": generation, "reviewed_value_digest": digest}
    )


def _review(*, digest: str = DIGEST_A, generation: int = 1) -> GateReview:
    return GateReview(generation=generation, reviewed=ReviewedArtifactRef(run_id=RUN, node_id="draft", value_digest=digest))


# --- primary counts and classification -----------------------------------------------------------


def test_explicit_zero_input_is_a_noop() -> None:
    """A map handed nothing, on a workflow that declared empty as its own result, is a no-op -- never unknown."""
    outcome = observed_outcome(_result(empty_primary="noop"), status="completed", facts=(), primary=_batch(), decisions=(), reviews={})

    assert outcome.status == "noop"
    assert outcome.attempted == 0
    assert outcome.primary_input == 0


def test_every_item_failing_is_all_failed() -> None:
    outcome = observed_outcome(
        _result(), status="completed", facts=(_fact("a", "failed"), _fact("b", "failed")), primary=_batch("a", "b"), decisions=(), reviews={}
    )

    assert outcome.status == "failed"
    assert outcome.attempted == 2
    assert outcome.succeeded == 0
    assert outcome.failed == 2


def test_a_mix_of_dispositions_is_partial() -> None:
    outcome = observed_outcome(
        _result(),
        status="completed",
        facts=(_fact("a", "succeeded"), _fact("b", "skipped"), _fact("c", "failed")),
        primary=_batch("a", "b", "c"),
        decisions=(),
        reviews={},
    )

    assert outcome.status == "partial"
    assert (outcome.succeeded, outcome.skipped, outcome.failed) == (1, 1, 1)


def test_an_item_that_succeeded_only_after_a_retry_still_counts_as_a_plain_success() -> None:
    """The reach it took to get there is counted apart, never folded into whether it succeeded."""
    outcome = observed_outcome(
        _result(), status="completed", facts=(_fact("a", "succeeded", retry_attempts=2),), primary=_batch("a"), decisions=(), reviews={}
    )

    assert outcome.status == "succeeded"
    assert outcome.retry_attempts == 2


# --- a required gate's provenance, four ways it can fail ------------------------------------------


def test_an_auto_approval_never_satisfies_a_required_check() -> None:
    """Automode is machine evidence, never a person's, a judge's, or an orchestrator's acceptance."""
    outcome = observed_outcome(
        _result(required_gates=(RequiredGateCheck(gate_id="approve", accepted_sources=("person",)),)),
        status="completed",
        facts=(_fact("a", "succeeded"),),
        primary=_batch("a"),
        decisions=(_decision(verdict="approve", source="auto"),),
        reviews={"approve": _review()},
    )

    assert outcome.status == "unknown"
    (check,) = outcome.checks
    assert check.passed is False
    assert check.observed_source == "auto"


def test_a_cannot_judge_escalation_to_a_person_does_not_satisfy_a_judge_only_check() -> None:
    """The person's decision is the one on record; a judge-only check cannot borrow it."""
    outcome = observed_outcome(
        _result(required_gates=(RequiredGateCheck(gate_id="approve", accepted_sources=("judge",)),)),
        status="completed",
        facts=(_fact("a", "succeeded"),),
        primary=_batch("a"),
        decisions=(_decision(verdict="cannot_judge", source="judge"), _decision(verdict="approve", source="person")),
        reviews={"approve": _review()},
    )

    assert outcome.status == "unknown"
    (check,) = outcome.checks
    assert check.passed is False
    assert check.observed_source == "person"


def test_a_reviewable_value_with_no_decision_on_file_reads_as_unknown_provenance() -> None:
    """A value the gate could read back but never received a decision over cannot pass any check."""
    outcome = observed_outcome(
        _result(required_gates=(RequiredGateCheck(gate_id="approve", accepted_sources=("judge", "person", "orchestrator")),)),
        status="completed",
        facts=(_fact("a", "succeeded"),),
        primary=_batch("a"),
        decisions=(),
        reviews={"approve": _review()},
    )

    assert outcome.status == "unknown"
    (check,) = outcome.checks
    assert check.passed is False
    assert check.observed_source == "unknown"


def test_an_approval_of_a_stale_digest_does_not_authorize_the_current_value() -> None:
    """Approval of draft A cannot authorize draft B after a rewind, even from an otherwise accepted source."""
    outcome = observed_outcome(
        _result(required_gates=(RequiredGateCheck(gate_id="approve", accepted_sources=("person",)),)),
        status="completed",
        facts=(_fact("a", "succeeded"),),
        primary=_batch("a"),
        decisions=(_decision(verdict="approve", source="person", digest=DIGEST_A),),
        reviews={"approve": _review(digest=DIGEST_B)},
    )

    assert outcome.status == "unknown"
    (check,) = outcome.checks
    assert check.passed is False
    assert "other bytes" in check.detail
