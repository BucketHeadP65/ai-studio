"""The rules: detectors that are code, no model, free. Each reads a transcript and produces leads.

A rule is a pydantic-evals evaluator built from the tool facts. It appends leads to the inspected transcript and returns
one count per symptom for the report. A rule that needs a fact the application did not declare stays silent.
"""

from __future__ import annotations

import json

from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING, Final

from pydantic_ai.messages import (
    CompactionPart,
    ModelMessage,
    ModelRequestPart,
    ModelResponse,
    ModelResponsePart,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_evals.evaluators import Evaluator

from core.ai.inspection.detectors import Inspected, InspectionContext, Rules, record_lead
from core.ai.inspection.domain.taxonomy import TAXONOMY, Symptom
from core.ai.transcripts.contracts import Call, Span, Transcript

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

SCHEMA_REJECTED_ARGUMENTS: Final = TAXONOMY.symptom("schema_rejected_arguments")
RETRIED_AFTER_CLEAR_ERROR: Final = TAXONOMY.symptom("retried_after_clear_error")
DENIED_ACTION_REPEATED: Final = TAXONOMY.symptom("denied_action_repeated")
DUPLICATE_CALL: Final = TAXONOMY.symptom("duplicate_call")
RESULT_OVERFLOWED_CONTEXT: Final = TAXONOMY.symptom("result_overflowed_context")
BUDGET_EXHAUSTED_UNFINISHED: Final = TAXONOMY.symptom("budget_exhausted_unfinished")


def call_key(call: ToolCallPart) -> str:
    """The tool and its arguments as one string: two calls with the same meaning have the same key whatever the key order.

    Args:
        call: The tool call.

    Returns:
        The key.
    """
    return f"{call.tool_name}:{json.dumps(call.args_as_dict(), sort_keys=True)}"


def _failed(call: Call) -> bool:
    """Whether the answer was a failed return or a schema refusal."""
    return isinstance(call.answer, RetryPromptPart) or (isinstance(call.answer, ToolReturnPart) and call.answer.outcome == "failed")


def _succeeded(call: Call) -> bool:
    return isinstance(call.answer, ToolReturnPart) and call.answer.outcome == "success"


def _before(a: Span, b: Span) -> bool:
    return (a.step, a.message, a.part) < (b.step, b.message, b.part)


def _keyed(transcript: Transcript) -> list[tuple[Call, str]]:
    """Every call with its key, computed once, for the rules that compare every call against every other."""
    return [(call, call_key(call.call)) for call in transcript.calls()]


def _answered_or_called(call: Call) -> Span:
    """Where the answer is, or the call itself when the transcript ended before one came."""
    return call.answered_at if call.answered_at is not None else call.at


def _between(transcript: Transcript, start: Span, end: Span) -> Iterator[tuple[Span, ModelRequestPart | ModelResponsePart]]:
    """Every part recorded after `start` and before `end`, with its span."""
    for span, message in transcript.recorded():
        for part_index, part in enumerate(message.parts):
            at = Span(step=span.step, message=span.message, part=part_index)
            if _before(start, at) and _before(at, end):
                yield at, part


def _count(ctx: InspectionContext, symptom: Symptom, pairs: list[tuple[Span, ...]], *, found_by: str) -> dict[str, int]:
    for spans in pairs:
        record_lead(ctx, symptom, spans, found_by=found_by)
    return {symptom.name: len(pairs)}


@dataclass
class SchemaRejectedArguments(Evaluator[str, Inspected, None]):
    """The schema refused the call before the tool ran: its answer is a retry prompt."""

    def evaluate(self, ctx: InspectionContext) -> dict[str, int]:
        """One lead per call the schema refused, at the retry prompt that carries the refusal text.

        Args:
            ctx: The rule's context, whose output receives the leads.

        Returns:
            The number of leads recorded, by symptom name.
        """
        hits = [(_answered_or_called(call),) for call in ctx.output.transcript.calls() if isinstance(call.answer, RetryPromptPart)]
        return _count(ctx, SCHEMA_REJECTED_ARGUMENTS, hits, found_by="schema_rejected_arguments")


@dataclass
class RetriedAfterError(Evaluator[str, Inspected, None]):
    """The same call, right after the same call failed. Whether the error text was clear is the inspector's question."""

    def evaluate(self, ctx: InspectionContext) -> dict[str, int]:
        """One lead per adjacent pair where the first failed and the second repeats it.

        Args:
            ctx: The rule's context, whose output receives the leads.

        Returns:
            The number of leads recorded, by symptom name.
        """
        calls = ctx.output.transcript.calls()
        hits = [
            (first.at, second.at) for first, second in pairwise(calls) if _failed(first) and call_key(first.call) == call_key(second.call)
        ]
        return _count(ctx, RETRIED_AFTER_CLEAR_ERROR, hits, found_by="retried_after_clear_error")


@dataclass
class DeniedActionRepeated(Evaluator[str, Inspected, None]):
    """A denied call, then the same ask, anywhere later."""

    def evaluate(self, ctx: InspectionContext) -> dict[str, int]:
        """One lead per denied call that some later call repeats.

        Args:
            ctx: The rule's context, whose output receives the leads.

        Returns:
            The number of leads recorded, by symptom name.
        """
        keyed = _keyed(ctx.output.transcript)
        hits: list[tuple[Span, ...]] = []
        for index, (denied, key) in enumerate(keyed):
            if not (isinstance(denied.answer, ToolReturnPart) and denied.answer.outcome == "denied"):
                continue
            repeat = next((later for later, other in keyed[index + 1 :] if other == key), None)
            if repeat is not None:
                hits.append((denied.at, repeat.at))
        return _count(ctx, DENIED_ACTION_REPEATED, hits, found_by="denied_action_repeated")


@dataclass
class DuplicateCall(Evaluator[str, Inspected, None]):
    """The same call again after it succeeded, with nothing in between that could have changed the answer."""

    world_changing: frozenset[str] = field(default_factory=frozenset)
    """The tool names whose call may change what a repeated call answers."""

    def evaluate(self, ctx: InspectionContext) -> dict[str, int]:
        """One lead per call that repeats an earlier success with nothing that could have changed the answer between them.

        Args:
            ctx: The rule's context, whose output receives the leads.

        Returns:
            The number of leads recorded, by symptom name.
        """
        transcript = ctx.output.transcript
        keyed = _keyed(transcript)
        hits: list[tuple[Span, ...]] = []
        for index, (later, key) in enumerate(keyed):
            earlier = next((first for first, other in reversed(keyed[:index]) if other == key), None)
            if earlier is None or not _succeeded(earlier):
                continue
            if any(self._resets(part) for _, part in _between(transcript, earlier.at, later.at)):
                continue
            hits.append((earlier.at, later.at))
        return _count(ctx, DUPLICATE_CALL, hits, found_by="duplicate_call")

    def _resets(self, part: ModelRequestPart | ModelResponsePart) -> bool:
        """Whether a part between two calls means the world may have changed."""
        if isinstance(part, CompactionPart):
            return True
        return isinstance(part, ToolCallPart) and part.tool_name in self.world_changing


@dataclass
class ResultOverflowedContext(Evaluator[str, Inspected, None]):
    """A return over the cap, followed by a compaction or by a step that holds less than the one before."""

    cap_chars: int | None = None
    """The return size in characters past which a return is oversized. None keeps the rule silent."""

    def evaluate(self, ctx: InspectionContext) -> dict[str, int]:
        """One lead per oversized return followed by a compaction or a step that shrank.

        Args:
            ctx: The rule's context, whose output receives the leads.

        Returns:
            The number of leads recorded, by symptom name.
        """
        transcript = ctx.output.transcript
        if self.cap_chars is None:
            return {RESULT_OVERFLOWED_CONTEXT.name: 0}
        hits = [
            (call.answered_at,)
            for call in transcript.calls()
            if call.answered_at is not None
            and isinstance(call.answer, ToolReturnPart)
            and len(call.answer.model_response_str()) > self.cap_chars
            and (_compaction_after(transcript, call.answered_at) or _a_step_shrank(transcript, call.answered_at))
        ]
        return _count(ctx, RESULT_OVERFLOWED_CONTEXT, hits, found_by="result_overflowed_context")


@dataclass
class BudgetExhaustedUnfinished(Evaluator[str, Inspected, None]):
    """The last response was cut by length and carried no text: the run ran out before it finished."""

    def evaluate(self, ctx: InspectionContext) -> dict[str, int]:
        """One lead when the last step's last response was cut by length with no text part, at the call it was writing.

        Args:
            ctx: The rule's context, whose output receives the leads.

        Returns:
            The number of leads recorded, by symptom name.
        """
        steps = ctx.output.transcript.steps
        last_step = len(steps) - 1
        found = _last_response(steps[last_step].messages)
        if found is None:
            return _count(ctx, BUDGET_EXHAUSTED_UNFINISHED, [], found_by="budget_exhausted_unfinished")
        message_index, last = found
        cut = last.finish_reason == "length" and not any(isinstance(part, TextPart) for part in last.parts)
        part_index = next((index for index, part in enumerate(last.parts) if isinstance(part, ToolCallPart)), 0)
        hits = [(Span(step=last_step, message=message_index, part=part_index),)] if cut else []
        return _count(ctx, BUDGET_EXHAUSTED_UNFINISHED, hits, found_by="budget_exhausted_unfinished")


def _last_response(messages: Sequence[ModelMessage]) -> tuple[int, ModelResponse] | None:
    """The last model response of a step and where it sits, or None when the step holds none."""
    for index in reversed(range(len(messages))):
        message = messages[index]
        if isinstance(message, ModelResponse):
            return index, message
    return None


def _compaction_after(transcript: Transcript, span: Span) -> bool:
    return any(isinstance(part, CompactionPart) for at, message in transcript.recorded() for part in message.parts if _before(span, at))


def _a_step_shrank(transcript: Transcript, span: Span) -> bool:
    """Whether a step after the one `span` points at holds less than the step before it."""
    steps = transcript.steps
    return any(len(steps[index].messages) < len(steps[index - 1].messages) for index in range(span.step + 1, len(steps)))


RULES: Final = Rules(
    by_key={
        "schema_rejected_arguments": lambda _facts: SchemaRejectedArguments(),
        "retried_after_clear_error": lambda _facts: RetriedAfterError(),
        "denied_action_repeated": lambda _facts: DeniedActionRepeated(),
        "duplicate_call": lambda facts: DuplicateCall(world_changing=facts.world_changing),
        "result_overflowed_context": lambda facts: ResultOverflowedContext(cap_chars=facts.result_cap_chars),
        "budget_exhausted_unfinished": lambda _facts: BudgetExhaustedUnfinished(),
    }
)
