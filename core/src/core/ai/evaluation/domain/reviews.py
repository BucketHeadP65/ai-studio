"""Owner reviews: an append-only, artifact-backed acceptance or rejection of a run's outcome.

A review names the exact bytes it was decided over. The same review id can never be repeated
under a different run, route source or request content. An identical retry returns the original
event, and anything else is refused. `actor` is whoever the caller says decided. It is text a
request carries, never verified identity.
"""

from __future__ import annotations

import json

from datetime import datetime
from typing import Literal

from pydantic import Field

from core.ai.artifacts import ArtifactRef
from core.ai.evaluation.domain.models import EvaluationModel, Fingerprint, Nonempty
from core.fingerprints import content_fingerprint


class OwnerReviewRequest(EvaluationModel):
    """What an owner decided about a run's already-fixed artifacts, and why."""

    review_id: Nonempty
    actor: Nonempty
    outcome: Literal["accepted", "rejected"]
    reason: Nonempty
    artifact_refs: tuple[ArtifactRef, ...] = Field(min_length=1)


def owner_review_request_fingerprint(run_id: str, source: str, request: OwnerReviewRequest) -> Fingerprint:
    """Identity of one review request: everything about it except the server time it was minted at.

    Two requests carrying the same review id and this same fingerprint are the same event. A
    request carrying a different fingerprint under that id is an attempt to overwrite it.

    Args:
        run_id: The run being reviewed.
        source: The route source the review came through.
        request: What the owner decided.

    Returns:
        The request's fingerprint.
    """
    payload = {
        "review_id": request.review_id,
        "run_id": run_id,
        "source": source,
        "actor": request.actor,
        "outcome": request.outcome,
        "reason": request.reason,
        "artifact_refs": [ref.model_dump(mode="json") for ref in request.artifact_refs],
    }
    return content_fingerprint(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


class OwnerReview(OwnerReviewRequest):
    """One committed review event: the request, its route source, and when it was minted."""

    source: Nonempty
    at: datetime
    request_fingerprint: Fingerprint


class OwnerReviewResult(EvaluationModel):
    """The review a request now names, and whether this call was the one that minted it."""

    review: OwnerReview
    created: bool
