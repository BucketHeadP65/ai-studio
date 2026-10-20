"""What a run actually did, classified from facts rather than from anything it says about itself.

Every case here is the classifier alone: item facts, the active key set, decision facts and the
execution status go in, one `Outcome` comes out. Nothing reads a report counter, and a non-empty
value is never treated as evidence that useful work happened.
"""

from __future__ import annotations

import pytest

from core.ai.artifacts import ArtifactRef
from core.ai.workflows.contracts import (
    GateDecisionFact,
    GateReview,
    MapBatch,
    Outcome,
    RetryFeedback,
    ReviewedArtifactRef,
    WorkflowResultSpec,
    WorkItemFact,
    observed_outcome,
)
from core.ai.workflows.domain.outcome import AcceptedSource, OutcomeCheck
from core.ai.workflows.domain.vocabulary import RequiredGateCheck

RUN = "20260915T120000000000-abcdef12"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _result(**overrides: object) -> WorkflowResultSpec:
    """The declaration under test: one primary map, no gates, no artifacts unless a case adds them."""
    fields: dict[str, object] = {"summary": "the digested sources", "primary_work": "digest"}
    return WorkflowResultSpec.model_validate(fields | overrides)


def _batch(*keys: str, generation: int = 0) -> MapBatch:
    return MapBatch(generation=generation, ordered_digest=DIGEST_A, items=tuple(keys))


def _fact(key: str, disposition: str, *, generation: int = 0, retry_attempts: int = 0, node_id: str = "digest") -> WorkItemFact:
    return WorkItemFact.model_validate(
        {
            "node_id": node_id,
            "generation": generation,
            "item_key": key,
            "disposition": disposition,
            "retry_attempts": retry_attempts,
        }
    )


def _approved(gate_id: str, *, generation: int = 1, digest: str = DIGEST_A, source: str = "person") -> GateDecisionFact:
    return GateDecisionFact.model_validate(
        {
            "gate_id": gate_id,
            "attempt": generation,
            "verdict": "approve",
            "source": source,
            "review_generation": generation,
            "reviewed_value_digest": digest,
        }
    )


def _review(*, generation: int = 1, digest: str = DIGEST_A) -> GateReview:
    return GateReview(generation=generation, reviewed=ReviewedArtifactRef(run_id=RUN, node_id="draft", value_digest=digest))


def _gate_result(sources: tuple[AcceptedSource, ...] = ("person",)) -> WorkflowResultSpec:
    return _result(required_gates=(RequiredGateCheck(gate_id="approve", accepted_sources=sources),))


# --- the truth table, row by row -------------------------------------------------------------


def test_every_item_succeeding_under_a_passing_gate_is_a_succeeded_result() -> None:
    outcome = observed_outcome(
        _gate_result(),
        status="completed",
        facts=[_fact("0:0:aaaa", "succeeded"), _fact("0:1:bbbb", "succeeded")],
        primary=_batch("0:0:aaaa", "0:1:bbbb"),
        decisions=[_approved("approve")],
        reviews={"approve": _review()},
    )

    assert outcome.status == "succeeded"
    assert (outcome.attempted, outcome.succeeded, outcome.skipped, outcome.failed) == (2, 2, 0, 0)
    assert outcome.primary_input == 2
    assert outcome.attempted == outcome.succeeded + outcome.skipped + outcome.failed


def test_an_empty_primary_is_a_noop_only_where_the_workflow_declared_one() -> None:
    declared = observed_outcome(
        _result(empty_primary="noop"), status="completed", facts=[], primary=_batch(), decisions=[], reviews={}
    )
    undeclared = observed_outcome(_result(), status="completed", facts=[], primary=_batch(), decisions=[], reviews={})

    assert declared.status == "noop"
    assert undeclared.status == "unknown"
    assert declared.attempted == 0
    assert declared.primary_input == 0


