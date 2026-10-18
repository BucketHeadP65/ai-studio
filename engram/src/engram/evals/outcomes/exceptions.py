"""The error an outcome-evaluation trial raises when a withheld reference reaches a payload."""

from core.ai.evaluation.contracts import EvaluationError


class WithheldEvidenceLeakError(EvaluationError):
    """A finished payload still named a withheld reference, so the trial cannot be trusted."""
