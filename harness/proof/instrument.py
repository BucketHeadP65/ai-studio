"""Shared instruments for both experiment paths — fairness in retrieval, tool cost tracking.

Extraction reads results only (what came back is what was surfaced); the guard refuses a
call that targets a withheld page before it happens; the scrub drops withheld lines from
listings. Both paths share these objects, so neither can get private accounting.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic_ai.models.wrapper import WrapperModel

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage, ModelResponse
    from pydantic_ai.models import Model, ModelRequestParameters
    from pydantic_ai.settings import ModelSettings

_ID = re.compile(r"\b(?:paper|ent|q)\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")
"""The knowledge base's id grammar, as groundtruth.py's link table names it. A `#section` suffix is
not part of the match; the character class excludes `#`, so sections fold to their page."""

REFUSAL = "This page is withheld from this experiment. Answer from primary sources instead."


@dataclass(frozen=True)
class ToolCall:
    """A single tool invocation record: args sent, result received, refusal flag."""

    tool: str
    args: str
    result: str
    refused: bool = False


@dataclass
class ToolTrace:
    """Mutable list of tool calls for recording during an experiment run."""

    calls: list[ToolCall] = field(default_factory=list)


def surfaced_ids(trace: ToolTrace) -> frozenset[str]:
    """Knowledge base ids that came back — results only, because an id merely asked for was never surfaced."""
    return frozenset(match.group(0) for call in trace.calls for match in _ID.finditer(call.result))


def guard_args(args_text: str, withheld: frozenset[str]) -> str | None:
    """Refusal when the call itself targets a withheld page — a fetch by id, a WHERE clause by name."""
    return REFUSAL if any(page in args_text for page in withheld) else None


def scrub_result(text: str, withheld: frozenset[str]) -> tuple[str, bool]:
    """The text with every withheld-mentioning line removed, and whether the scrub removed anything."""
    kept = [line for line in text.splitlines() if not any(page in line for page in withheld)]
    scrubbed = "\n".join(kept)
    return (scrubbed if kept else REFUSAL), len(kept) != len(text.splitlines())


class RecordingModel(WrapperModel):
    """The one cost meter both paths pay through. Requests and tokens, summed; nothing altered."""

    def __init__(self, wrapped: Model) -> None:
        super().__init__(wrapped)
        self.requests = 0
        self.input_tokens = 0
        self.output_tokens = 0

    async def request(
        self, messages: list[ModelMessage], model_settings: ModelSettings | None, model_request_parameters: ModelRequestParameters
    ) -> ModelResponse:
        """Pass through to wrapped model, incrementing request and token counters."""
        response = await super().request(messages, model_settings, model_request_parameters)
        self.requests += 1
        self.input_tokens += response.usage.input_tokens or 0
        self.output_tokens += response.usage.output_tokens or 0
        return response
