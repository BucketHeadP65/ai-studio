"""The session projection: the fold of session logs and transcripts into rows, and the reads and writes over them.

The fold is pure given the same session log and transcripts, so a rebuild and a run of refreshes write the same rows.
"""

from __future__ import annotations

import json
import uuid

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from core.observability import get_logger
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolReturnPart, UserPromptPart
from pydantic_core import to_jsonable_python

from harness import session as session_state
from harness.adapters.contracts import priced_cost
from harness.domain.contracts import (
    ConversationForgotten,
    FrictionFlagged,
    SessionArchived,
    SessionDeleted,
    SessionRenamed,
    SessionStarted,
    TurnAbandoned,
    TurnFailed,
    TurnSettled,
    TurnStarted,
    live_session,
    session_spans,
    strip_page_context,
)
from harness.features.sessions.infrastructure import reads, schema, writes
from harness.workspace import open_workspace

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from core.cqrs.contracts import TopicSink
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai_harness.step_persistence import RunRecord, StepStore

    from harness.domain.contracts import Recorded, SessionEvents, SessionSpan
    from harness.features.sessions.internal_contracts import ProjectionPool
    from harness.features.sessions.read_models import FlagRow, MessageRow, ProjectRow, SessionRow, TurnRow
    from harness.session import Runtime, SessionRefresh

    class _Refresh(Protocol):
        """The `refresh_live` shape `sweep_projects` calls once per session."""

        async def __call__(
            self,
            pool: ProjectionPool,
            events: Sequence[Recorded],
            store: StepStore,
            *,
            project_root: Path,
            only_session_id: str | None = None,
        ) -> None: ...

_logger = get_logger(__name__)


