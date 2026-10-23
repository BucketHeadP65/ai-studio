"""A stable identity for a pydantic-ai message, so a later rewrite of it is stored as an edit, not as a new message."""

from __future__ import annotations

import json

from hashlib import sha256

from pydantic_ai.messages import (
    BaseToolCallPart,
    BaseToolReturnPart,
    CompactionPart,
    FilePart,
    ModelMessage,
    ModelRequestPart,
    ModelResponse,
    ModelResponsePart,
    RetryFeedbackPart,
    SpeechPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolAvailabilityDeltaPart,
    UserPromptPart,
)
from pydantic_core import to_jsonable_python


def message_key(message: ModelMessage) -> str:
    """The identity of `message`, a 32-character hex digest that survives edits to its content.

    Built from what nobody rewrites: the message's kind, a response's timestamp and provider id, and per part its kind plus
    the one field that names it (a tool call id, a part timestamp, a part id) or its position when it has none. A request's
    own timestamp is left out because pydantic-ai re-stamps it on every send. A message with nothing stable to be known by
    is keyed by its content instead, so an edit to it is stored again, which costs space, never correctness.

    Args:
        message: The message.

    Returns:
        The 32-character hex identity.
    """
    fields: list[str] = [message.kind]
    if isinstance(message, ModelResponse):
        fields.append(message.timestamp.isoformat() if message.timestamp is not None else "")
        fields.append(message.provider_response_id or "")
    else:
        fields.append("")
    named = any(field for field in fields[1:])
    for index, part in enumerate(message.parts):
        fields.append(part.part_kind)
        name = _part_name(part)
        named = named or name is not None
        fields.append(name if name is not None else str(index))
    if not named:
        # Changing this encoding re-keys every stored message.
        fields.append(sha256(json.dumps(to_jsonable_python(message), sort_keys=True).encode()).hexdigest())
    return sha256("\x1f".join(fields).encode()).hexdigest()[:32]


def _part_name(part: ModelRequestPart | ModelResponsePart) -> str | None:
    """The one field that names `part`: its tool call id, else its timestamp, else its part id, else nothing."""
    if isinstance(part, BaseToolCallPart | BaseToolReturnPart | ToolAvailabilityDeltaPart):
        tool_call_id = part.tool_call_id
        if tool_call_id:
            return tool_call_id
    if isinstance(part, SystemPromptPart | UserPromptPart | BaseToolReturnPart | RetryFeedbackPart):
        return part.timestamp.isoformat()
    if isinstance(part, BaseToolCallPart | TextPart | ThinkingPart | CompactionPart | FilePart | SpeechPart):
        return part.id or None
    return None
