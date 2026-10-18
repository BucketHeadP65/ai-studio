"""The refusals a durable turn meets when it begins, continues or commits against its conversation."""


class ContinuationInFlightError(RuntimeError):
    """Another attempt on this conversation began and has not settled, so nothing else may run against it."""


class ContinuationConflictError(RuntimeError):
    """The conversation is not where this attempt believed it was, or the attempt is not the one stored under its id."""


class ContinuationUnsafeReplayError(RuntimeError):
    """Running this turn again automatically would not be safe: the checkpoint is not settled, or an effect is unresolved."""


class ContinuationPayloadTooLargeError(ValueError):
    """A committed output or a returned ref is past the bytes this contract allows."""