@dataclass(frozen=True)
class SessionProjection:
    """The five projected tables, over the one database port this process opened for them.

    Every read answers a validated row type, so a caller reads a column by name. No caller writes SQL of its own.
    """

    pool: ProjectionPool
    """The port every read and write runs through."""

    async def sessions(self, *, project_id: uuid.UUID | None, archived: bool, session_type: str) -> list[SessionRow]:
        """Every session a listing covers.

        Args:
            project_id: The project to narrow to, or `None` for every project.
            archived: Whether archived sessions are included.
            session_type: The session type to narrow to, or `all` for every type.

        Returns:
            The sessions' rows, in no set order.
        """
        return await reads.sessions(self.pool, project_id=project_id, archived=archived, session_type=session_type)

    async def session(self, session_id: uuid.UUID) -> SessionRow | None:
        """One session's row.

        Args:
            session_id: The session's id.

        Returns:
            The row, or `None` when nothing projected that session.
        """
        return await reads.session(self.pool, session_id)

    async def session_exists(self, session_id: uuid.UUID) -> bool:
        """Whether a session has a projected row at all.

        Args:
            session_id: The session's id.

        Returns:
            True when a session row has that id.
        """
        return await reads.session_exists(self.pool, session_id)

    async def turns(self, session_id: uuid.UUID) -> list[TurnRow]:
        """One session's turns.

        Args:
            session_id: The session's id.

        Returns:
            The rows, in the order the turns happened.
        """
        return await reads.turns(self.pool, session_id)

    async def messages(self, session_id: uuid.UUID) -> list[MessageRow]:
        """Every message one session's turns produced.

        Args:
            session_id: The session's id.

        Returns:
            The rows, turn by turn, each run's messages in the order they were said.
        """
        return await reads.messages(self.pool, session_id)

    async def ranked_sessions(self, query: str) -> list[uuid.UUID]:
        """The sessions a full-text search over their messages matched.

        Args:
            query: The search text.

        Returns:
            At most 50 session ids, best match first.
        """
        return await reads.ranked_sessions(self.pool, query)

    async def flags(self, *, target_kind: str | None, target: str | None, session_id: uuid.UUID | None) -> list[FlagRow]:
        """Every friction flag a listing covers.

        Args:
            target_kind: The friction target type to narrow to, or `None` for every type.
            target: The name of the flagged thing to narrow to, or `None` for every name.
            session_id: The subject session to narrow to, or `None` for every session.

        Returns:
            The rows, newest first.
        """
        return await reads.flags(self.pool, target_kind=target_kind, target=target, session_id=session_id)

    async def project(self, project_id: uuid.UUID) -> ProjectRow | None:
        """One project's row.

        Args:
            project_id: The project's id.

        Returns:
            The row, or `None` when nothing projected that project.
        """
        return await reads.project(self.pool, project_id)

    async def refresh_live(
        self, events: Sequence[Recorded], store: StepStore, *, project_root: Path, only_session_id: str | None = None
    ) -> None:
        """Refresh one session's own rows, and only those. It never raises.

        Args:
            events: The entries of the session log the session is on.
            store: Where the session's transcripts are read from.
            project_root: The folder of the project the session belongs to.
            only_session_id: The session to refresh. `None` refreshes the session log's live session.
        """
        await refresh_live(self.pool, events, store, project_root=project_root, only_session_id=only_session_id)

    async def delete_session_rows(self, session_id: str) -> list[str]:
        """Remove one session's rows: its session row, its turns, its messages and its flags.

        Args:
            session_id: The session's id.

        Returns:
            The run ids the session's turns were written under, for the caller to remove elsewhere.
        """
        return await writes.delete_session_rows(self.pool, session_id)

    async def reconcile_projects(self, projects: Sequence[tuple[str, str, str]]) -> None:
        """Upsert one row per project on the registry of projects. Never removes a row.

        Args:
            projects: Each project's id, root path and display name.
        """
        await writes.reconcile_projects(self.pool, projects)

    async def ensure_schema(self) -> None:
        """Create every table, column and index the projection needs that is not already there."""
        await schema.ensure_schema(self.pool)

    async def sweep_projects(self, checkout: Path, *, session_log: SessionEvents, store: StepStore) -> None:
        """Bring every registered project's sessions up to date, additively.

        Args:
            checkout: The harness checkout, whose sessions are read from `session_log`.
            session_log: The checkout's own session log.
            store: Where every project's transcripts are read from.
        """
        await sweep_projects(self.pool, checkout, session_log=session_log, store=store)


class ProjectedIndex:
    """Which project owns a session, and where that project lives, read off the projected rows.

    A malformed id and an id nothing projected both answer `None`.
    """

    def __init__(self, pool: ProjectionPool) -> None:
        """Hold the port the projected rows are read through.

        Args:
            pool: The projection's database port.
        """
        self._pool = pool

    def id_of(self, root: Path) -> str:
        """The project id a folder belongs to.

        Args:
            root: The project's folder.

        Returns:
            The project id.
        """
        return project_id_of(root)

    async def root_of(self, project_id: str) -> Path | None:
        """Where a project lives.

        Args:
            project_id: The project's id.

        Returns:
            The project's root, or `None` for a malformed id or one no project row has.
        """
        return await reads.project_root(self._pool, project_id)

    async def root_of_session(self, session_id: str) -> Path | None:
        """The root of the project a session was opened against.

        Args:
            session_id: The session's id.

        Returns:
            The project's root, or `None` for a malformed id or one no session row has.
        """
        return await reads.session_root(self._pool, session_id)


_PROJECT_NS = uuid.UUID("d9f1a7b0-7b7b-4b1e-9b0a-2a6f9d6d9b1e")
"""The namespace of a project's id: `uuid5(_PROJECT_NS, str(resolved_root))`."""

_FLAG_NS = uuid.UUID("b6f3c9a4-3b0a-4f7e-9c1a-6e2d6a9c9f31")
"""The namespace of a flag's id: `uuid5(_FLAG_NS, str(log_seq))`."""


