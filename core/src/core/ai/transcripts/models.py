"""A conversation as pydantic-ai records it, step by step, with where it came from and a pointer into it.

pydantic-ai's `ModelMessage` is the transcript. A source records the history once per step (once per model request), and
each step's copy is the history exactly as the model was given it at that moment. Between steps that history may have been
edited by compaction, by the harness, or by anything else that rewrites what the model sees. Every step's copy is kept,
and every message carries the ordinal its source knows it by: the same ordinal is the same message in every step, whatever
form it has there.

Two views come out of that. `messages` is what the model sees now, the last step. `recorded()` is what happened: every
message once, in the form of the first step that held it, so a tool's error text is read before a later step blanked it.
A `Span` names a step, a message in that step and a part in that message, and `seen_at(span)` is what the model had in
front of it at that point.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai.messages import ModelMessage, ModelRequestPart, ModelResponsePart, ToolCallPart, ToolReturnPart
from pydantic_ai.models import ModelRequestParameters


class Span(BaseModel):
    """A pointer into a transcript: which step, which message in that step, which part in that message. All from zero."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: int = Field(ge=0)
    message: int = Field(ge=0)
    part: int = Field(ge=0)


class Step(BaseModel):
    """The history as the model was given it at one step, and what the request was sent with."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    messages: tuple[ModelMessage, ...] = Field(min_length=1)
    ordinals: tuple[int, ...] = Field(
        description="One per message: the number the source knows that message by across every step. Same ordinal, same message."
    )
    parameters: ModelRequestParameters | None = Field(
        default=None, description="What the request was sent with, the tool definitions among it, when the source recorded it."
    )
    model: str | None = Field(default=None, description="The model the request was sent to, when the source recorded it.")

    @model_validator(mode="after")
    def one_ordinal_per_message_each_once(self) -> Self:
        """Check that the ordinals pair one to one with the messages.

        Returns:
            The step, unchanged.

        Raises:
            ValueError: when there are more or fewer ordinals than messages, or the same ordinal twice.
        """
        if len(self.ordinals) != len(self.messages):
            msg = f"a step carries one ordinal per message, got {len(self.ordinals)} for {len(self.messages)} messages"
            raise ValueError(msg)
        seen: set[int] = set()
        for ordinal in self.ordinals:
            if ordinal in seen:
                msg = f"a step lists ordinal {ordinal} twice"
                raise ValueError(msg)
            seen.add(ordinal)
        return self

    @model_validator(mode="after")
    def every_answer_follows_one_call(self) -> Self:
        """Check that every tool answer follows exactly one earlier call.

        Returns:
            The step, unchanged.

        Raises:
            ValueError: when an answer has no earlier call, a call is answered twice, or a call id is used twice.
        """
        called: set[str] = set()
        answered: set[str] = set()
        for message_index, message in enumerate(self.messages):
            for part in message.parts:
                if isinstance(part, ToolCallPart):
                    if part.tool_call_id in called:
                        msg = f"tool call id {part.tool_call_id!r} is used twice"
                        raise ValueError(msg)
                    called.add(part.tool_call_id)
                elif isinstance(part, ToolReturnPart):
                    if part.tool_call_id not in called:
                        msg = f"answer to {part.tool_call_id!r} at message {message_index} has no earlier call"
                        raise ValueError(msg)
                    if part.tool_call_id in answered:
                        msg = f"tool call {part.tool_call_id!r} is answered twice"
                        raise ValueError(msg)
                    answered.add(part.tool_call_id)
        return self


class Call(BaseModel):
    """One tool call and what answered it.

    The answer is the call's return, whose outcome says how it ended (a retry that asks for the call again is a return too), or
    None when the transcript ended before one came, and `answered_at` is where that answer is.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    at: Span
    call: ToolCallPart
    answer: ToolReturnPart | None
    answered_at: Span | None
    duration_ms: int | None


class Transcript(BaseModel):
    """A whole conversation, one copy of the history per step, with its source."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    source: str = Field(
        pattern=r"^[a-z][a-z0-9-]*:.+$",
        description="Where to find the original: `<scheme>:<locator>`, such as `record:harness-author-9fb3731a` or "
        "`claude-code:<session id>`. The loader registered for the scheme can reopen it.",
    )
    steps: tuple[Step, ...] = Field(min_length=1, description="The history as given to the model at each step, in order.")
    durations_ms: Mapping[str, int] = Field(
        default_factory=dict,
        description="Wall time per tool call id, when the source's event stream recorded it. No message format carries it.",
    )

    @property
    def messages(self) -> tuple[ModelMessage, ...]:
        """What the model sees now: the history of the last step."""
        return self.steps[-1].messages

    def part_at(self, span: Span) -> ModelRequestPart | ModelResponsePart:
        """The part a span points at.

        Args:
            span: The step, the message in it and the part in that message.

        Returns:
            The request or response part at that place.

        Raises:
            IndexError: when the span is outside the transcript.
        """
        return self.steps[span.step].messages[span.message].parts[span.part]

    def seen_at(self, span: Span) -> tuple[ModelMessage, ...]:
        """What the model had in front of it when the part at `span` was produced.

        Args:
            span: Where the produced part is.

        Returns:
            That step's history up to the span's message, without it.
        """
        return self.steps[span.step].messages[: span.message]

    def recorded(self) -> Iterator[tuple[Span, ModelMessage]]:
        """Every message once, in the form of the first step that held it.

        Yields:
            The span of that recording, its part at zero, and the message.
        """
        seen: set[int] = set()
        for step_index, step in enumerate(self.steps):
            for message_index, (ordinal, message) in enumerate(zip(step.ordinals, step.messages, strict=True)):
                if ordinal in seen:
                    continue
                seen.add(ordinal)
                yield Span(step=step_index, message=message_index, part=0), message

    def calls(self) -> tuple[Call, ...]:
        """Every tool call in order, each with its answer as first recorded.

        Returns:
            One call per tool call, with where its answer is and its duration when the source recorded one.
        """
        answers: dict[str, tuple[Span, ToolReturnPart]] = {}
        found: list[tuple[Span, ToolCallPart]] = []
        for span, message in self.recorded():
            for part_index, part in enumerate(message.parts):
                at = Span(step=span.step, message=span.message, part=part_index)
                if isinstance(part, ToolCallPart):
                    found.append((at, part))
                elif isinstance(part, ToolReturnPart):
                    answers.setdefault(part.tool_call_id, (at, part))
        calls: list[Call] = []
        for at, call in found:
            answered = answers.get(call.tool_call_id)
            calls.append(
                Call(
                    at=at,
                    call=call,
                    answer=answered[1] if answered is not None else None,
                    answered_at=answered[0] if answered is not None else None,
                    duration_ms=self.durations_ms.get(call.tool_call_id),
                )
            )
        return tuple(calls)