def test_one_skipped_item_beside_two_successes_is_partial() -> None:
    outcome = observed_outcome(
        _result(),
        status="completed",
        facts=[_fact("0:0:a", "succeeded"), _fact("0:1:b", "succeeded"), _fact("0:2:c", "skipped")],
        primary=_batch("0:0:a", "0:1:b", "0:2:c"),
        decisions=[],
        reviews={},
    )

    assert outcome.status == "partial"
    assert (outcome.attempted, outcome.succeeded, outcome.skipped, outcome.failed) == (3, 2, 1, 0)
    assert outcome.attempted == outcome.succeeded + outcome.skipped + outcome.failed


def test_nothing_succeeding_is_failed_however_the_items_were_lost() -> None:
    outcome = observed_outcome(
        _result(),
        status="completed",
        facts=[_fact("0:0:a", "failed"), _fact("0:1:b", "skipped"), _fact("0:2:c", "skipped")],
        primary=_batch("0:0:a", "0:1:b", "0:2:c"),
        decisions=[],
        reviews={},
    )

    assert outcome.status == "failed"
    assert (outcome.attempted, outcome.succeeded, outcome.skipped, outcome.failed) == (3, 0, 2, 1)


def test_an_item_that_succeeded_after_a_rewind_succeeds_and_counts_its_retry_separately() -> None:
    outcome = observed_outcome(
        _result(),
        status="completed",
        facts=[_fact("0:0:a", "succeeded", retry_attempts=1)],
        primary=_batch("0:0:a"),
        decisions=[],
        reviews={},
    )

    assert outcome.status == "succeeded"
    assert outcome.retry_attempts == 1
    assert outcome.attempted == outcome.succeeded + outcome.skipped + outcome.failed == 1


@pytest.mark.parametrize("source", ["auto", "orchestrator"])
def test_an_approval_from_a_source_the_result_does_not_accept_leaves_the_outcome_unknown(source: str) -> None:
    outcome = observed_outcome(
        _gate_result(),
        status="completed",
        facts=[_fact("0:0:a", "succeeded")],
        primary=_batch("0:0:a"),
        decisions=[_approved("approve", source=source)],
        reviews={"approve": _review()},
    )

    check = outcome.checks[0]
    assert outcome.status == "unknown"
    assert check.passed is False
    assert check.observed_source == source
    assert check.expected_sources == ("person",)


def test_a_spent_retry_ceiling_after_the_primary_settled_fails_and_carries_the_whole_repair() -> None:
    reviewed = ReviewedArtifactRef(run_id=RUN, node_id="draft", value_digest=DIGEST_B)
    refused = RetryFeedback(
        gate_id="approve",
        source="person",
        target_node="draft",
        attempt=3,
        feedback="the third paragraph still cites nothing",
        reviewed=reviewed,
        requested_repair="cite the source for every claim in paragraph three",
    )

    outcome = observed_outcome(
        _gate_result(),
        status="failed",
        facts=[_fact("0:0:a", "succeeded")],
        primary=_batch("0:0:a"),
        decisions=[_approved("approve", generation=3, digest=DIGEST_B).model_copy(update={"verdict": "reject"})],
        reviews={"approve": _review(generation=3, digest=DIGEST_B)},
        exhausted=refused,
    )

    check = outcome.checks[0]
    assert outcome.status == "failed"
    assert check.passed is False
    assert check.observed_source == "person"
    assert check.review_generation == 3
    assert check.reviewed_value_digest == DIGEST_B
    assert check.reviewed == reviewed
    assert check.feedback == "the third paragraph still cites nothing"
    assert check.requested_repair == "cite the source for every claim in paragraph three"


def test_a_generation_still_missing_one_item_is_unknown_however_well_the_rest_went() -> None:
    outcome = observed_outcome(
        _result(),
        status="completed",
        facts=[_fact("0:0:a", "succeeded")],
        primary=_batch("0:0:a", "0:1:b"),
        decisions=[],
        reviews={},
    )

    assert outcome.status == "unknown"
    assert (outcome.attempted, outcome.succeeded, outcome.primary_input) == (1, 1, 2), "what settled is still reported"