@dataclass
class _TurnUnderway:
    """One turn's projected shape, changed in place as later log entries settle, fail or abandon it."""

    session_id: str
    seq: int
    entry: str
    started_at: datetime
    run_id: str | None = None
    settled_at: datetime | None = None
    closed_at: datetime | None = None
    """When this turn reached any end: settled, failed or abandoned. `settled_at` stays `None` after a failure or
    an abandonment, and a session's `last_activity_at` falls back to this."""
    prompt_excerpt: str | None = None
    """The turn's opening words, from `TurnStarted` when it carries them, else from `TurnFailed`. A session's title
    falls back to it when no run wrote a transcript to read one from."""
    outcome: Literal["settled", "failed", "abandoned", "open"] = "open"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: Decimal = Decimal(0)
    unpriced: int = 0
    """Responses in this turn that no source could price. Above zero, `cost` is a floor rather than the cost."""


@dataclass
class _FlagRow:
    """One friction flag's row, resolved to its subject session."""

    id: str
    session_id: str
    log_seq: int
    target_kind: str
    target: str
    note: str
    tool_call_id: str | None
    source: str
    created_at: datetime


def _close_turn(open_stack: list[_TurnUnderway], started_seq: int) -> _TurnUnderway | None:
    """The open turn `started_seq` names, taken off the stack."""
    for index, row in enumerate(open_stack):
        if row.seq == started_seq:
            del open_stack[index]
            return row
    return None


def _stamp_outcome(open_stack: list[_TurnUnderway], record: Recorded) -> None:
    """Stamp the outcome, and a settle's run id, on the open turn this record closes."""
    match record.event:
        case TurnSettled(run_id=run_id, started_seq=started_seq):
            if (row := _close_turn(open_stack, started_seq)) is not None:
                row.run_id, row.settled_at, row.outcome = run_id, record.at, "settled"
                row.closed_at = record.at
        case TurnFailed(started_seq=started_seq, prompt_excerpt=prompt_excerpt):
            if (row := _close_turn(open_stack, started_seq)) is not None:
                row.outcome = "failed"
                row.closed_at = record.at
                row.prompt_excerpt = prompt_excerpt or row.prompt_excerpt
        case TurnAbandoned(started_seq=started_seq):
            if (row := _close_turn(open_stack, started_seq)) is not None:
                row.outcome = "abandoned"
                row.closed_at = record.at
        case _:
            pass


def _flag_row(record: Recorded, current: str | None) -> _FlagRow | None:
    """The flag row of one `FrictionFlagged` record, on its own subject session, else on `current`, else `None`."""
    event = record.event
    if not isinstance(event, FrictionFlagged):
        return None
    subject = event.subject_session_id if event.subject_session_id is not None else current
    if subject is None:
        return None
    return _FlagRow(
        id=str(uuid.uuid5(_FLAG_NS, str(record.seq))),
        session_id=subject,
        log_seq=record.seq,
        target_kind=event.target_kind,
        target=event.target,
        note=event.note,
        tool_call_id=event.tool_call_id,
        source=event.source,
        created_at=record.at,
    )


def _fold_turns_and_flags(events: Sequence[Recorded]) -> tuple[list[_TurnUnderway], list[_FlagRow]]:
    """Turn rows and flag rows in one forward pass over `events`, a flag with no subject filed under the current session."""
    turns: list[_TurnUnderway] = []
    flags: list[_FlagRow] = []
    open_stack: list[_TurnUnderway] = []
    current: str | None = None

    for record in events:
        match record.event:
            case SessionStarted(session_id=session_id):
                current = session_id
            case ConversationForgotten(session_id=session_id) if session_id is None or session_id == current:
                current = None
            case TurnStarted(session_id=session_id, entry=entry, prompt_excerpt=prompt_excerpt):
                row = _TurnUnderway(session_id=session_id, seq=record.seq, entry=entry, started_at=record.at, prompt_excerpt=prompt_excerpt)
                turns.append(row)
                open_stack.append(row)
            case TurnSettled() | TurnFailed() | TurnAbandoned():
                _stamp_outcome(open_stack, record)
            case FrictionFlagged():
                if (flag := _flag_row(record, current)) is not None:
                    flags.append(flag)
            case _:
                pass
    return turns, flags


