from __future__ import annotations

import asyncio

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import pytest

from pydantic import ValidationError

from core.ai.artifacts import ArtifactDocument, ArtifactRef, ArtifactUnavailableError, artifact_digest
from core.ai.evaluation.contracts import (
    Assessment,
    EvaluationLimits,
    FileOwnerReviewStore,
    OwnerReviewConflictError,
    OwnerReviewRequest,
    OwnerReviewService,
    ReviewedTaskOutcome,
    TaskOutcome,
)
from core.ai.evaluation.domain.reviews import OwnerReviewResult, owner_review_request_fingerprint
from tests.support.fakes.artifacts import FakeArtifactResolver
from tests.support.fakes.evaluation import PROVENANCE

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

RUN = "run-1"


def _ref(**overrides: object) -> ArtifactRef:
    fields: dict[str, object] = {
        "id": "result",
        "label": "Result",
        "run_id": RUN,
        "node_id": "render",
        "turn": None,
        "value_digest": "a" * 64,
        "media_type": "text/markdown",
    }
    return ArtifactRef.model_validate(fields | overrides)


def _document_for(ref_id: str, value: str = "Matches the criterion, cites two sources.") -> tuple[ArtifactRef, ArtifactDocument]:
    ref = _ref(id=ref_id, value_digest=artifact_digest(value))
    return ref, ArtifactDocument(ref=ref, value=value)


def _request(**overrides: object) -> OwnerReviewRequest:
    artifact, _ = _document_for("result")
    fields: dict[str, object] = {
        "review_id": "r1",
        "actor": "owner",
        "outcome": "accepted",
        "reason": "Matches the success criterion.",
        "artifact_refs": (artifact,),
    }
    return OwnerReviewRequest.model_validate(fields | overrides)


# --- OwnerReviewRequest: a closed, non-empty shape --------------------------------------------


def test_a_request_refuses_an_empty_artifact_list() -> None:
    with pytest.raises(ValidationError, match="at least 1"):
        _request(artifact_refs=())


def test_a_request_refuses_an_unrecognized_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        OwnerReviewRequest.model_validate({**_request().model_dump(), "url": "https://example.test/review"})


@pytest.mark.parametrize("field", ["actor", "reason", "review_id"])
def test_a_request_refuses_an_empty_required_field(field: str) -> None:
    with pytest.raises(ValidationError, match="at least 1 character"):
        _request(**{field: ""})


# --- the fingerprint: identity of a request, apart from when it was minted --------------------


def test_the_fingerprint_is_stable_over_the_same_inputs() -> None:
    request = _request()

    assert owner_review_request_fingerprint(RUN, "app:harness", request) == owner_review_request_fingerprint(RUN, "app:harness", request)


@pytest.mark.parametrize(
    "vary",
    [
        lambda run_id, source, request: ("other-run", source, request),
        lambda run_id, source, request: (run_id, "app:other", request),
        lambda run_id, source, request: (run_id, source, request.model_copy(update={"reason": "a different reason"})),
        lambda run_id, source, request: (run_id, source, request.model_copy(update={"outcome": "rejected"})),
    ],
)
def test_the_fingerprint_changes_with_run_source_or_request_content(
    vary: Callable[[str, str, OwnerReviewRequest], tuple[str, str, OwnerReviewRequest]],
) -> None:
    request = _request()
    baseline = owner_review_request_fingerprint(RUN, "app:harness", request)

    varied_run, varied_source, varied_request = vary(RUN, "app:harness", request)

    assert owner_review_request_fingerprint(varied_run, varied_source, varied_request) != baseline


# --- FileOwnerReviewStore: append at most once per review id ----------------------------------