def test_a_run_that_failed_before_its_primary_took_an_input_is_unknown() -> None:
    outcome = observed_outcome(_result(), status="failed", facts=[], primary=None, decisions=[], reviews={})

    assert outcome.status == "unknown"
    assert outcome.primary_input is None


def test_a_workflow_that_declares_no_result_is_unknown() -> None:
    outcome = observed_outcome(None, status="completed", facts=[], primary=None, decisions=[], reviews={})

    assert outcome.status == "unknown"


# --- the rules the table implies ---------------------------------------------------------------


def test_facts_of_another_generation_are_audit_and_never_counted() -> None:
    outcome = observed_outcome(
        _result(),
        status="completed",
        facts=[_fact("0:0:a", "failed", generation=0), _fact("1:0:a", "succeeded", generation=1)],
        primary=_batch("1:0:a", generation=1),
        decisions=[],
        reviews={},
    )

    assert outcome.status == "succeeded"
    assert outcome.generation == 1
    assert outcome.attempted == 1


def test_facts_of_another_node_are_not_this_primarys_work() -> None:
    outcome = observed_outcome(
        _result(),
        status="completed",
        facts=[_fact("0:0:a", "succeeded"), _fact("0:0:a", "failed", node_id="render")],
        primary=_batch("0:0:a"),
        decisions=[],
        reviews={},
    )

    assert outcome.status == "succeeded"
    assert outcome.attempted == 1


def test_two_facts_disagreeing_about_one_item_settle_nothing() -> None:
    outcome = observed_outcome(
        _result(),
        status="completed",
        facts=[_fact("0:0:a", "succeeded"), _fact("0:0:a", "failed")],
        primary=_batch("0:0:a"),
        decisions=[],
        reviews={},
    )

    assert outcome.status == "unknown"
    assert "disagree" in outcome.summary


def test_two_facts_agreeing_about_one_item_are_one_disposition() -> None:
    outcome = observed_outcome(
        _result(),
        status="completed",
        facts=[_fact("0:0:a", "succeeded"), _fact("0:0:a", "succeeded")],
        primary=_batch("0:0:a"),
        decisions=[],
        reviews={},
    )

    assert outcome.status == "succeeded"
    assert outcome.attempted == 1


def test_a_run_node_primary_settles_under_generation_zero_and_the_empty_item_key() -> None:
    outcome = observed_outcome(
        _result(),
        status="completed",
        facts=[_fact("", "succeeded")],
        primary=_batch("", generation=0),
        decisions=[],
        reviews={},
    )

    assert outcome.status == "succeeded"
    assert outcome.attempted == 1
    assert outcome.primary_input == 1


def test_an_execution_that_did_not_complete_reports_its_counts_and_claims_nothing() -> None:
    outcome = observed_outcome(
        _result(), status="stopped", facts=[_fact("0:0:a", "succeeded")], primary=_batch("0:0:a"), decisions=[], reviews={}
    )

    assert outcome.status == "unknown"
    assert (outcome.attempted, outcome.succeeded) == (1, 1), "what settled is still reported"


def test_declared_artifacts_ride_with_the_outcome() -> None:
    ref = ArtifactRef(
        id="report", label="The report", run_id=RUN, node_id="draft", value_digest=DIGEST_A, media_type="text/markdown"
    )
    outcome = observed_outcome(
        _result(),
        status="completed",
        facts=[_fact("0:0:a", "succeeded")],
        primary=_batch("0:0:a"),
        decisions=[],
        reviews={},
        artifacts=[ref],
    )

    assert outcome.artifacts == (ref,)


# --- required gates ----------------------------------------------------------------------------


def test_a_gate_with_no_decision_at_all_reads_as_an_unknown_source() -> None:
    outcome = observed_outcome(
        _gate_result(),
        status="completed",
        facts=[_fact("0:0:a", "succeeded")],
        primary=_batch("0:0:a"),
        decisions=[],
        reviews={"approve": _review()},
    )

    check = outcome.checks[0]
    assert outcome.status == "unknown"
    assert check.observed_source == "unknown"
    assert check.review_generation is None
    assert check.reviewed_value_digest is None


