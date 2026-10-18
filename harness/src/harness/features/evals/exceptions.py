"""The errors the evals slice raises when a step cannot be performed or a model request cannot be metered."""

from core.ai.evaluation.contracts import BudgetError, EvaluationError


class StepNotABriefingError(ValueError):
    """A step eval was asked to perform a step that is not a briefing (a `Briefing`)."""


class StepOutputReusesAnotherError(ValueError):
    """A step's output slot names another step's type instead of declaring its own shape."""


class UnmeteredModelCallError(BudgetError):
    """A model path the record cannot meter, refused under a budgeted scope rather than spent."""


class UnmeterablePilotError(BudgetError):
    """A pilot that cannot be shown to be metered, refused before its cycle opens."""


class UnrecordedRequestsError(EvaluationError):
    """A participant reported more model requests than the record admitted for that trial."""