async def test_identical_retry_preserves_first_server_time(tmp_path: Path) -> None:
    store = FileOwnerReviewStore(tmp_path / "reviews.jsonl")
    artifact = ArtifactRef(
        id="result",
        label="Result",
        run_id="run-1",
        node_id="render",
        turn=None,
        value_digest="a" * 64,
        media_type="text/markdown",
    )
    request = OwnerReviewRequest(
        review_id="r1",
        actor="owner",
        outcome="rejected",
        reason="Missing counterevidence",
        artifact_refs=(artifact,),
    )
    t1 = datetime(2026, 9, 13, 10, tzinfo=UTC)
    t2 = datetime(2026, 9, 13, 11, tzinfo=UTC)

    first = await store.append_or_return("run-1", "studio", request, clock=lambda: t1)
    retry = await store.append_or_return("run-1", "studio", request, clock=lambda: t2)

    assert first.created is True
    assert retry.created is False
    assert retry.review == first.review
    assert retry.review.at == t1
    assert await store.for_run("run-1") == (first.review,)


async def test_a_changed_request_under_the_same_review_id_is_a_conflict_and_the_store_is_unchanged(tmp_path: Path) -> None:
    store = FileOwnerReviewStore(tmp_path / "reviews.jsonl")
    request = _request()
    t1 = datetime(2026, 9, 13, 10, tzinfo=UTC)

    committed = await store.append_or_return(RUN, "app:harness", request, clock=lambda: t1)
    changed = request.model_copy(update={"reason": "a different reason entirely"})

    with pytest.raises(OwnerReviewConflictError):
        await store.append_or_return(RUN, "app:harness", changed, clock=lambda: t1)

    assert await store.get(request.review_id) == committed.review


async def test_a_different_route_source_under_the_same_review_id_is_also_a_conflict(tmp_path: Path) -> None:
    store = FileOwnerReviewStore(tmp_path / "reviews.jsonl")
    request = _request()
    t1 = datetime(2026, 9, 13, 10, tzinfo=UTC)

    await store.append_or_return(RUN, "app:harness", request, clock=lambda: t1)

    with pytest.raises(OwnerReviewConflictError):
        await store.append_or_return(RUN, "app:studio", request, clock=lambda: t1)


async def test_a_changed_artifact_ref_under_the_same_review_id_is_a_conflict_and_the_store_is_unchanged(tmp_path: Path) -> None:
    store = FileOwnerReviewStore(tmp_path / "reviews.jsonl")
    request = _request()
    t1 = datetime(2026, 9, 13, 10, tzinfo=UTC)

    committed = await store.append_or_return(RUN, "app:harness", request, clock=lambda: t1)
    other_ref, _ = _document_for("result", value="A different value entirely.")
    changed = request.model_copy(update={"artifact_refs": (other_ref,)})

    with pytest.raises(OwnerReviewConflictError):
        await store.append_or_return(RUN, "app:harness", changed, clock=lambda: t1)

    assert await store.get(request.review_id) == committed.review


async def test_a_changed_path_run_under_the_same_review_id_is_a_conflict_and_the_store_is_unchanged(tmp_path: Path) -> None:
    store = FileOwnerReviewStore(tmp_path / "reviews.jsonl")
    request = _request()
    t1 = datetime(2026, 9, 13, 10, tzinfo=UTC)

    committed = await store.append_or_return(RUN, "app:harness", request, clock=lambda: t1)

    with pytest.raises(OwnerReviewConflictError):
        await store.append_or_return("run-2", "app:harness", request, clock=lambda: t1)

    assert await store.get(request.review_id) == committed.review


async def test_for_run_answers_only_reviews_committed_against_that_run(tmp_path: Path) -> None:
    store = FileOwnerReviewStore(tmp_path / "reviews.jsonl")
    here = _request(review_id="here")
    elsewhere_ref, _ = _document_for("result")
    elsewhere = _request(review_id="elsewhere", artifact_refs=(elsewhere_ref.model_copy(update={"run_id": "run-2"}),))
    t1 = datetime(2026, 9, 13, 10, tzinfo=UTC)

    await store.append_or_return(RUN, "app:harness", here, clock=lambda: t1)
    await store.append_or_return("run-2", "app:harness", elsewhere, clock=lambda: t1)

    kept = await store.for_run(RUN)

    assert [review.review_id for review in kept] == ["here"]


