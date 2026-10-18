"""The session-event grammar: what happened in a session, typed, and the projections over it.

**Model-visible means logged**: anything that changes what the model sees on the next turn leaves
an event. Events carry references (a run id, a plugin name), never content. Message text lives in
the transcript, authority state in its own files. A run's own lifecycle belongs to the workflow
that ran (`core.ai.workflows`), and the session log records only that the session launched it.
Everything here is pure: the codec maps lines to events and back, and the projections answer
questions over a sequence.
"""

from __future__ import annotations

import json
import uuid

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence


class _Event(BaseModel):
    """Every event is frozen and closed: an unknown key is a different event, not extra baggage."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Booted(_Event):
    """The process came up, and which transcript it resumed the conversation from."""

    kind: Literal["boot"] = "boot"
    resumed_run_id: str | None


class TurnStarted(_Event):
    """A prompt entered the agent through one of the two entry points.

    `entry` names which one: the Studio over HTTP, or an editor over ACP. `session_id` names the
    session it opened under when the writer knows it. It is `None` on lines written before a turn
    carried its own session id. `prompt_excerpt` is the prompt's own opening words, truncated, and
    the only trace of what the person said when a turn is cut off before its run registers. It is
    `None` on lines written before the excerpt was recorded.
    """

    kind: Literal["turn_started"] = "turn_started"
    entry: Literal["spa", "acp"]
    chars: int
    session_id: str | None = None
    prompt_excerpt: str | None = None


class TurnSettled(_Event):
    """The turn produced its reply. `run_id` names the transcript it wrote.

    `started_seq` names the `turn_started` log entry it closes when the writer knows it. `None`
    closes whichever turn the positional walk currently considers open.
    """

    kind: Literal["turn_settled"] = "turn_settled"
    run_id: str | None
    started_seq: int | None = None
    pending: tuple[str, ...] = ()
    """Tool names the turn stopped for, left to the browser to perform or the person to approve, empty when the
    turn answered outright. The continuation is its own turn."""


class TurnFailed(_Event):
    """The turn raised. `error` is the error's first sentence, never a traceback.

    `started_seq` names the `turn_started` log entry it closes when the writer knows it. `None`
    closes whichever turn the positional walk currently considers open. `prompt_excerpt` is the
    turn's own opening words, truncated, which the session projection takes its title from when
    the session's first turn failed before any run saved a step. It is `None` when the writer had
    nothing to give.
    """

    kind: Literal["turn_failed"] = "turn_failed"
    error: str
    started_seq: int | None = None
    prompt_excerpt: str | None = None


class TurnAbandoned(_Event):
    """Appended at boot for a started turn with no terminal event: the crash, made visible."""

    kind: Literal["turn_abandoned"] = "turn_abandoned"
    started_seq: int


class TurnSteered(_Event):
    """The person sent a message into a turn already in flight, and that turn folds it in.

    `chars` sizes it the way `TurnStarted` does. The text itself reaches the model, never
    the session log.
    """

    kind: Literal["turn_steered"] = "turn_steered"
    chars: int
    session_id: str | None = None


class ConversationForgotten(_Event):
    """The boundary: the person pressed Forget. Forgetting is a watermark, never an erasure.

    `session_id` names the session being forgotten when the writer knows it. `None` closes
    whichever session the positional walk currently considers live.
    """

    kind: Literal["conversation_forgotten"] = "conversation_forgotten"
    session_id: str | None = None


class DraftSettled(_Event):
    """A proposal passed the checker and went on screen."""

    kind: Literal["draft_settled"] = "draft_settled"
    name: str
    version: int


class PluginSet(_Event):
    """The owner turned one plugin on or off. The change applies from the very next turn."""

    kind: Literal["plugin_set"] = "plugin_set"
    plugin: str
    enabled: bool


class ProfileSelected(_Event):
    """A profile took over the setup, or `None` handed it back to the hand-set plugins."""

    kind: Literal["profile_selected"] = "profile_selected"
    name: str | None


class TierSelected(_Event):
    """The owner set a tier's model and thinking, or with both `None` put its default back."""

    kind: Literal["tier_selected"] = "tier_selected"
    tier: Literal["orchestrator", "worker", "helper"]
    model: str | None
    thinking: Literal["low", "medium", "high", "xhigh"] | None


