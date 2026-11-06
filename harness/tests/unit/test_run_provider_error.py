"""A run's evidence says whether it stopped with a provider error, read off the error itself where the run ended."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import FromNode, FromParameter, ParameterSpec, RunEvidence, RunNode, StepRegistry, Workflow
from pydantic_ai.exceptions import (
    ContentFilterError,
    ModelConnectionError,
    ModelHTTPError,
    ModelRateLimitError,
    ModelRequestTimeout,
    ModelServerError,
    ModelTimeoutError,
    ModelUnavailableError,
    ToolRetryError,
    UnexpectedModelBehavior,
)
from pydantic_ai.messages import RetryFeedbackCause, RetryFeedbackPart, RetryInfo

from harness.features.maintenance.contracts import LandingRefusedError, LandingReport
from harness.features.running import service
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.runtime import a_runtime
from tests.support.state import a_state, records_under

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

WORKFLOW_ID = "wf.plan"


def _out_of_output_retries(cause: RetryFeedbackCause) -> UnexpectedModelBehavior:
    """What pydantic-ai raises once a model's output retries run out, chained from the retry the last reply asked for."""
    raised = UnexpectedModelBehavior("Exceeded maximum output retries (1)")
    raised.__cause__ = ToolRetryError(RetryFeedbackPart("Please return text or call a tool.", retry=RetryInfo(cause=cause)))
    return raised


def _spec() -> Workflow:
    return Workflow(
        name="plan",
        version=1,
        description="plan",
        parameters={"topic": ParameterSpec(description="what to plan", json_schema={"type": "string"})},
        nodes=(RunNode(id="plan", work="plan", inputs={"topic": FromParameter(parameter="topic")}),),
        output=FromNode(node="plan"),
    )


async def _evidence_of(tmp_path: Path, raised: Callable[[], BaseException] | None) -> RunEvidence:
    """The evidence of one run whose only step raises what `raised` makes, or completes when it is None."""

    async def plan(topic: str) -> str:
        if raised is not None:
            raise raised()
        return topic

    registry = StepRegistry()
    registry.register(plan)
    session = a_runtime(registry=registry, state=a_state(tmp_path))
    records = records_under(tmp_path)
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=_spec(), workflow_id=WORKFLOW_ID, params={"topic": "steps"})
    task = session.active.task
    assert task is not None
    await task
    (evidence,) = [event for event in await records.evidence.events_for(WORKFLOW_ID) if isinstance(event, RunEvidence)]
    return evidence


@pytest.mark.parametrize(
    "raised",
    [
        pytest.param(lambda: ContentFilterError("Content filter triggered. Finish reason: 'content_filter'."), id="content filter"),
        pytest.param(lambda: ModelConnectionError("gpt", "the connection dropped"), id="lost connection"),
        pytest.param(lambda: ModelTimeoutError("gpt", "the read timed out"), id="connection timeout"),
        pytest.param(lambda: ModelRequestTimeout("gpt", 120.0), id="request timeout"),
        pytest.param(lambda: ModelHTTPError(503, "gpt"), id="HTTP 503"),
        pytest.param(lambda: ModelHTTPError(500, "gpt"), id="HTTP 500"),
        pytest.param(lambda: ModelHTTPError(429, "gpt"), id="HTTP 429"),
        pytest.param(lambda: ModelServerError("gpt", "the server failed"), id="server error"),
        pytest.param(lambda: ModelUnavailableError("gpt", "the model is unavailable"), id="unavailable"),
        pytest.param(lambda: ModelRateLimitError("gpt", "slow down"), id="rate limit"),
        pytest.param(lambda: _out_of_output_retries("no_output"), id="empty reply"),
    ],
)
async def test_a_run_stopped_by_its_model_provider_says_so(tmp_path: Path, raised: Callable[[], BaseException]) -> None:
    evidence = await _evidence_of(tmp_path, raised)

    assert evidence.outcome == "failed"
    assert evidence.provider_error is True


@pytest.mark.parametrize(
    "raised",
    [
        pytest.param(lambda: ModelHTTPError(400, "gpt"), id="HTTP 400"),
        pytest.param(lambda: ModelHTTPError(401, "gpt"), id="HTTP 401"),
        pytest.param(lambda: _out_of_output_retries("validation_error"), id="output retries run out on a real answer"),
        pytest.param(lambda: UnexpectedModelBehavior("Exceeded maximum output retries (1)"), id="output retries with no cause"),
        pytest.param(lambda: ValueError("the change does not validate"), id="a validation error inside the run"),
        pytest.param(
            lambda: LandingRefusedError(LandingReport(changes=[], left_for_next_run=[], left_to_repository=[]), told="The owner was told."),
            id="a landing refusal",
        ),
    ],
)
async def test_a_run_that_failed_for_any_other_reason_is_no_provider_error(tmp_path: Path, raised: Callable[[], BaseException]) -> None:
    evidence = await _evidence_of(tmp_path, raised)

    assert evidence.outcome == "failed"
    assert evidence.provider_error is False


async def test_a_completed_run_is_no_provider_error(tmp_path: Path) -> None:
    evidence = await _evidence_of(tmp_path, None)

    assert evidence.outcome == "completed"
    assert evidence.provider_error is False