async def test_two_concurrent_identical_new_requests_produce_exactly_one_creation(tmp_path: Path) -> None:
    store = FileOwnerReviewStore(tmp_path / "reviews.jsonl")
    request = _request()
    barrier = asyncio.Barrier(2)
    t1 = datetime(2026, 9, 13, 10, tzinfo=UTC)
    t2 = datetime(2026, 9, 13, 11, tzinfo=UTC)

    async def attempt(at: datetime) -> OwnerReviewResult:
        await barrier.wait()
        return await store.append_or_return(RUN, "app:harness", request, clock=lambda: at)

    first, second = await asyncio.gather(attempt(t1), attempt(t2))

    outcomes = (first, second)
    created = [result for result in outcomes if result.created]
    retried = [result for result in outcomes if not result.created]
    assert len(created) == 1
    assert len(retried) == 1
    assert created[0].review == retried[0].review
    assert created[0].review.at in (t1, t2)
    assert await store.for_run(RUN) == (created[0].review,)


# --- OwnerReviewService: resolve and confirm every reference before appending -----------------


async def test_the_service_resolves_every_ref_before_appending_and_returns_the_same_refs(tmp_path: Path) -> None:
    store = FileOwnerReviewStore(tmp_path / "reviews.jsonl")
    ref, document = _document_for("result")
    resolver = FakeArtifactResolver(documents={"result": document})
    service = OwnerReviewService(resolver, store)
    request = _request(artifact_refs=(ref,))
    t1 = datetime(2026, 9, 13, 10, tzinfo=UTC)

    result = await service.review(RUN, "app:harness", request, clock=lambda: t1)

    assert result.created is True
    assert result.review.artifact_refs == (ref,)
    assert resolver.calls == [ref]


async def test_when_the_second_of_two_refs_fails_to_resolve_neither_is_appended(tmp_path: Path) -> None:
    store = FileOwnerReviewStore(tmp_path / "reviews.jsonl")
    first_ref, first_document = _document_for("first")
    second_ref, _ = _document_for("second")
    resolver = FakeArtifactResolver(documents={"first": first_document}, expired=frozenset({"second"}))
    service = OwnerReviewService(resolver, store)
    request = _request(artifact_refs=(first_ref, second_ref))
    t1 = datetime(2026, 9, 13, 10, tzinfo=UTC)

    with pytest.raises(ArtifactUnavailableError):
        await service.review(RUN, "app:harness", request, clock=lambda: t1)

    assert await store.get(request.review_id) is None
    assert resolver.calls == [first_ref, second_ref]


async def test_a_resolver_answering_for_a_different_reference_than_asked_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    store = FileOwnerReviewStore(tmp_path / "reviews.jsonl")
    ref, _ = _document_for("result")
    other_ref, other_document = _document_for("other")
    resolver = FakeArtifactResolver(documents={"result": other_document.model_copy(update={"ref": other_ref})})
    service = OwnerReviewService(resolver, store)
    request = _request(artifact_refs=(ref,))
    t1 = datetime(2026, 9, 13, 10, tzinfo=UTC)

    with pytest.raises(ArtifactUnavailableError):
        await service.review(RUN, "app:harness", request, clock=lambda: t1)

    assert await store.get(request.review_id) is None


async def test_an_artifact_naming_another_run_than_the_one_reviewed_is_refused_without_a_resolver_call(tmp_path: Path) -> None:
    store = FileOwnerReviewStore(tmp_path / "reviews.jsonl")
    ref, document = _document_for("result")
    other_run_ref = ref.model_copy(update={"run_id": "some-other-run"})
    resolver = FakeArtifactResolver(documents={"result": document})
    service = OwnerReviewService(resolver, store)
    request = _request(artifact_refs=(other_run_ref,))
    t1 = datetime(2026, 9, 13, 10, tzinfo=UTC)

    with pytest.raises(ArtifactUnavailableError):
        await service.review(RUN, "app:harness", request, clock=lambda: t1)

    assert resolver.calls == []