class RunLaunched(_Event):
    """The session launched a workflow. What became of the run is the run's own log to answer."""

    kind: Literal["run_launched"] = "run_launched"
    workflow_id: str
    run_id: str = ""
    """The run this launch minted. `""` when the writer named none."""


FrictionTargetType = Literal["tool", "skill", "capability", "workflow", "agent", "instructions", "self"]
"""What a friction flag is about: a tool, a skill, a capability, a workflow, an agent, the instructions, or the agent itself."""


class SessionStarted(_Event):
    """A fresh session opened. Every following turn belongs to it until the next boundary.

    `session_kind` names what the session is for: `"chat"` for the authoring conversation, or
    `"runner"` for one opened to drive a workflow unattended. It is `"chat"` on lines written
    before the field existed.
    """

    kind: Literal["session_started"] = "session_started"
    session_id: str
    session_kind: str = "chat"


class SessionRenamed(_Event):
    """The owner set a title, replacing the derived first-prompt excerpt."""

    kind: Literal["session_renamed"] = "session_renamed"
    session_id: str
    title: str


class SessionArchived(_Event):
    """The owner hid a session from the default listing, or brought it back."""

    kind: Literal["session_archived"] = "session_archived"
    session_id: str
    archived: bool


class SessionDeleted(_Event):
    """The owner removed a session. Its step data is gone, and the projection keeps it only as deleted."""

    kind: Literal["session_deleted"] = "session_deleted"
    session_id: str


FlagSource = Literal["live", "retro"]
"""Where a flag came from. Only `live` is written. `retro` is kept for lines an earlier pass wrote,
which a rebuild still replays."""


class FrictionFlagged(_Event):
    """The agent reported something that ground: what kind of thing, which one, and why."""

    kind: Literal["friction_flagged"] = "friction_flagged"
    target_kind: FrictionTargetType
    target: str
    note: str
    tool_call_id: str | None
    source: FlagSource
    subject_session_id: str | None


class ExternalAgentStarted(_Event):
    """The agent handed a brief to another harness over ACP, at a working directory."""

    kind: Literal["external_agent_started"] = "external_agent_started"
    agent: str
    cwd: str


class ExternalAgentUpdated(_Event):
    """One line of what the external agent was doing, at most one per second of its run.

    `text` is an excerpt, not the transcript: the external agent's own log is where the whole
    thing lives, and this line exists so the timeline shows the run was alive.
    """

    kind: Literal["external_agent_update"] = "external_agent_update"
    agent: str
    text: str


class ExternalAgentFinished(_Event):
    """The external agent's turn ended. `stop_reason` is the protocol's own word for why."""

    kind: Literal["external_agent_finished"] = "external_agent_finished"
    agent: str
    stop_reason: str
    touched_files: tuple[str, ...] = ()


SessionEvent = Annotated[
    Booted
    | TurnStarted
    | TurnSettled
    | TurnFailed
    | TurnAbandoned
    | TurnSteered
    | ConversationForgotten
    | DraftSettled
    | PluginSet
    | ProfileSelected
    | TierSelected
    | RunLaunched
    | SessionStarted
    | SessionRenamed
    | SessionArchived
    | SessionDeleted
    | FrictionFlagged
    | ExternalAgentStarted
    | ExternalAgentUpdated
    | ExternalAgentFinished,
    Field(discriminator="kind"),
]


class Recorded(BaseModel):
    """One session log entry: the event plus where and when it sits on the timeline."""

    model_config = ConfigDict(frozen=True)

    seq: int
    at: datetime
    event: SessionEvent


_RECORDED = TypeAdapter(Recorded)


def encode(record: Recorded) -> str:
    """One flat JSONL line: `seq`, `at`, then the event's own keys, readable with grep alone.

    Args:
        record: The log entry to write.

    Returns:
        The line, with no trailing newline.
    """
    payload = {"seq": record.seq, "at": record.at.isoformat(), **record.event.model_dump(mode="json")}
    return json.dumps(payload)


