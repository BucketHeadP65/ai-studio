"""The evaluation kernel's ports: the budget record, the evidence store and the owner review store."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from decimal import Decimal

    from pydantic_ai.messages import ModelResponse

    from core.ai.evaluation.domain.budget import BudgetAdmission, BudgetRole, BudgetScope, BudgetSnapshot, StateRootIdentity
    from core.ai.evaluation.domain.evidence import DeliveredEvidence, DeliveredEvidenceRef, DeliveredSource
    from core.ai.evaluation.domain.outcomes import EvaluationLimits
    from core.ai.evaluation.domain.reviews import OwnerReview, OwnerReviewRequest, OwnerReviewResult


class VerifiedStateRoot(Protocol):
    """A record that can name the state root and device it holds itself to."""

    def state_identity(self) -> StateRootIdentity:
        """The canonical root and POSIX device this record verifies every mutation against.

        Returns:
            The root and device.
        """
        ...


class ResponseCharge(Protocol):
    """What one response actually cost, or `None` when nothing could price it."""

    def __call__(self, response: ModelResponse) -> Decimal | None:
        """The measured charge for `response`. `None` is unknown and never zero.

        Args:
            response: The model response to price.

        Returns:
            The charge, or `None`.
        """
        ...


class EvaluationBudgetRecord(Protocol):
    """The persisted admission record every budgeted model asks before it reaches a provider."""

    async def begin_cycle(self, cycle_id: str, limits: EvaluationLimits, *, pricing_known: bool, at: datetime) -> None:
        """Open `cycle_id` under `limits`, or accept the identical call again without resetting it.

        Args:
            cycle_id: The cycle to open.
            limits: The stop thresholds the cycle runs under.
            pricing_known: Whether every configured role's model has a resolvable price.
            at: When the cycle starts.

        Raises:
            BudgetPreflightError: `pricing_known` is false, or this process cannot verify the
                cycle's own state root.
            BudgetConflictError: the cycle exists under different limits or a different start.
        """
        ...

    async def begin_trial(self, scope: BudgetScope, *, at: datetime) -> None:
        """Open `scope.trial_id` at `at`, deadline included, or accept the identical call again.

        Args:
            scope: The cycle and trial to open.
            at: When the trial starts.

        Raises:
            BudgetNotOpenError: the cycle was never begun.
            BudgetConflictError: the trial exists with a different start.
        """
        ...

    async def begin_attempt(self, scope: BudgetScope, attempt_id: str, *, at: datetime) -> BudgetSnapshot:
        """Record one attempt at the trial's work, or accept the identical attempt again.

        Args:
            scope: The trial the attempt belongs to.
            attempt_id: The attempt to record.
            at: When the attempt began.

        Returns:
            The trial's snapshot after the attempt is recorded.

        Raises:
            BudgetNotOpenError: the cycle or the trial was never begun.
            BudgetRefusedError: the trial has already spent its attempts.
        """
        ...

    async def admit(self, scope: BudgetScope, request_id: str, role: BudgetRole, *, at: datetime) -> BudgetAdmission:
        """Let one request through, persisted before the caller may make it.

        Args:
            scope: The trial the request belongs to.
            request_id: The request to admit.
            role: The model role making the request.
            at: When the request is admitted.

        Returns:
            The admission, to settle once the response arrives.

        Raises:
            BudgetNotOpenError: the cycle or the trial was never begun.
            BudgetRefusedError: a rule of the record refuses, named by `refusal`.
            BudgetConflictError: `request_id` was admitted under a different scope or role.
        """
        ...

    async def settle(self, admission: BudgetAdmission, charge_usd: Decimal | None, *, at: datetime) -> BudgetSnapshot:
        """Record what the admitted request actually cost. `None` leaves the cycle unresolved.

        Args:
            admission: The admission being settled.
            charge_usd: The measured charge, or `None` when nothing could price it.
            at: When the request settled.

        Returns:
            The trial's snapshot after settlement.

        Raises:
            BudgetNotOpenError: nothing was admitted under this request id.
            BudgetConflictError: it was already settled for a different charge.
        """
        ...


class DeliveredEvidenceReader(Protocol):
    """Read back the exact payload a reference names, or refuse."""

    async def read(self, ref: DeliveredEvidenceRef) -> DeliveredEvidence:
        """The payload `ref` names, byte-identical to what was stored.

        Args:
            ref: The reference to resolve.

        Returns:
            The stored payload beside its reference.

        Raises:
            DeliveredEvidenceUnreadableError: The payload is missing, its path, byte count or hash
                differs from `ref`, or the trial's record cannot be read back. Partial content is
                never returned.
            DeliveredEvidencePersistenceError: The stored payload could not be read at all.
        """
        ...


class DeliveredEvidenceStore(DeliveredEvidenceReader, Protocol):
    """Append-once delivered evidence for a trial, and the reader over what it holds."""

    async def append(
        self, trial_id: str, ordinal: int, payload_json: str, *, sources: tuple[DeliveredSource, ...]
    ) -> DeliveredEvidenceRef:
        """Store `payload_json` as the trial's `ordinal`-th delivered payload and name it.

        Repeating a call with the same trial, ordinal and bytes returns the reference already
        filed, without admitting the payload's bytes toward the trial bound a second time.

        Args:
            trial_id: The trial that delivered the payload.
            ordinal: The payload's position in the trial.
            payload_json: The exact serialized payload handed to the model.
            sources: The sources the payload carried.

        Returns:
            The reference that names the stored payload.

        Raises:
            DeliveredEvidenceBoundError: The payload crosses `DELIVERED_PAYLOAD_BYTES`, or the
                trial's accumulated bytes would cross `TRIAL_DELIVERED_EVIDENCE_BYTES`. Nothing is
                written for a refused payload.
            DeliveredEvidenceConflictError: This trial and ordinal already name different bytes.
            DeliveredEvidenceUnreadableError: The trial's own record is no longer readable.
            DeliveredEvidencePersistenceError: The payload could not be written.
        """
        ...


class OwnerReviewStore(Protocol):
    """Append-only owner reviews: mint one at most once per review id, and read them back."""

    async def get(self, review_id: str) -> OwnerReview | None:
        """The review filed under `review_id`, or `None` when nothing is.

        Args:
            review_id: The review to find.

        Returns:
            The review, or `None`.
        """
        ...

    async def for_run(self, run_id: str) -> tuple[OwnerReview, ...]:
        """Every review committed against `run_id`, in the order they were appended.

        Args:
            run_id: The run whose reviews are wanted.

        Returns:
            The reviews, oldest first.
        """
        ...

    async def append_or_return(
        self, run_id: str, source: str, request: OwnerReviewRequest, *, clock: Callable[[], datetime]
    ) -> OwnerReviewResult:
        """Mint and append a genuinely new review, or return the one already filed under its id.

        `clock` is called at most once, and only by whichever concurrent caller actually appends.

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
        """
        ...
