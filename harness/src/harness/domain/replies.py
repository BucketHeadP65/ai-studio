"""Reading an assistant's own words back out of a stored text part.

Every plugin that shows a past reply reads it through here, so the chat panel and the session
transcript can never disagree about what a turn said.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, ValidationError


class LegacyReply(BaseModel):
    """The reply envelope a run recorded before replies were plain prose, read back for its message only.

    `extra="forbid"` is what keeps this narrow. A text part is read through this model to recover
    an old turn's words, and a lookalike dict that merely has a `message` key must not pass for one.
    """

    model_config = ConfigDict(extra="forbid")

    message: str
    spec_yaml: str = ""
    title: str = ""
    summary: str = ""
    applicability: str = ""


def said(text: str) -> str:
    """The agent's own words out of a final part.

    A part written before replies were plain prose is a JSON envelope, read back through
    `LegacyReply` for its `message` field. A part that is not that exact shape (ordinary prose, or
    a lookalike dict a model actually meant to say) is returned as itself.

    Args:
        text: The content of one stored text part.

    Returns:
        The words, with surrounding whitespace stripped.
    """
    try:
        return LegacyReply.model_validate_json(text).message.strip()
    except ValidationError:
        return text.strip()