def _group_sessions(
    spans: Sequence[SessionSpan], by_session: Mapping[str, Sequence[_TurnUnderway]]
) -> dict[str, tuple[datetime, datetime]]:
    """Each session's started-at and last-activity-at over all its spans, an open span ending at its own newest turn."""
    grouped: dict[str, list[SessionSpan]] = {}
    for span in spans:
        grouped.setdefault(span.session_id, []).append(span)
    result: dict[str, tuple[datetime, datetime]] = {}
    for session_id, group in grouped.items():
        started_at = min(span.started_at for span in group)
        own_last_turn = max((row.closed_at or row.started_at for row in by_session.get(session_id, ())), default=started_at)
        ends = [span.ended_at if span.ended_at is not None else own_last_turn for span in group]
        result[session_id] = (started_at, max(ends))
    return result


def _text_of(message: ModelMessage) -> str:
    """The message's user-prompt, text, and tool-return parts joined, capped at 20,000 chars."""
    texts: list[str] = []
    for part in message.parts:
        match part:
            case UserPromptPart(content=content) if isinstance(content, str):
                texts.append(content)
            case TextPart(content=content):
                texts.append(content)
            case ToolReturnPart(content=content) if isinstance(content, str):
                texts.append(content)
            case _:
                pass
    return " ".join(texts)[:20_000]


def _first_user_text(messages: Sequence[ModelMessage]) -> str | None:
    """The first string user-prompt content in `messages`, its page-context fence stripped, or `None` when there isn't one."""
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                return strip_page_context(part.content)
    return None


def _latest_rename(events: Sequence[Recorded], session_id: str) -> str | None:
    """The most recent `SessionRenamed` title for `session_id`, or `None` if it was never renamed."""
    title: str | None = None
    for record in events:
        if isinstance(record.event, SessionRenamed) and record.event.session_id == session_id:
            title = record.event.title
    return title


def title_for(events: Sequence[Recorded], span: SessionSpan, *, excerpt: str | None = None) -> str:
    """A session's display title: its latest rename, else `excerpt`, else `session` and the id's first eight characters.

    Args:
        events: The entries of the session log the session is on.
        span: The session.
        excerpt: The session's first prompt, when the caller read it from the transcripts.

    Returns:
        The title.
    """
    if (title := _latest_rename(events, span.session_id)) is not None:
        return title
    if excerpt:
        return excerpt
    return f"session {span.session_id[:8]}"


def _session_types(events: Sequence[Recorded]) -> dict[str, str]:
    """Each session id's session type, set once by the `SessionStarted` that opened it."""
    session_types: dict[str, str] = {}
    for record in events:
        if isinstance(record.event, SessionStarted):
            session_types.setdefault(record.event.session_id, record.event.session_kind)
    return session_types


def _forgotten_seq_by_session(events: Sequence[Recorded]) -> dict[str, int]:
    """Each session id's latest Forget stamped with that session's id, as the log seq it landed on."""
    watermarks: dict[str, int] = {}
    for record in events:
        if isinstance(record.event, ConversationForgotten) and record.event.session_id is not None:
            session_id = record.event.session_id
            if record.seq > watermarks.get(session_id, -1):
                watermarks[session_id] = record.seq
    return watermarks


def _owner_acts(events: Sequence[Recorded]) -> tuple[dict[str, bool], set[str]]:
    """Final archived flag per session id (latest wins) and the set of tombstoned ids."""
    archived: dict[str, bool] = {}
    deleted: set[str] = set()
    for record in events:
        match record.event:
            case SessionArchived(session_id=session_id, archived=flag):
                archived[session_id] = flag
            case SessionDeleted(session_id=session_id):
                deleted.add(session_id)
                archived.pop(session_id, None)
            case _:
                pass
    return archived, deleted


