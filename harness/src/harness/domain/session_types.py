"""What a session is opened as, and the refusal for an id that names no session."""

from __future__ import annotations

from typing import Literal, get_args

SessionType = Literal["chat", "runner", "master"]
"""What a session was opened as. The projected `session_kind` column holds exactly these."""

DEFAULT_SESSION_TYPE: SessionType = "chat"
"""What a session or a run log that names no type is read as."""

UNKNOWN_SESSION = "no session with that id here"
"""The refusal text for a session id this process does not hold."""


def session_type_of(text: str) -> SessionType:
    """The session type `text` names, or `"chat"` when it names none.

    Args:
        text: A session kind as a session or a run log holds it.

    Returns:
        The matching session type.
    """
    for kind in get_args(SessionType):
        if text == kind:
            return kind
    return DEFAULT_SESSION_TYPE
