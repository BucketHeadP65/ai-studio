"""Building the running controller a route test drives, over files under the test's own directory."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.ai.artifacts import ArtifactUnavailableError
from core.ai.evaluation.contracts import OwnerReview, OwnerReviewConflictError
from core.ai.evaluation.domain.reviews import OwnerReviewResult, owner_review_request_fingerprint

from harness.features.running.contracts import RunCollaborators, SpendLimit
from harness.features.running.controller import RunningController
from harness.session import SessionPool
from harness.settings import RunSettings, StateSettings
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.sessions import APool
from tests.support.runtime import a_runtime
from tests.support.state import records_under

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    from core.ai.artifacts import ArtifactDocument, ArtifactRef
    from core.ai.evaluation.contracts import OwnerReviewRequest
    from core.ai.workflows.contracts import StepRegistry

    from harness.domain.arguments import KnowledgeBaseNames


def a_pool(registry: StepRegistry | None = None, *, state: Path | None = None) -> SessionPool:
    """A pool over one session with nothing behind it, for a route that needs neither model nor library.

    `state` is the home its default session writes its own files under; a pool for a route that
    writes none may leave it out.
    """
    default = a_runtime() if registry is None else a_runtime(registry=registry)
    if state is not None:
        default.state = StateSettings(home=state)
    return SessionPool(default)


def a_running_controller(
    pool: SessionPool,
    state: Path,
    *,
    run: RunSettings | None = None,
    knowledge_bases: KnowledgeBaseNames | None = None,
    spend_limits: tuple[SpendLimit, ...] = (),
) -> RunningController:
    """The controller over `pool`, its archive and its ledger written under `state`.

    Args:
        pool: The sessions the routes resolve in, its default the one an unscoped route acts on.
        state: The directory this test's archive, ledger and evidence log live under.
        run: Which engine this deployment runs on. Defaults to the in-process one.
        knowledge_bases: Which names a launch may admit a selection of. Left out for a test whose
            workflow selects none.
        spend_limits: The money ceilings this deployment holds a run to. Left out for a test that is not
            about them, which then reads as a deployment with none configured.

    Returns:
        A controller reaching nothing but `state`.
    """
    return RunningController(
        sessions=pool,
        run=RunCollaborators(
            records=records_under(state),
            topics=FakeBroadcaster(),
            run_settings=run if run is not None else RunSettings(),
            sessions=APool(pool),
            knowledge_bases=knowledge_bases,
            spend_limits=spend_limits,
        ),
    )


@dataclass
class FakeArtifactResolver:
    """A scriptable `ArtifactResolver`: answers with a document by ref id, refuses one held as expired."""

    documents: dict[str, ArtifactDocument] = field(default_factory=dict)
    expired: frozenset[str] = frozenset()
    calls: list[ArtifactRef] = field(default_factory=list)

    async def resolve(self, ref: ArtifactRef) -> ArtifactDocument:
        self.calls.append(ref)
        if ref.id in self.expired:
            msg = f"artifact {ref.id!r} is no longer available"
            raise ArtifactUnavailableError(msg)
        document = self.documents.get(ref.id)
        if document is None:
            msg = f"artifact {ref.id!r} is unknown to this resolver"
            raise ArtifactUnavailableError(msg)
        return document


@dataclass
class FakeOwnerReviewStore:
    """An in-memory `OwnerReviewStore`: the same append-at-most-once contract, no file or lock."""

    rows: dict[str, tuple[str, OwnerReview]] = field(default_factory=dict)

    async def get(self, review_id: str) -> OwnerReview | None:
        found = self.rows.get(review_id)
        return None if found is None else found[1]

    async def for_run(self, run_id: str) -> tuple[OwnerReview, ...]:
        return tuple(review for stored_run, review in self.rows.values() if stored_run == run_id)

    async def append_or_return(
        self, run_id: str, source: str, request: OwnerReviewRequest, *, clock: Callable[[], datetime]
    ) -> OwnerReviewResult:
        fingerprint = owner_review_request_fingerprint(run_id, source, request)
        existing = self.rows.get(request.review_id)
        if existing is not None:
            _, review = existing
            if review.request_fingerprint != fingerprint:
                msg = f"review {request.review_id!r} already names a different request, run or route source"
                raise OwnerReviewConflictError(msg)
            return OwnerReviewResult(review=review, created=False)
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
        self.rows[request.review_id] = (run_id, review)
        return OwnerReviewResult(review=review, created=True)
