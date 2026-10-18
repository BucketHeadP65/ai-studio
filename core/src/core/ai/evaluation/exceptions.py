"""The errors the evaluation kernel raises: failed trials, budget refusals, evidence and review failures."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.evaluation.domain.models import ResourceUsage

if TYPE_CHECKING:
    from core.ai.evaluation.domain.budget import BudgetRefusal, PreflightRefusal


class EvaluationError(Exception):
    """A failed trial an adapter declares, carrying all usage spent before the failure.

    An adapter translates recoverable provider, timeout and output-validation errors into this.
    Programming errors and cancellation propagate to the caller.
    """

    def __init__(self, message: str, *, usage: ResourceUsage | None = None) -> None:
        """Name the failure and the usage it spent.

        Args:
            message: What failed.
            usage: The usage spent before the failure. Nothing spent when omitted.
        """
        super().__init__(message)
        self.usage = usage or ResourceUsage()


class BudgetError(EvaluationError):
    """Anything the evaluation budget record refuses or cannot do."""


class BudgetRefusedError(BudgetError):
    """Admission refused by a rule the record checked, named by `refusal`."""

    refusal: BudgetRefusal

    def __init__(self, message: str, *, refusal: BudgetRefusal) -> None:
        """Name the refusal and the rule behind it.

        Args:
            message: Why admission was refused.
            refusal: The rule that refused.
        """
        super().__init__(message)
        self.refusal = refusal


class BudgetPreflightError(BudgetError):
    """A live trial refused before execution, named by `refusal`."""

    refusal: PreflightRefusal

    def __init__(self, message: str, *, refusal: PreflightRefusal) -> None:
        """Name the refusal and the check behind it.

        Args:
            message: Why the trial was refused.
            refusal: The check that refused.
        """
        super().__init__(message)
        self.refusal = refusal


class BudgetConflictError(BudgetError):
    """A repeat that is not the same call: changed limits, root, start, attempt or charge."""


class BudgetNotOpenError(BudgetError):
    """A cycle or a trial that was never begun, asked to admit or settle a request."""


class BudgetUnreadableError(BudgetError):
    """A persisted record that is no longer a readable cycle state."""


class BudgetPersistenceError(BudgetError):
    """The record could not be read or written at all."""


class DeliveredEvidenceError(EvaluationError):
    """Delivered evidence could not be admitted or read back exactly as it was stored."""


class DeliveredEvidenceBoundError(DeliveredEvidenceError):
    """A payload or a trial's accumulated payloads crossed a size bound, so the trial is invalid."""


class DeliveredEvidenceConflictError(DeliveredEvidenceError):
    """An evidence identity already names different bytes than the ones submitted now."""


class DeliveredEvidencePersistenceError(DeliveredEvidenceError):
    """The store could not write or read what a trial delivered, so the trial has no record."""


class DeliveredEvidenceUnreadableError(DeliveredEvidenceError):
    """A stored payload is missing, or its path, byte count or hash differs from its reference."""


class OwnerReviewConflictError(ValueError):
    """A review id already names a request, run or route source other than the one submitted."""