def decode(line: str) -> Recorded | None:
    """The line's log entry, or `None` for a torn tail or a kind this reader does not know.

    It never raises, so a damaged final line, or a line a newer process wrote, costs one event and
    never the boot.

    Args:
        line: One line of the session log.

    Returns:
        The log entry, or `None` when the line cannot be read.
    """
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    if not isinstance(payload, dict) or "seq" not in payload or "at" not in payload:
        return None
    seq, at = payload.pop("seq"), payload.pop("at")
    try:
        return _RECORDED.validate_python({"seq": seq, "at": at, "event": payload})
    except ValidationError:
        return None


def open_turns(events: Sequence[Recorded]) -> tuple[Recorded, ...]:
    """Every `turn_started` with no terminal event, which is what the boot sweep closes as abandoned.

    A settle or failure that names its own `started_seq` closes exactly that turn, wherever it
    sits among other sessions' open turns. One written before turns carried it closes the newest
    open turn instead. An abandonment always closes exactly the turn it names.

    Args:
        events: The timeline, in order.

    Returns:
        The log entries of the turns still open, in timeline order.
    """
    open_records: list[Recorded] = []
    for record in events:
        match record.event:
            case TurnStarted():
                open_records.append(record)
            case TurnSettled(started_seq=started_seq) | TurnFailed(started_seq=started_seq):
                if started_seq is not None:
                    open_records = [record for record in open_records if record.seq != started_seq]
                elif open_records:
                    open_records.pop()
            case TurnAbandoned(started_seq=started_seq):
                open_records = [record for record in open_records if record.seq != started_seq]
            case _:
                pass
    return tuple(open_records)


def last_forgotten_at(events: Sequence[Recorded]) -> datetime | None:
    """When the person last pressed Forget, or `None` if they never have.

    Args:
        events: The timeline.

    Returns:
        The newest `conversation_forgotten` time, or `None`.
    """
    stamps = [record.at for record in events if isinstance(record.event, ConversationForgotten)]
    return max(stamps) if stamps else None


def logged_run_ids(events: Sequence[Recorded]) -> tuple[str, ...]:
    """Every run this timeline credits to one of its sessions, oldest first.

    Args:
        events: The timeline, in order.

    Returns:
        The run ids, span by span as `session_spans` returns them.
    """
    return tuple(run_id for span in session_spans(events) for run_id in span.run_ids)



_LEGACY_SESSION_NS = uuid.UUID("6ba7b812-9dad-11d1-80b4-00c04fd430c8")


class SessionSpan(BaseModel):
    """One stretch of the timeline belonging to one session id.

    `session_kind` is set once by the `SessionStarted` that opened the span, and is `"chat"` for
    a span no `SessionStarted` opened. It is right only on a session's first span. A span reopened
    after a `ConversationForgotten` on the same session id is `"chat"` whatever the session's kind.
    """

    model_config = ConfigDict(frozen=True)

    session_id: str
    started_at: datetime
    ended_at: datetime | None
    turns: int
    run_ids: tuple[str, ...]
    session_kind: str = "chat"


@dataclass
class _SpanBuilder:
    """One session's span still in progress: mutable while `session_spans` folds the timeline."""

    started_at: datetime | None = None
    first_seq: int | None = None
    turns: int = 0
    run_ids: list[str] = field(default_factory=list)
    session_kind: str = "chat"