def _turn_payload(session_id: str, row: _TurnUnderway) -> tuple[object, ...]:
    return (
        session_id,
        row.seq,
        row.run_id,
        row.entry,
        row.started_at,
        row.settled_at,
        row.outcome,
        row.input_tokens,
        row.output_tokens,
        row.cache_read_tokens,
        row.cache_write_tokens,
    )


def _flag_payload(flag: _FlagRow) -> tuple[object, ...]:
    return (
        flag.id,
        flag.session_id,
        flag.log_seq,
        flag.target_kind,
        flag.target,
        flag.note,
        flag.tool_call_id,
        flag.source,
        flag.created_at,
    )


def _claim_runs(rows_for_session: Sequence[_TurnUnderway], runs: Sequence[RunRecord]) -> None:
    """Give each turn that closed with no run id the first unclaimed transcript run that started inside its window."""
    claimed = {row.run_id for row in rows_for_session if row.run_id is not None}
    unclaimed = [run for run in runs if run.run_id not in claimed]
    for row in rows_for_session:
        if row.run_id is not None:
            continue
        for run in unclaimed:
            if run.started_at >= row.started_at and (row.closed_at is None or run.started_at <= row.closed_at):
                row.run_id = run.run_id
                unclaimed.remove(run)
                break


def _first_new_index(messages: Sequence[ModelMessage], previous_last: ModelMessage | None) -> int:
    """The index right after the last message in `messages` matching `previous_last` by kind and parts, searched from the end, or 0 when none matches."""
    if previous_last is None:
        return 0
    for index in range(len(messages) - 1, -1, -1):
        candidate = messages[index]
        if candidate.kind == previous_last.kind and candidate.parts == previous_last.parts:
            return index + 1
    return 0


async def _fold_session_messages(
    rows_for_session: Sequence[_TurnUnderway], store: StepStore
) -> tuple[list[tuple[object, ...]], str | None]:
    """This session's message rows, each run's new tail, and its first prompt, adding each response's usage to its turn."""
    message_payloads: list[tuple[object, ...]] = []
    previous_last: ModelMessage | None = None
    excerpt: str | None = None
    excerpt_checked = False
    for row in rows_for_session:
        if row.run_id is None:
            continue
        try:
            snapshot = await store.latest_snapshot(run_id=row.run_id)
        except Exception as error:  # noqa: BLE001 (one run's unreadable snapshot must not cost the session its row)
            _logger.warning("could not read the snapshot for run {run_id}: {reason}", run_id=row.run_id, reason=str(error))
            continue
        if snapshot is None:
            continue
        # A compaction rewrites earlier history in place, so a turn's new messages start after the previous turn's
        # own last message, found by kind and parts, never at the previous snapshot's length.
        new_messages = snapshot.messages[_first_new_index(snapshot.messages, previous_last) :]
        previous_last = snapshot.messages[-1] if snapshot.messages else previous_last
        if not excerpt_checked:
            excerpt = _first_user_text(snapshot.messages)
            excerpt_checked = True
        for idx, message in enumerate(new_messages):
            if isinstance(message, ModelResponse):
                row.input_tokens += message.usage.input_tokens
                row.output_tokens += message.usage.output_tokens
                row.cache_read_tokens += message.usage.cache_read_tokens
                row.cache_write_tokens += message.usage.cache_write_tokens
                price = priced_cost(message)
                if price is None:
                    row.unpriced += 1
                else:
                    row.cost += price
            message_payloads.append((row.run_id, idx, message.kind, json.dumps(to_jsonable_python(message)), _text_of(message)))
    return message_payloads, excerpt