def test_an_approval_of_an_earlier_generation_releases_nothing_for_the_current_one() -> None:
    """Identical bytes across two generations: the approval still belongs to the reach it was given over."""
    outcome = observed_outcome(
        _gate_result(),
        status="completed",
        facts=[_fact("0:0:a", "succeeded")],
        primary=_batch("0:0:a"),
        decisions=[_approved("approve", generation=1, digest=DIGEST_A)],
        reviews={"approve": _review(generation=2, digest=DIGEST_A)},
    )

    assert outcome.status == "unknown"
    assert outcome.checks[0].passed is False


def test_an_approval_of_other_bytes_at_this_generation_releases_nothing() -> None:
    outcome = observed_outcome(
        _gate_result(),
        status="completed",
        facts=[_fact("0:0:a", "succeeded")],
        primary=_batch("0:0:a"),
        decisions=[_approved("approve", generation=2, digest=DIGEST_A)],
        reviews={"approve": _review(generation=2, digest=DIGEST_B)},
    )

    assert outcome.status == "unknown"
    assert outcome.checks[0].passed is False


def test_a_judge_that_could_not_judge_and_a_person_who_then_approved_is_the_persons_decision() -> None:
    outcome = observed_outcome(
        _gate_result(("judge", "person")),
        status="completed",
        facts=[_fact("0:0:a", "succeeded")],
        primary=_batch("0:0:a"),
        decisions=[
            _approved("approve", generation=1, source="judge").model_copy(update={"verdict": "cannot_judge"}),
            _approved("approve", generation=1, source="person"),
        ],
        reviews={"approve": _review(generation=1)},
    )

    check = outcome.checks[0]
    assert outcome.status == "succeeded"
    assert check.passed is True
    assert check.observed_source == "person"


def test_a_gate_whose_reviewed_value_cannot_be_read_back_passes_nothing() -> None:
    outcome = observed_outcome(
        _gate_result(),
        status="completed",
        facts=[_fact("0:0:a", "succeeded")],
        primary=_batch("0:0:a"),
        decisions=[_approved("approve")],
        reviews={"approve": GateReview(generation=1, reviewed=None)},
    )

    assert outcome.status == "unknown"
    assert outcome.checks[0].passed is False


def test_every_required_gate_gets_its_own_check_in_declaration_order() -> None:
    result = _result(
        required_gates=(
            RequiredGateCheck(gate_id="approve", accepted_sources=("person",)),
            RequiredGateCheck(gate_id="sign_off", accepted_sources=("orchestrator",)),
        )
    )
    outcome = observed_outcome(
        result,
        status="completed",
        facts=[_fact("0:0:a", "succeeded")],
        primary=_batch("0:0:a"),
        decisions=[_approved("approve"), _approved("sign_off", source="orchestrator")],
        reviews={"approve": _review(), "sign_off": _review()},
    )

    assert [check.id for check in outcome.checks] == ["approve", "sign_off"]
    assert [check.passed for check in outcome.checks] == [True, True]
    assert outcome.status == "succeeded"


def test_a_check_is_a_closed_frozen_record() -> None:
    check = OutcomeCheck(
        id="approve", passed=True, expected_sources=("person",), observed_source="person", detail="a person approved it"
    )

    with pytest.raises(ValueError, match="frozen"):
        check.id = "other"  # type: ignore[misc]
    with pytest.raises(ValueError, match="Extra inputs"):
        OutcomeCheck.model_validate({**check.model_dump(), "who": "nobody"})
    assert check.kind == "gate"
    assert check.required is True


def test_an_outcome_needs_only_its_status_and_summary() -> None:
    outcome = Outcome(status="unknown", summary="Nothing was checked.")

    assert (outcome.attempted, outcome.succeeded, outcome.skipped, outcome.failed, outcome.retry_attempts) == (0, 0, 0, 0, 0)
    assert outcome.checks == ()
    assert outcome.artifacts == ()
    assert outcome.primary_input is None
