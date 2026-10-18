"""The authoring slice's errors: a draft that cannot be kept, and a turn that is refused or could not finish."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.domain.contracts import DraftReadiness


class DraftNotSavableError(Exception):
    """Why the draft cannot be kept, in words the person can act on."""

    def __init__(self, message: str, readiness: DraftReadiness | None = None) -> None:
        """Hold the refusal and, when it came from one, the field-by-field reading.

        Args:
            message: The refusal, in words the person can act on.
            readiness: The field-by-field reading, so a form can put each finding beside its own box.
                `None` when no spec parsed, so there is nothing to read.
        """
        super().__init__(message)
        self.readiness = readiness


class DigestWorkflowUnreadableError(Exception):
    """The studio could not say which workflow digests a knowledge base, in words the model reads."""


class SessionUnknownError(Exception):
    """No session with that id here."""


class PartAnsweredError(Exception):
    """A continuation arrived without an answer for every call the turn stopped for."""


class TurnEmptyError(Exception):
    """A request with nothing to say and nothing to answer, refused rather than run as a bare continuation."""


class PlainTurnError(Exception):
    """A turn that could not finish, in one sentence for the chat bubble.

    The raw provider text stays on the session log. This is what reaches the stream, where the
    protocol's own error path turns it into the error chunk that ends the turn.
    """
