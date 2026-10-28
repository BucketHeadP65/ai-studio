"""What the inspector reads: the run in one screen, a slice of it in full, a search over it, and the view a lead opens on.

Every position is a span written `step.message.part`, the transcript's own coordinates. Every string the inspector sees
is built here and nowhere else, so the view a lead opens on and the answer a tool gives read the same way.
"""

from __future__ import annotations

import re

from dataclasses import dataclass
from itertools import islice
from typing import TYPE_CHECKING, Final, Literal

from pydantic import JsonValue, TypeAdapter, ValidationError
from pydantic_ai.messages import (
    CompactionPart,
    InstructionDeltaPart,
    ModelMessage,
    ModelRequest,
    ModelRequestPart,
    ModelResponse,
    ModelResponsePart,
    RetryFeedbackPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters

from core.ai.inspection.domain.taxonomy import TAXONOMY, Taxonomy
from core.ai.transcripts.contracts import Span

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from core.ai.inspection.domain.leads import Lead
    from core.ai.inspection.domain.taxonomy import Symptom
    from core.ai.transcripts.contracts import Call, RunSummary, Step, Transcript

PART_CHARS: Final = 2_000
"""How much rendered evidence one read carries before its continuation."""
EVIDENCE_CHARS: Final = 20_000
"""How much of one part's rendered content a finding's evidence carries."""
OUTLINE_CHARS: Final = 200
"""How much of one prompt, answer or summary line an outline carries."""
FIND_LIMIT: Final = 50
"""How many matches a search answers before it stops."""
WINDOW: Final = 12
"""How many messages before the lead's last span the moment view carries."""
_SPAN_PIECES: Final = 3
"""How many dot-separated numbers a span reads: step, message, part."""

QUESTIONS: Final = (
    "A1. Did a tool, the infrastructure, the outside host or the state the tool depends on misbehave, judged against what the tool should have answered?",
    "A2. Did the agent have the fact it needed, in what it could see, at that point?",
    "A3. Do the instructions it was given cover this situation, and say the right thing?",
    "A4. Did the tool's definition make the mistake easy?",
    "A5. Was the request ambiguous or contradictory so that any reading would have failed?",
    "A6. Does the evidence support an agent-reasoning hypothesis? A stronger model succeeding requires a separate replay to test.",
)

type ReadResource = Literal["messages", "instructions", "tools"]

_REQUEST_PARAMETERS: Final = TypeAdapter(ModelRequestParameters)
_PARAMETER_FIELDS: Final = TypeAdapter(dict[str, JsonValue])

_ENDED: Final[Mapping[str, str]] = {"success": "answered", "failed": "raised", "denied": "denied", "interrupted": "interrupted"}
"""How a tool return's outcome reads in an outline."""
_RETRIED: Final[Mapping[str, str]] = {
    "validation_error": "rejected by the schema",
    "model_retry": "refused by the tool",
    "unknown_tool": "an unknown tool",
    "unavailable_tool": "not available yet",
}
"""Why a retried return asks for its call again, by the retry's cause."""
_RETRIED_UNKNOWN: Final = "asked again"
"""How a retried return reads when it does not say why."""
_WITHDRAWN: Final = "This instruction was withdrawn."
"""How an instruction change that withdraws its block reads."""


def text_of(part: ModelRequestPart | ModelResponsePart) -> str:
    """A part as text: its content, or its arguments for a call. A part with neither reads empty.

    Args:
        part: The message part to read.

    Returns:
        The part's text.
    """
    if isinstance(part, ToolCallPart):
        return str(part.args_as_dict())
    if isinstance(part, InstructionDeltaPart):
        return part.content if part.content is not None else _WITHDRAWN
    if isinstance(part, TextPart | SystemPromptPart | ThinkingPart | RetryFeedbackPart | ToolReturnPart | UserPromptPart | CompactionPart):
        return str(part.content)
    return ""


def kind_of(part: ModelRequestPart | ModelResponsePart) -> str:
    """A part's kind as a reader sees it: the pydantic-ai part kind, and for a retried return why it asks for its call again.

    Args:
        part: The message part.

    Returns:
        The kind, such as `tool-call` or `tool-return, rejected by the schema`.
    """
    if isinstance(part, ToolReturnPart) and part.outcome == "retried":
        return f"{part.part_kind}, {_retried(part)}"
    return part.part_kind


def _retried(part: ToolReturnPart) -> str:
    """Why a retried return asks for its call again, in the words an outline uses."""
    return _RETRIED_UNKNOWN if part.retry is None else _RETRIED.get(part.retry.cause, part.retry.cause)


_WRITER_OF: Final[Mapping[type, str]] = {
    UserPromptPart: "the user's prompt",
    SystemPromptPart: "the system prompt",
    TextPart: "the model said",
    ThinkingPart: "the model's thinking",
}
"""What wrote a part, for the kinds a fixed phrase names."""


def label_of(span: Span, part: ModelRequestPart | ModelResponsePart) -> str:
    """What a part is, in words a reader of the evidence needs: the step and who wrote it.

    Args:
        span: Where the part sits.
        part: The message part.

    Returns:
        The label, such as `step 3, the model said`.
    """
    return f"step {span.step}, {_writer_of(part)}"


def _writer_of(part: ModelRequestPart | ModelResponsePart) -> str:
    """Who wrote a part and what it is: the words that follow `step N,` in its label."""
    fixed = _WRITER_OF.get(type(part))
    if fixed is not None:
        return fixed
    if isinstance(part, ToolCallPart):
        return f"the model's call to {part.tool_name}"
    if isinstance(part, ToolReturnPart):
        return f"the retry asked of {part.tool_name}" if part.outcome == "retried" else f"the return of {part.tool_name}"
    if isinstance(part, RetryFeedbackPart):
        return "the retry feedback part"
    return part.part_kind


def shown(part: ModelRequestPart | ModelResponsePart, *, limit: int = EVIDENCE_CHARS) -> str:
    """A part's content as a person reads it, cut at `limit` characters with a tail saying how many were left.

    Structured content renders as an indented tree. A string that holds JSON unfolds into that tree first, and
    every line break is a real one, never escaped.

    Args:
        part: The message part to show.
        limit: The most characters shown before the tail.

    Returns:
        The rendered content.
    """
    rendered = _render(_unfold(_value_of(part)))
    if len(rendered) <= limit:
        return rendered
    left = len(rendered) - limit
    return f"{rendered[:limit]}\n... {left} more characters"


_JSON: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)
"""Parses and validates JSON-shaped data crossing the boundary from a part pydantic_ai leaves loosely typed."""