class _SpanFold:
    """The mutable state threaded through one pass over the timeline, one session per builder."""

    def __init__(self) -> None:
        self.spans: list[SessionSpan] = []
        self.builders: dict[str | None, _SpanBuilder] = {}
        self.seq_to_session: dict[int, str] = {}
        self.current_id: str | None = None

    def _close(self, key: str | None, ended_at: datetime | None) -> None:
        builder = self.builders.pop(key, None)
        if builder is None or builder.started_at is None or builder.first_seq is None:
            return
        resolved_id = key if key is not None else str(uuid.uuid5(_LEGACY_SESSION_NS, str(builder.first_seq)))
        self.spans.append(
            SessionSpan(
                session_id=resolved_id,
                started_at=builder.started_at,
                ended_at=ended_at,
                turns=builder.turns,
                run_ids=tuple(builder.run_ids),
                session_kind=builder.session_kind,
            )
        )

    def _opened(self, key: str | None, record: Recorded) -> _SpanBuilder:
        builder = self.builders.setdefault(key, _SpanBuilder())
        if builder.started_at is None:
            builder.started_at, builder.first_seq = record.at, record.seq
        return builder

    def _credit(self, key: str | None, run_id: str) -> None:
        builder = self.builders.get(key)
        if builder is not None and builder.started_at is not None:
            builder.run_ids.append(run_id)

    def apply(self, record: Recorded) -> None:
        """Fold one record: a boundary re-points the positional walk, a turn credits its span."""
        match record.event:
            case SessionStarted(session_id=new_id, session_kind=new_kind):
                self._close(self.current_id, record.at)
                self.current_id = new_id
                self._opened(new_id, record).session_kind = new_kind
            case ConversationForgotten(session_id=stamped_id):
                target = stamped_id if stamped_id is not None else self.current_id
                self._close(target, record.at)
                if stamped_id is None or stamped_id == self.current_id:
                    self.current_id = None
            case TurnStarted(session_id=stamped_id):
                key = stamped_id if stamped_id is not None else self.current_id
                self._opened(key, record).turns += 1
                if stamped_id is not None:
                    self.seq_to_session[record.seq] = stamped_id
            case TurnSettled(run_id=run_id, started_seq=started_seq) if run_id is not None:
                key = self.seq_to_session.get(started_seq) if started_seq is not None else self.current_id
                self._credit(key, run_id)
            case _:
                pass

    def finish(self) -> tuple[SessionSpan, ...]:
        """Close the positionally-current span, then every stamped span still open, live."""
        self._close(self.current_id, None)
        for remaining in list(self.builders):
            self._close(remaining, None)
        return tuple(self.spans)


def session_spans(events: Sequence[Recorded]) -> tuple[SessionSpan, ...]:
    """The timeline cut at every boundary, each span carrying its session id.

    A `turn_started` that names its own `session_id` is grouped under that id, so turns from
    different sessions may interleave in the session log. One written before turns carried it
    follows the positional walk instead: whichever session `session_started` most recently named.
    A `turn_settled` that names its own `started_seq` credits its run to the session that turn
    opened under, by the same rule. A `conversation_forgotten` that names its own `session_id`
    closes only that session's span, and one that does not closes whichever span the positional
    walk currently considers live. A span no event names a session for gets an id derived from its
    first seq, the same on every rebuild.

    Args:
        events: The timeline, in order.

    Returns:
        The spans in the order they closed, the ones still live last.
    """
    fold = _SpanFold()
    for record in events:
        fold.apply(record)
    return fold.finish()


class LiveSession(BaseModel):
    """Which session holds the live slot, and the newest settled run to resume it from."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    last_run_id: str | None


def live_session(events: Sequence[Recorded]) -> LiveSession | None:
    """The session the live slot points at, or None when the log ends on a Forget or is empty.

    A `turn_settled` that names its own `started_seq` credits the run to the session that turn
    opened under, even if another session's turns interleave around it. One written before turns
    carried it credits whichever session is current when it lands.

    Args:
        events: The timeline, in order.

    Returns:
        The live session with its newest settled run, or `None`.
    """
    current_id: str | None = None
    seq_to_session: dict[int, str] = {}
    newest_run: dict[str, str] = {}
    for record in events:
        match record.event:
            case SessionStarted(session_id=new_id):
                current_id = new_id
            case ConversationForgotten(session_id=stamped_id):
                if stamped_id is None or stamped_id == current_id:
                    current_id = None
            case TurnStarted(session_id=stamped_id) if stamped_id is not None:
                seq_to_session[record.seq] = stamped_id
            case TurnSettled(run_id=run_id, started_seq=started_seq) if run_id is not None:
                owner = seq_to_session.get(started_seq) if started_seq is not None else current_id
                if owner is not None:
                    newest_run[owner] = run_id
            case _:
                pass
    if current_id is None:
        return None
    return LiveSession(session_id=current_id, last_run_id=newest_run.get(current_id))