async def _project_rows(
    events: Sequence[Recorded], store: StepStore, *, project_id: str, only_session_id: str | None
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]], list[tuple[object, ...]], list[tuple[object, ...]]]:
    """Session, turn, message and flag rows ready to bind, only `only_session_id`'s when it is given."""
    spans = session_spans(events)
    turn_rows, flag_rows = _fold_turns_and_flags(events)
    archived, deleted = _owner_acts(events)
    session_types = _session_types(events)
    forgotten_at = _forgotten_seq_by_session(events)

    by_session: dict[str, list[_TurnUnderway]] = {}
    for row in turn_rows:
        by_session.setdefault(row.session_id, []).append(row)

    groups = _group_sessions(spans, by_session)

    session_payloads: list[tuple[object, ...]] = []
    turn_payloads: list[tuple[object, ...]] = []
    message_payloads: list[tuple[object, ...]] = []

    live_ids = (session_id for session_id in groups if session_id not in deleted)
    for session_id in live_ids:
        if only_session_id is not None and session_id != only_session_id:
            continue
        started_at, last_activity_at = groups[session_id]
        rows_for_session = sorted(by_session.get(session_id, ()), key=lambda row: row.seq)
        # A forgotten session keeps only the turns after its latest Forget. The session log and transcript keep the rest.
        watermark = forgotten_at.get(session_id)
        if watermark is not None:
            rows_for_session = [row for row in rows_for_session if row.seq > watermark]
        if any(row.run_id is None for row in rows_for_session):
            try:
                runs = await store.list_runs(conversation_id=session_id)
            except Exception as error:  # noqa: BLE001 (an unreadable run list costs only the run claiming, never the session's row)
                _logger.warning("could not list runs for session {session_id}: {reason}", session_id=session_id, reason=str(error))
                runs = []
            _claim_runs(rows_for_session, runs)
        session_messages, excerpt = await _fold_session_messages(rows_for_session, store)
        if excerpt is None:
            # No run wrote a transcript to read a title from, so the earliest turn that stored its own excerpt names the session.
            excerpt = next((row.prompt_excerpt for row in rows_for_session if row.prompt_excerpt is not None), None)
        message_payloads.extend(session_messages)
        turn_payloads.extend(_turn_payload(session_id, row) for row in rows_for_session)

        span_for_id = next(span for span in spans if span.session_id == session_id)
        title = title_for(events, span_for_id, excerpt=excerpt)
        session_payloads.append(
            (
                session_id,
                project_id,
                title,
                started_at,
                last_activity_at,
                archived.get(session_id, False),
                len(rows_for_session),
                sum(row.input_tokens for row in rows_for_session),
                sum(row.output_tokens for row in rows_for_session),
                sum(row.cache_read_tokens for row in rows_for_session),
                sum(row.cache_write_tokens for row in rows_for_session),
                sum((row.cost for row in rows_for_session), Decimal(0)),
                sum(row.unpriced for row in rows_for_session),
                session_types.get(session_id, "chat"),
            )
        )

    flag_payloads = [
        _flag_payload(flag)
        for flag in flag_rows
        if flag.session_id not in deleted and (only_session_id is None or flag.session_id == only_session_id)
    ]

    return session_payloads, turn_payloads, message_payloads, flag_payloads


def _project_identity(project_root: Path) -> tuple[str, str, str]:
    """One project's id, resolved root path and display name."""
    resolved_root = project_root.resolve()
    project_id = str(uuid.uuid5(_PROJECT_NS, str(resolved_root)))
    return project_id, str(resolved_root), resolved_root.name


def project_id_of(project_root: Path) -> str:
    """The project id a folder belongs to, the same id the projection writes for it.

    Args:
        project_root: The project's folder.

    Returns:
        The id, a `uuid5` over the resolved folder path.
    """
    return _project_identity(project_root)[0]


