"""The owner review use case: resolve every artifact a verdict names, then record it once."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.artifacts import ArtifactUnavailableError
from core.ai.evaluation.domain.reviews import OwnerReviewResult, owner_review_request_fingerprint
from core.ai.evaluation.exceptions import OwnerReviewConflictError

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from core.ai.artifacts import ArtifactResolver
    from core.ai.evaluation.domain.reviews import OwnerReviewRequest
    from core.ai.evaluation.internal_contracts import OwnerReviewStore


class OwnerReviewService:
    """Turns one owner verdict into an append-only review, backed by artifacts a run actually fixed.

    Every reference is resolved and checked against the document it addresses before anything is
    written. A request repeating an already-committed review id returns that event without
    resolving anything again, even if what it once pointed at can no longer be read back.
    """

    def __init__(self, resolver: ArtifactResolver, store: OwnerReviewStore) -> None:
        """Review through one resolver and one store.

        Args:
            resolver: Resolves each artifact reference a review names.
            store: Where reviews are appended and read back.
        """
        self._resolver = resolver
        self._store = store

    async def review(
        self, run_id: str, source: str, request: OwnerReviewRequest, *, clock: Callable[[], datetime]
    ) -> OwnerReviewResult:
        """Accept `request` against `run_id`, or refuse it.

        Args:
            run_id: The run being reviewed.
            source: The route source the review came through.
            request: What the owner decided.
            clock: The clock that stamps a newly minted review.

        Returns:
            The review the id now names, and whether this call minted it.

        Raises:
            OwnerReviewConflictError: `request.review_id` already names a different request, run or
                route source than the one submitted now.
            ArtifactUnavailableError: an artifact reference names another run, cannot be
                resolved, or resolves to a document addressing a different reference.
        """
        existing = await self._store.get(request.review_id)
        if existing is not None:
            if owner_review_request_fingerprint(run_id, source, request) == existing.request_fingerprint:
                return OwnerReviewResult(review=existing, created=False)
            msg = f"review {request.review_id!r} already names a different request, run or route source"
            raise OwnerReviewConflictError(msg)
        for ref in request.artifact_refs:
            if ref.run_id != run_id:
                msg = f"artifact {ref.id!r} belongs to another run than the one being reviewed"
                raise ArtifactUnavailableError(msg)
            document = await self._resolver.resolve(ref)
            if document.ref != ref:
                msg = f"the resolver answered for {document.ref.id!r}, not the reference {ref.id!r} asked for"
                raise ArtifactUnavailableError(msg)
        return await self._store.append_or_return(run_id, source, request, clock=clock)