async def test_an_identical_retry_returns_the_stored_event_even_once_its_artifacts_have_expired(tmp_path: Path) -> None:
    store = FileOwnerReviewStore(tmp_path / "reviews.jsonl")
    ref, document = _document_for("result")
    resolver = FakeArtifactResolver(documents={"result": document})
    service = OwnerReviewService(resolver, store)
    request = _request(artifact_refs=(ref,))
    t1 = datetime(2026, 9, 13, 10, tzinfo=UTC)
    t2 = datetime(2026, 9, 13, 11, tzinfo=UTC)

    first = await service.review(RUN, "app:harness", request, clock=lambda: t1)
    resolver.expired = frozenset({"result"})
    retry = await service.review(RUN, "app:harness", request, clock=lambda: t2)

    assert retry.created is False
    assert retry.review == first.review
    assert resolver.calls == [ref], "the identical retry never called the resolver a second time"


async def test_a_changed_request_under_the_same_id_is_refused_before_any_resolver_call(tmp_path: Path) -> None:
    store = FileOwnerReviewStore(tmp_path / "reviews.jsonl")
    ref, document = _document_for("result")
    resolver = FakeArtifactResolver(documents={"result": document})
    service = OwnerReviewService(resolver, store)
    request = _request(artifact_refs=(ref,))
    t1 = datetime(2026, 9, 13, 10, tzinfo=UTC)

    await service.review(RUN, "app:harness", request, clock=lambda: t1)
    resolver.calls.clear()
    changed = request.model_copy(update={"reason": "a different reason"})

    with pytest.raises(OwnerReviewConflictError):
        await service.review(RUN, "app:harness", changed, clock=lambda: t1)

    assert resolver.calls == []


# --- EvaluationLimits: strictly positive accrual stops -----------------------------------------


def _limits(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "cycle_cost_stop_usd": "10",
        "trial_cost_stop_usd": "2",
        "trial_deadline_seconds": 180,
        "trial_request_limit": 16,
        "trial_attempt_limit": 3,
    }
    return fields | overrides


@pytest.mark.parametrize(
    "field",
    ["cycle_cost_stop_usd", "trial_cost_stop_usd", "trial_deadline_seconds", "trial_request_limit", "trial_attempt_limit"],
)
def test_evaluation_limits_refuse_a_non_positive_value(field: str) -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        EvaluationLimits.model_validate(_limits(**{field: 0}))


def test_evaluation_limits_accept_every_field_strictly_positive() -> None:
    limits = EvaluationLimits.model_validate(_limits())

    assert limits.trial_attempt_limit == 3


# --- TaskOutcome and ReviewedTaskOutcome: unknown stays unknown without a review ---------------


def _assessment(status: Literal["pass", "fail", "unknown", "not_applicable"] = "pass") -> Assessment:
    return Assessment(status=status, reason="Independent grading.", grader=PROVENANCE)


def _outcome() -> TaskOutcome:
    return TaskOutcome(
        run_id=RUN,
        task_id="task-1",
        structural_validity=_assessment(),
        workflow_design=_assessment(),
        evidence_support=_assessment(),
        task_success=_assessment(),
    )


def test_a_reviewed_outcome_with_no_reviews_must_carry_unknown_owner_acceptance() -> None:
    with pytest.raises(ValidationError, match="unknown"):
        ReviewedTaskOutcome(outcome=_outcome(), owner_acceptance=_assessment("pass"), reviews=())


def test_a_reviewed_outcome_with_no_reviews_and_unknown_acceptance_is_accepted() -> None:
    reviewed = ReviewedTaskOutcome(outcome=_outcome(), owner_acceptance=_assessment("unknown"), reviews=())

    assert reviewed.reviews == ()


def test_task_outcome_refuses_an_unrecognized_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        TaskOutcome.model_validate({**_outcome().model_dump(), "cost_usd": 1})