async def rebuild(pool: ProjectionPool, events: Sequence[Recorded], store: StepStore, *, project_root: Path) -> None:
    """Recompute every projected table from one session log and the transcripts, from a clean slate.

    All five tables are emptied and every row is written again, in one transaction. A session tombstoned by
    `SessionDeleted` gets no row. Running it twice in a row leaves the same rows.

    Args:
        pool: The projection's database port.
        events: The entries of the project's session log.
        store: Where the transcripts are read from.
        project_root: The project's folder.
    """
    project_id, root_path, name = _project_identity(project_root)
    sessions, turns, messages, flags = await _project_rows(events, store, project_id=project_id, only_session_id=None)
    await writes.replace_all_rows(pool, (project_id, root_path, name), (sessions, turns, messages, flags))


async def refresh_live(
    pool: ProjectionPool, events: Sequence[Recorded], store: StepStore, *, project_root: Path, only_session_id: str | None = None
) -> None:
    """Refresh one session's own rows, and only those, cheap enough to run after every settle.

    The session's turns and their messages are replaced outright. Its session row and its flags are upserted. A failure is logged as a warning and never raised.
    Announcing the refreshed rows is the caller's.

    Args:
        pool: The projection's database port.
        events: The entries of the session log the session is on.
        store: Where the session's transcripts are read from.
        project_root: The folder of the project the session belongs to.
        only_session_id: The session to refresh. `None` refreshes the session log's live session, the newest one it
            opened, which is wrong for a settle in one session while another has since started.
    """
    try:
        session_id = only_session_id
        if session_id is None:
            live = live_session(events)
            if live is None:
                return
            session_id = live.session_id
        project_id, root_path, name = _project_identity(project_root)
        sessions, turns, messages, flags = await _project_rows(events, store, project_id=project_id, only_session_id=session_id)
        if not sessions:
            return
        await writes.write_session_rows(pool, session_id, (project_id, root_path, name), (sessions, turns, messages, flags))
    except Exception as error:  # noqa: BLE001 (a settle must never fail on its refresh)
        _logger.warning("session projection refresh_live failed: {reason}", reason=str(error))


def live_refresh(pool: ProjectionPool, topics: TopicSink) -> SessionRefresh:
    """How a settled turn refreshes its own session's rows behind the answer, then announces the `sessions` topic.

    The refresh names the session it runs for, never the timeline's newest one.

    Args:
        pool: The projection's database port.
        topics: Where the change to the session list is announced.

    Returns:
        The refresh a settled turn schedules.
    """

    async def refresh(runtime: Runtime) -> None:
        """Bring one session's projected rows up to date, then say the session list moved."""
        await refresh_live(
            pool,
            session_state.timeline(runtime),
            runtime.store,
            project_root=session_state.project_root(runtime),
            only_session_id=runtime.session_id,
        )
        topics.publish("sessions")

    return refresh


async def sweep_projects(
    pool: ProjectionPool, checkout: Path, *, session_log: SessionEvents, store: StepStore, refresh: _Refresh = refresh_live
) -> None:
    """Bring every registered project's own sessions up to date in the projection, additively.

    Each session span of each project's session log is refreshed by id, and nothing is truncated, so a session
    already projected keeps its row even when this pass cannot read it. The checkout reads `session_log`, and every
    other project reads its own workspace's session log.

    Args:
        pool: The projection's database port.
        checkout: The harness checkout.
        session_log: The checkout's own session log.
        store: Where every project's transcripts are read from.
        refresh: The write run once per session.
    """
    from harness.features.sessions import project_registry  # noqa: PLC0415 (project_registry imports this module at its top)

    def _home(root: Path) -> Sequence[Recorded]:
        """The entries of the session log `root` reads from, resolved outside the `async def`."""
        if root.resolve() == checkout.resolve():
            return session_log.events()
        return open_workspace(root).session_log.events()

    for project in project_registry.registered_projects():
        root = Path(project.root_path)
        events = _home(root)
        for span in session_spans(events):
            await refresh(pool, events, store, project_root=root, only_session_id=span.session_id)
