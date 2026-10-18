"""Owner reviews as one append-only JSON-lines file, safe under concurrent processes."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os

from typing import TYPE_CHECKING

from core.ai.evaluation.domain.reviews import OwnerReview, OwnerReviewResult, owner_review_request_fingerprint
from core.ai.evaluation.exceptions import OwnerReviewConflictError

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    from core.ai.evaluation.domain.reviews import OwnerReviewRequest


class FileOwnerReviewStore:
    """`OwnerReviewStore` over one append-only JSON-lines file, safe under concurrent processes.

    A cooperative file lock serializes every read-check-write, so two callers minting the same
    review id at once still produce exactly one event: the first to hold the lock appends and
    mints `at`. The second, once it gets the lock, finds that event already filed and returns it
    unmodified, never calling its own clock.
    """

    def __init__(self, path: Path) -> None:
        """Keep reviews in one file.

        Args:
            path: The JSON-lines file. It and its folder are created on the first append.
        """
        self.path = path

    async def get(self, review_id: str) -> OwnerReview | None:
        """The review filed under `review_id`, or `None` when nothing is.

        Args:
            review_id: The review to find.

        Returns:
            The review, or `None`.
        """
        return await asyncio.to_thread(self._get, review_id)

    async def for_run(self, run_id: str) -> tuple[OwnerReview, ...]:
        """Every review committed against `run_id`, in the order they were appended.

        Args:
            run_id: The run whose reviews are wanted.

        Returns:
            The reviews, oldest first.
        """
        return await asyncio.to_thread(self._for_run, run_id)

    async def append_or_return(
        self, run_id: str, source: str, request: OwnerReviewRequest, *, clock: Callable[[], datetime]
    ) -> OwnerReviewResult:
        """Mint and append a genuinely new review, or return the one already filed under its id.

        Args:
            run_id: The run being reviewed.
            source: The route source the review came through.
            request: What the owner decided.
            clock: The clock that stamps a newly minted review. It is called at most once.

        Returns:
            The review the id now names, and whether this call minted it.

        Raises:
            OwnerReviewConflictError: `request.review_id` already names a different request, run or
                route source than the one submitted now.
        """
        return await asyncio.to_thread(self._append_or_return, run_id, source, request, clock)

    def _parse(self, line: str) -> tuple[str, OwnerReview]:
        raw = json.loads(line)
        return raw["run_id"], OwnerReview.model_validate(raw["review"])

    def _rows(self) -> tuple[tuple[str, OwnerReview], ...]:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        return tuple(self._parse(line) for line in content.splitlines() if line.strip())

    def _get(self, review_id: str) -> OwnerReview | None:
        return next((review for _, review in self._rows() if review.review_id == review_id), None)

    def _for_run(self, run_id: str) -> tuple[OwnerReview, ...]:
        return tuple(review for stored_run, review in self._rows() if stored_run == run_id)

    def _append_or_return(
        self, run_id: str, source: str, request: OwnerReviewRequest, clock: Callable[[], datetime]
    ) -> OwnerReviewResult:
        fingerprint = owner_review_request_fingerprint(run_id, source, request)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                stream.seek(0)
                rows = tuple(self._parse(line) for line in stream if line.strip())
                existing = next((review for _, review in rows if review.review_id == request.review_id), None)
                if existing is not None:
                    if existing.request_fingerprint != fingerprint:
                        msg = f"review {request.review_id!r} already names a different request, run or route source"
                        raise OwnerReviewConflictError(msg)
                    return OwnerReviewResult(review=existing, created=False)
                review = OwnerReview(
                    review_id=request.review_id,
                    actor=request.actor,
                    outcome=request.outcome,
                    reason=request.reason,
                    artifact_refs=request.artifact_refs,
                    source=source,
                    at=clock(),
                    request_fingerprint=fingerprint,
                )
                line = json.dumps({"run_id": run_id, "review": review.model_dump(mode="json")}, sort_keys=True, separators=(",", ":"))
                stream.write(line + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                return OwnerReviewResult(review=review, created=True)
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