def _as_json(value: object) -> JsonValue:
    """`value` as JSON-shaped data, or its string when it holds something JSON cannot represent."""
    try:
        return _JSON.validate_python(value)
    except ValidationError:
        return str(value)


def _value_of(part: ModelRequestPart | ModelResponsePart) -> JsonValue:
    """The value `shown` renders: a call's arguments, a part's content, or the lines a sequence of user content reads as."""
    if isinstance(part, ToolCallPart):
        return _as_json(part.args_as_dict())
    if isinstance(part, UserPromptPart):
        if isinstance(part.content, str):
            return part.content
        return "\n".join(item if isinstance(item, str) else f"[{type(item).__name__}]" for item in part.content)
    if isinstance(part, ToolReturnPart | RetryFeedbackPart):
        return _as_json(part.content)
    if isinstance(part, TextPart | ThinkingPart | SystemPromptPart):
        return part.content
    return text_of(part)


def _unfold(value: JsonValue) -> JsonValue:
    """`value` with every string that holds JSON turned into what it holds, recursively."""
    if isinstance(value, str):
        try:
            parsed = _JSON.validate_json(value)
        except ValidationError:
            return value
        return _unfold(parsed) if isinstance(parsed, dict | list) else value
    if isinstance(value, dict):
        return {key: _unfold(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unfold(item) for item in value]
    return value


def _render(value: JsonValue, *, level: int = 0) -> str:
    """`value` as an indented tree, two spaces per level, with `key: value` lines for a dict and `- ` lines for a list."""
    if isinstance(value, dict):
        return "\n".join(_dict_entry(key, item, level) for key, item in value.items()) if value else "{}"
    if isinstance(value, list):
        return "\n".join(_list_entry(item, level) for item in value) if value else "[]"
    return _scalar(value)


def _dict_entry(key: str, value: JsonValue, level: int) -> str:
    """One `key: value` line of a dict, the value nested under it when it does not fit the line."""
    indent = "  " * level
    if isinstance(value, dict | list) and value:
        return f"{indent}{key}:\n{_render(value, level=level + 1)}"
    if isinstance(value, str) and "\n" in value:
        return f"{indent}{key}:\n{_indented(value, level + 1)}"
    return f"{indent}{key}: {_scalar(value)}"


def _list_entry(value: JsonValue, level: int) -> str:
    """One `- ` line of a list, the value nested under it when it does not fit the line."""
    indent = "  " * level
    if isinstance(value, dict | list) and value:
        return f"{indent}-\n{_render(value, level=level + 1)}"
    if isinstance(value, str) and "\n" in value:
        return f"{indent}-\n{_indented(value, level + 1)}"
    return f"{indent}- {_scalar(value)}"


def _indented(text: str, level: int) -> str:
    """Every line of `text`, indented one level."""
    pad = "  " * level
    return "\n".join(f"{pad}{line}" for line in text.splitlines())


def _scalar(value: JsonValue) -> str:
    """A value that sits on its key's or dash's own line: an empty container literally, `null`, `true`/`false`, or itself."""
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return str(value)


def told(messages: Sequence[ModelMessage]) -> str:
    """Every system prompt and every set of instructions in the history, in order and once each.

    Args:
        messages: The history to read.

    Returns:
        One line per distinct prompt or set of instructions, or `(none recorded)`.
    """
    said: list[str] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        if message.instructions:
            said.append(f"[instructions] {message.instructions}")
        said.extend(f"[system-prompt] {part.content}" for part in message.parts if isinstance(part, SystemPromptPart))
    return "\n".join(dict.fromkeys(said)) or "(none recorded)"


def rendered(messages: Sequence[ModelMessage], *, part_chars: int = PART_CHARS) -> str:
    """A run of messages, one line per part, each cut at `part_chars`.

    Args:
        messages: The messages to render.
        part_chars: The most characters kept of each part.

    Returns:
        The rendered lines.
    """
    lines: list[str] = []
    for message in messages:
        label = "model" if isinstance(message, ModelResponse) else "request"
        if isinstance(message, ModelRequest) and message.instructions:
            lines.append(f"[instructions] {message.instructions[:part_chars]}")
        lines.extend(f"[{label} {kind_of(part)}] {text_of(part)[:part_chars]}" for part in message.parts)
    return "\n".join(lines)


def parse_span(text: str, transcript: Transcript) -> Span:
    """A span written `step.message.part`, checked against the transcript.

    Args:
        text: The span as written.
        transcript: The transcript the span must point into.

    Returns:
        The parsed span.

    Raises:
        ValueError: when the text is not three whole numbers separated by dots, or points outside the transcript.
    """
    pieces = text.split(".")
    if len(pieces) != _SPAN_PIECES or not all(piece.isdigit() for piece in pieces):
        msg = f"a span reads step.message.part, not {text!r}"
        raise ValueError(msg)
    step, message, part = (int(piece) for piece in pieces)
    span = Span(step=step, message=message, part=part)
    try:
        transcript.part_at(span)
    except IndexError as outside:
        msg = f"span {text} is not in {transcript.source}"
        raise ValueError(msg) from outside
    return span


@dataclass(frozen=True)
class StepOutline:
    """One step of the run in one line: how many messages the run added there, and what happened in them."""

    step: int
    messages: int
    """How many messages were first recorded at this step."""
    pieces: tuple[str, ...]
    """What was asked, called (and how the call ended) and said, in order. Empty when the step added nothing."""


def outline_steps(transcript: Transcript) -> list[StepOutline]:
    """The run in one screen, one entry per step.

    Every message appears once, under the step that first held it, so a later step's copy of the history does not repeat.

    Args:
        transcript: The run to outline.

    Returns:
        One outline entry per step, in order.
    """
    ended = {call.at: _ended(call) for call in transcript.calls()}
    counts: dict[int, int] = dict.fromkeys(range(len(transcript.steps)), 0)
    per_step: dict[int, list[str]] = {index: [] for index in range(len(transcript.steps))}
    for span, message in transcript.recorded():
        counts[span.step] += 1
        for part_index, part in enumerate(message.parts):
            at = Span(step=span.step, message=span.message, part=part_index)
            piece = _outline_part(part, at, ended)
            if piece is not None:
                per_step[span.step].append(piece)
    return [StepOutline(step=index, messages=counts[index], pieces=tuple(per_step[index])) for index in range(len(transcript.steps))]


def outline(transcript: Transcript, *, offset: int = 0) -> str:
    """One bounded page of the run's outline, with a next call when more steps or events remain.

    Args:
        transcript: The run to outline.
        offset: The character the page starts at.

    Returns:
        The page, followed by the call for the rest when more remains.

    Raises:
        ValueError: when offset is outside the outline.
    """
    body = "\n".join(f"step {line.step}: {', '.join(line.pieces) or '(nothing new)'}" for line in outline_steps(transcript))
    return _page(body, offset, EVIDENCE_CHARS, f"outline(run={transcript.source!r}, offset={offset + EVIDENCE_CHARS})")


def step_at(transcript: Transcript, step: int) -> Turn:
    """The messages the run added at one step, in order, none cited, with the history index they start at.

    Args:
        transcript: The run to read.
        step: The step's index.

    Returns:
        The step's new messages as a turn.

    Raises:
        ValueError: when the transcript has no such step.
    """
    if step < 0 or step >= len(transcript.steps):
        msg = f"no step {step} in {transcript.source}: it has {len(transcript.steps)} steps"
        raise ValueError(msg)
    history = transcript.steps[step].messages
    indices = [span.message for span, _ in transcript.recorded() if span.step == step]
    return Turn(step=step, messages=tuple(history[index] for index in indices), cited=(), first=indices[0] if indices else len(history))


def read(
    transcript: Transcript, step: int, start: int = 0, end: int | None = None, *, resource: ReadResource = "messages", offset: int = 0
) -> str:
    """Read a bounded page of messages, recorded instructions, or complete tool definitions.

    Tool definitions belong to the step, so their read ignores the message range.

    Args:
        transcript: The run to read.
        step: The step's index.
        start: The first message index of the half-open range.
        end: The message index the range stops before, or `None` for the rest of the history.
        resource: Which evidence to read: messages, instructions or tools.
        offset: The character the page starts at, counted in the full selected resource.

    Returns:
        The page, followed by the call for the rest when more remains.

    Raises:
        ValueError: when the step or offset is outside the recorded evidence.
    """
    if step < 0 or step >= len(transcript.steps):
        msg = f"no step {step} in {transcript.source}: it has {len(transcript.steps)} steps. Choose a step from outline()"
        raise ValueError(msg)
    _check_offset(offset)
    selected = transcript.steps[step]
    stop = len(selected.messages) if end is None else end
    body = _read_content(step, selected, start, stop, resource)
    arguments = _read_arguments(step, start, stop, resource)
    return _page(body, offset, PART_CHARS, _read_call(arguments, offset + PART_CHARS, transcript.source))


def _page(body: str, offset: int, limit: int, next_call: str) -> str:
    """A bounded piece of a complete resource and the exact call for the rest, or ValueError when offset is outside it."""
    _check_offset(offset)
    if offset > len(body):
        msg = f"offset {offset} exceeds this resource's {len(body)} characters. Start again with offset=0"
        raise ValueError(msg)
    page = body[offset : offset + limit]
    following = offset + len(page)
    if following < len(body):
        return f"{page}\n... {len(body) - following} more characters. Continue with {next_call}"
    return page or "(end of resource)"


def _read_arguments(step: int, start: int, end: int, resource: ReadResource) -> str:
    """Arguments that identify the same selected evidence on every page."""
    return f"step={step}, start={start}, end={end}, resource={resource!r}"


def _read_call(arguments: str, offset: int, source: str) -> str:
    """An executable read of a selected resource, including its source when it is an earlier run."""
    return f"read({arguments}, offset={offset}, run={source!r})"


def _read_content(index: int, step: Step, start: int, end: int, resource: ReadResource) -> str:
    """The complete selected content, before paging, with stable coordinates."""
    if resource == "tools":
        if step.parameters is None:
            return f"step {index}: tool definitions not recorded. Their absence cannot establish what tools the model had"
        return "\n".join(body for _, body in _tool_sections(step.parameters))
    lines: list[str] = []
    for message_index in range(max(start, 0), min(end, len(step.messages))):
        message = step.messages[message_index]
        if resource == "instructions":
            if isinstance(message, ModelRequest) and message.instructions is not None:
                lines.append(f"{index}.{message_index}.instructions [instructions] {message.instructions}")
            for part_index, part in enumerate(message.parts):
                if isinstance(part, SystemPromptPart):
                    lines.append(f"{index}.{message_index}.{part_index} [system-prompt] {part.content}")
                elif isinstance(part, InstructionDeltaPart):
                    lines.append(f"{index}.{message_index}.{part_index} [instruction-delta {part.id}] {text_of(part)}")
        else:
            lines.extend(
                f"{index}.{message_index}.{part_index} [{kind_of(part)}] {text_of(part)}" for part_index, part in enumerate(message.parts)
            )
    empty = (
        f"step {index} holds no message in [{start}, {end})"
        if resource == "messages"
        else f"step {index}: instructions not recorded in [{start}, {end}); do not infer what the agent was told"
    )
    return "\n".join(lines) or empty


def _tool_sections(parameters: ModelRequestParameters) -> Iterator[tuple[str, str]]:
    """All recorded tool/output parameters as labelled JSON sections, with unordered sets stabilized for paging."""
    fields = _PARAMETER_FIELDS.validate_python(_REQUEST_PARAMETERS.dump_python(parameters, mode="json"))
    fields["revealed_tool_names"] = list[JsonValue](sorted(parameters.revealed_tool_names))
    fields["deferred_capability_ids"] = list[JsonValue](sorted(parameters.deferred_capability_ids))
    for name, value in fields.items():
        if name in ("function_tools", "output_tools", "native_tools") and isinstance(value, list) and value:
            for index, item in enumerate(value):
                label = f"{name}[{index}]"
                yield label, f"[{label}]\n{_JSON.dump_json(item, indent=2).decode()}"
        else:
            yield name, f"[{name}]\n{_JSON.dump_json(value, indent=2).decode()}"


@dataclass(frozen=True)
class _SearchUnit:
    """A searchable piece and where its text begins within a reproducible read."""

    label: str
    text: str
    arguments: str
    before: int = 0


def _search_units(transcript: Transcript) -> Iterator[_SearchUnit]:
    """Message parts once each, instructions in every recorded form, and the tools available at every step."""
    for span, message in transcript.recorded():
        arguments = _read_arguments(span.step, span.message, span.message + 1, "messages")
        before = 0
        for part_index, part in enumerate(message.parts):
            label = f"{span.step}.{span.message}.{part_index}"
            prefix = f"{label} [{kind_of(part)}] "
            body = text_of(part)
            yield _SearchUnit(label=label, text=body, arguments=arguments, before=before + len(prefix))
            before += len(prefix) + len(body) + 1
    seen_instructions: set[tuple[int, str]] = set()
    for index, step in enumerate(transcript.steps):
        for message_index, (ordinal, message) in enumerate(zip(step.ordinals, step.messages, strict=True)):
            if not isinstance(message, ModelRequest) or message.instructions is None:
                continue
            identity = (ordinal, message.instructions)
            if identity in seen_instructions:
                continue
            seen_instructions.add(identity)
            label = f"{index}.{message_index}.instructions"
            yield _SearchUnit(
                label=label,
                text=message.instructions,
                arguments=_read_arguments(index, message_index, message_index + 1, "instructions"),
                before=len(f"{label} [instructions] "),
            )
        if step.parameters is not None:
            before = 0
            for label, body in _tool_sections(step.parameters):
                yield _SearchUnit(
                    label=f"step {index} {label}",
                    text=body,
                    arguments=_read_arguments(index, 0, len(step.messages), "tools"),
                    before=before,
                )
                before += len(body) + 1


def find(transcript: Transcript, text: str, *, offset: int = 0) -> str:
    """Find bounded snippets in parts, instructions and tool definitions, with reads that reach each match.

    Continuations keep the query and the run.

    Args:
        transcript: The run to search.
        text: The phrase to find, case-insensitive.
        offset: How many matching resources to skip, counted in resources, not characters.

    Returns:
        One line per match with its snippet and read call, and a continuation when more remain.

    Raises:
        ValueError: when offset is negative or the query is empty or too long.
    """
    _check_offset(offset)
    if not text.strip() or len(text) > OUTLINE_CHARS:
        msg = f"search text must hold 1 to {OUTLINE_CHARS} characters. Choose a short phrase from the evidence"
        raise ValueError(msg)
    wanted = text.lower()
    hits = (unit for unit in _search_units(transcript) if wanted in unit.text.lower())
    selected = list(islice(hits, offset, offset + FIND_LIMIT + 1))
    lines = [_search_hit(unit, wanted, transcript.source) for unit in selected[:FIND_LIMIT]]
    if len(selected) > FIND_LIMIT:
        lines.append(f"Continue with find(text={text!r}, run={transcript.source!r}, offset={offset + FIND_LIMIT})")
    return "\n".join(lines) or f"nothing holds {text!r}" + (f" after match {offset}" if offset else "")


def _search_hit(unit: _SearchUnit, wanted: str, source: str) -> str:
    """A snippet around the actual match and a read starting just before it."""
    found = unit.text.lower().index(wanted)
    start = max(found - 60, 0)
    snippet = unit.text[start : start + OUTLINE_CHARS].replace("\n", " ")
    location = max(unit.before + found - 60, 0)
    return f"{unit.label} {snippet} | {_read_call(unit.arguments, location, source)}"


def _check_offset(offset: int) -> None:
    """Refuse a backwards continuation with ValueError when offset is negative."""
    if offset < 0:
        msg = "offset must be zero or greater. Start again with offset=0"
        raise ValueError(msg)


def runs_summary(runs: Sequence[RunSummary], *, offset: int = 0) -> str:
    """A bounded page of earlier runs with stable sources and the next listing call.

    Args:
        runs: The earlier runs to list.
        offset: The character the page starts at.

    Returns:
        The page, followed by the call for the rest when more remains.

    Raises:
        ValueError: when offset is outside the listing.
    """
    body = (
        "\n".join(
            f"- {run.source} | agent {run.agent or '(none)'} | started {run.started_at.isoformat()} "
            f"| asked {_cut(run.first_prompt)!r} | ended {run.last_event_type}"
            for run in runs
        )
        or "(none)"
    )
    return _page(body, offset, EVIDENCE_CHARS, f"earlier_runs(offset={offset + EVIDENCE_CHARS})")


def view_for(lead: Lead, transcript: Transcript, taxonomy: Taxonomy = TAXONOMY, earlier: Sequence[RunSummary] = ()) -> str:
    """What the inspector opens on for one lead, as far as the symptom's horizon reaches.

    `moment` is the flagged step, `run` adds the run's outline, `conversation` adds one line per earlier run.

    Args:
        lead: The lead the view opens on.
        transcript: The run the lead points into.
        taxonomy: The taxonomy that holds the lead's symptom.
        earlier: The conversation's earlier runs, listed when the horizon is `conversation`.

    Returns:
        The view text.

    Raises:
        KeyError: when the lead names a symptom the taxonomy does not hold.
    """
    symptom = taxonomy.symptom(lead.symptom)
    pieces = [_moment(lead, transcript, symptom)]
    if symptom.horizon in ("run", "conversation"):
        pieces.append(f"The run, one line per step:\n{outline(transcript)}")
    if symptom.horizon == "conversation":
        pieces.append(f"Earlier runs of this conversation, newest first:\n{runs_summary(earlier)}")
    return "\n\n".join(pieces)


def with_refusal(view: str, refusal: str) -> str:
    """The view again, with what the taxonomy said about the previous answer.

    Args:
        view: The view or prompt asked before.
        refusal: Why the previous answer was refused.

    Returns:
        The view followed by the refusal and a request to answer again.
    """
    return f"{view}\n\nYour previous answer was refused: {refusal}\nAnswer again with names the taxonomy allows."


@dataclass(frozen=True)
class Turn:
    """The messages of one step a reader looks at, and which of them hold a cited part."""

    step: int
    messages: tuple[ModelMessage, ...]
    cited: tuple[int, ...]
    """Indices into `messages` of the ones holding a cited part, ascending, each once."""
    first: int
    """The index in the step's history of the turn's first message."""


def turn_at(transcript: Transcript, step: int, spans: Sequence[Span]) -> Turn:
    """The turn a reader sees for one step.

    The step's history from the last request carrying a user prompt at or before the first cited message (the
    step's first message when none does), through the end of that history, followed by the model's response to
    the step when the next step's history holds it (the message at index `len(steps[step].messages)` of
    `steps[step + 1].messages`). `cited` marks the messages a span of `spans` with this step points at. A span
    whose message index is past the history is ignored.

    Args:
        transcript: The run to read.
        step: The step's index.
        spans: The cited spans, of any step.

    Returns:
        The turn for the step.
    """
    history = transcript.steps[step].messages
    for_step = [span for span in spans if span.step == step and span.message < len(history)]
    first_cited = min((span.message for span in for_step), default=0)
    start = _user_prompt_at_or_before(history, first_cited)
    messages = list(history[start:])
    if step + 1 < len(transcript.steps):
        after = transcript.steps[step + 1].messages
        if len(after) > len(history):
            messages.append(after[len(history)])
    cited = tuple(sorted({span.message - start for span in for_step}))
    return Turn(step=step, messages=tuple(messages), cited=cited, first=start)


def _user_prompt_at_or_before(history: Sequence[ModelMessage], index: int) -> int:
    """The last index at or before `index` holding a user prompt, or 0 when none does."""
    for position in range(index, -1, -1):
        message = history[position]
        if isinstance(message, ModelRequest) and any(isinstance(part, UserPromptPart) for part in message.parts):
            return position
    return 0


def turns_for(transcript: Transcript, spans: Sequence[Span]) -> list[Turn]:
    """One `Turn` per distinct step among `spans`, ascending.

    Args:
        transcript: The run to read.
        spans: The cited spans.

    Returns:
        The turns in step order.
    """
    return [turn_at(transcript, step, spans) for step in sorted({span.step for span in spans})]


_COORD_RUN: Final = re.compile(r"(?:\b(?:at|in)\s+)?\b\d+\.\d+\.\d+\b(?:\s*(?:/|,|\band\b|\bor\b)\s*\b\d+\.\d+\.\d+\b)*")
"""One or more coordinates in a row, with the preposition that introduced them."""
_EMPTY_PARENS: Final = re.compile(r"\(\s*\)")
_SPACE_INSIDE_PARENS_OPEN: Final = re.compile(r"\(\s+")
_SPACE_INSIDE_PARENS_CLOSE: Final = re.compile(r"\s+\)")
_SPACE_BEFORE_STOP: Final = re.compile(r"\s+([,.;:])")
_DOUBLE_SPACE: Final = re.compile(r" {2,}")


def plain_whys(whys: Sequence[str]) -> str:
    """The blames' sentences as a fallback explanation, with every `step.message.part` coordinate gone.

    A run of coordinates (joined by `/`, `,`, `and` or `or`) goes with the `at` or `in` before it. A parenthesis left empty
    goes too. Spaces collapse and no space is left before a comma or a full stop. The sentences join with one space.

    Args:
        whys: The blames' evidence sentences.

    Returns:
        The cleaned sentences as one paragraph.
    """
    cleaned: list[str] = []
    for why in whys:
        text = _COORD_RUN.sub("", why)
        text = _EMPTY_PARENS.sub("", text)
        text = _SPACE_INSIDE_PARENS_OPEN.sub("(", text)
        text = _SPACE_INSIDE_PARENS_CLOSE.sub(")", text)
        text = _SPACE_BEFORE_STOP.sub(r"\1", text)
        cleaned.append(_DOUBLE_SPACE.sub(" ", text).strip())
    return " ".join(part for part in cleaned if part)


def root_causes(symptom: Symptom) -> str:
    """One line per root cause the symptom allows: what it means, and which kinds of surface a fix for it lands on.

    Args:
        symptom: The symptom whose root causes are listed.

    Returns:
        The lines, joined.
    """
    return "\n".join(
        f"- {cause.name}: {cause.meaning} Fix lands on: {', '.join(kind.value for kind in cause.fix_lands_on)}."
        for cause in symptom.possible_root_causes
    )


def _moment(lead: Lead, transcript: Transcript, symptom: Symptom) -> str:
    """The flagged step: the symptom, its candidates, the questions, the tools there, what the agent was told, the window."""
    first = lead.spans[0]
    candidates = root_causes(symptom)
    tools = read(transcript, first.step, resource="tools")
    history = transcript.seen_at(lead.spans[-1])
    seen = history[-WINDOW:]
    pointed = "\n".join(
        read(transcript, step, message, message + 1) for step, message in dict.fromkeys((span.step, span.message) for span in lead.spans)
    )
    instructions = read(transcript, lead.spans[-1].step, 0, len(history), resource="instructions")
    window = read(transcript, lead.spans[-1].step, len(history) - len(seen), len(history))
    return (
        f"Symptom: {symptom.name}. {symptom.what_you_see}\n\n"
        f"Candidate root causes:\n{candidates}\n\n"
        f"Questions:\n" + "\n".join(QUESTIONS) + "\n\n"
        f"Tools the model could call at that step:\n{tools}\n\n"
        f"What the agent was told:\n{instructions}\n\n"
        f"What the model had in front of it (last {len(seen)} messages):\n{window}\n\n"
        f"The parts the detector pointed at:\n{pointed}"
    )


def _outline_part(part: ModelRequestPart | ModelResponsePart, at: Span, ended: Mapping[Span, str]) -> str | None:
    """One piece of a step's line: the prompt, the call with how it ended, or the model's text. Anything else is silent."""
    if isinstance(part, UserPromptPart):
        return f"asked {_cut(str(part.content))!r}"
    if isinstance(part, ToolCallPart):
        return f"called {part.tool_name} ({ended.get(at, 'unanswered')})"
    if isinstance(part, TextPart):
        return f"said {_cut(part.content)!r}"
    return None


def _ended(call: Call) -> str:
    """How a call ended: answered, raised, denied, interrupted, asked again and why, or never answered."""
    if call.answer is None:
        return "unanswered"
    if call.answer.outcome == "retried":
        return _retried(call.answer)
    return _ENDED.get(call.answer.outcome, call.answer.outcome)


def _cut(text: str) -> str:
    """One line of text, short enough for an outline."""
    return text.replace("\n", " ")[:OUTLINE_CHARS]
