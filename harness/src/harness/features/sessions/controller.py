"""The session and project routes: listing, detail, transcript, flags, and the project lifecycle.

Every read answers from the session projection, never from a session log. A session's `answering` and `context` are
never read from a stored column. Both are computed fresh from the pool's live state on every request.
"""

from __future__ import annotations

import json

from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from core.cqrs.contracts import TopicSink
from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from wireup.integration.fastapi import WireupRoute

from harness import session as session_state
from harness.domain.contracts import UNKNOWN_SESSION, SessionArchived, SessionDeleted, SessionRenamed
from harness.features.sessions import project_registry
from harness.features.sessions.api_models import (
    ContextVitals,
    FlagView,
    ProjectRegisterRequest,
    ProjectRename,
    ProjectView,
    SessionPatch,
    SessionSummary,
    TranscriptMessage,
    TurnView,
    UsageTotals,
)
from harness.features.sessions.internal_contracts import ConversationStore, Sessions
from harness.features.sessions.read_models import MessageRow, SessionRow, TurnRow
from harness.features.sessions.service import SessionProjection, project_id_of
from harness.settings import CHECKOUT

if TYPE_CHECKING:
    from pydantic_ai_harness.compaction import ContextUsage

_UNKNOWN_PROJECT = "no project with that id here"


def _is_missing(root_path: str) -> bool:
    """Whether `root_path` no longer names a real directory, checked fresh at read time."""
    return not Path(root_path).is_dir()


def _existing_directory(root_path: str) -> Path | None:
    """`root_path`, expanded, if it names a real directory on this machine, else `None`."""
    root = Path(root_path).expanduser()
    return root if root.is_dir() else None


def _uuid_or_none(value: str) -> UUID | None:
    """`value` parsed as a UUID, or `None` when it is not shaped like one."""
    try:
        return UUID(value)
    except ValueError:
        return None


def _no_such_session() -> JSONResponse:
    """The one refusal every session-scoped route gives for an id nothing knows."""
    return JSONResponse({"detail": UNKNOWN_SESSION}, status_code=404)


def _context_vitals(context: ContextUsage | None) -> ContextVitals | None:
    """`context` as the wire shape, or `None` when this process does not hold the session."""
    if context is None:
        return None
    return ContextVitals(
        used_tokens=context.used_tokens, window_tokens=context.window_tokens, fraction=context.fraction, resolved=context.resolved
    )


def _summary(row: SessionRow, *, answering: bool, context: ContextUsage | None) -> SessionSummary:
    """One `sessions` row, the pool's own answer to "is a turn in flight here right now", and its live context reading."""
    return SessionSummary(
        id=str(row.id),
        project_id=str(row.project_id),
        title=row.title,
        started_at=row.started_at,
        last_activity_at=row.last_activity_at,
        archived=row.archived,
        answering=answering,
        turn_count=row.turn_count,
        usage=UsageTotals(
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cache_read_tokens=row.cache_read_tokens,
            cache_write_tokens=row.cache_write_tokens,
        ),
        kind=row.session_kind,
        cost_usd=row.cost,
        unpriced=row.unpriced,
        context=_context_vitals(context),
    )


def _turn_view(row: TurnRow) -> TurnView:
    """One `turns` row as the shape a caller reads."""
    return TurnView(
        seq=row.seq,
        run_id=row.run_id,
        entry=row.entry,
        started_at=row.started_at,
        settled_at=row.settled_at,
        outcome=row.outcome,
        usage=UsageTotals(
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cache_read_tokens=row.cache_read_tokens,
            cache_write_tokens=row.cache_write_tokens,
        ),
    )


def live_prompt_messages(sessions: Sessions, session_id: str, *, base_idx: int) -> list[TranscriptMessage]:
    """The prompts of the turn running in one session right now, as trailing request rows.

    Only a turn still holding its session contributes. An ended turn, however it ended, and a session this process
    does not hold contribute nothing.

    Args:
        sessions: The sessions this process holds open.
        session_id: The session's id.
        base_idx: The `idx` the first prompt's row takes.

    Returns:
        One request row per prompt, in the order they were said.
    """
    live = sessions.steering(session_id)
    if live is None:
        return []
    return [
        TranscriptMessage(
            run_id="live",
            idx=base_idx + offset,
            kind="request",
            payload={"kind": "request", "parts": [{"part_kind": "user-prompt", "content": text}]},
            text=text,
        )
        for offset, text in enumerate(live.live_prompts)
    ]


def _transcript_message(row: MessageRow) -> TranscriptMessage:
    """One projected row as the SPA reads it."""
    return TranscriptMessage(run_id=row.run_id, idx=row.idx, kind=row.kind, payload=json.loads(row.payload), text=row.text_content)


def _known_project_ids() -> tuple[str, ...]:
    """Every project id a listing offers: the registry of projects' own, plus the checkout's, always."""
    ids = {project.project_id for project in project_registry.registered_projects()}
    ids.add(project_id_of(CHECKOUT))
    return tuple(ids)


class SessionsController:
    """The session projection over HTTP, and the projects it groups them by."""

    router = APIRouter(prefix="/api", tags=["sessions"], route_class=WireupRoute)

    def __init__(self, projection: SessionProjection, sessions: Sessions, topics: TopicSink) -> None:
        """Hold the projected tables every read answers from, the sessions held open, and this deployment's channel.

        Args:
            projection: The session projection every read answers from.
            sessions: The sessions this process holds open.
            topics: Where a change to the session list is announced.
        """
        self._projection = projection
        self._sessions = sessions
        self._topics = topics

    @router.get("/sessions")
    async def list_sessions(
        self,
        *,
        project_id: str | None = None,
        archived: bool = False,
        kind: Literal["chat", "runner", "master", "all"] = "chat",
        q: str | None = None,
    ) -> JSONResponse:
        """Every session, newest activity first, or every one a search matched, ranked by that match.

        Archived sessions are hidden unless `archived=true` asks for the whole set. `kind` narrows to
        the authoring conversations (the default), the runners, the follow-me companions, or `all` of
        them. `q` restricts the result to sessions a full-text search over their messages actually
        matched.
        """
        project_uuid = _uuid_or_none(project_id) if project_id is not None else None
        if project_id is not None and project_uuid is None:
            return JSONResponse({"sessions": []})
        rows = await self._projection.sessions(project_id=project_uuid, archived=archived, session_type=kind)
        by_id = {row.id: row for row in rows}
        if q:
            ranked = await self._projection.ranked_sessions(q)
            ordered = [by_id[session_id] for session_id in ranked if session_id in by_id]
        else:
            ordered = sorted(rows, key=lambda row: row.last_activity_at, reverse=True)
        summaries = [
            _summary(row, answering=self._sessions.is_answering(str(row.id)), context=self._sessions.context_of(str(row.id)))
            for row in ordered
        ]
        return JSONResponse({"sessions": [summary.model_dump(mode="json") for summary in summaries]})

    @router.get("/sessions/{session_id}")
    async def session_detail(self, session_id: str) -> JSONResponse:
        """One session's summary, and its turns in the order they happened."""
        session_uuid = _uuid_or_none(session_id)
        if session_uuid is None:
            return _no_such_session()
        row = await self._projection.session(session_uuid)
        if row is None:
            return _no_such_session()
        turn_rows = await self._projection.turns(session_uuid)
        summary = _summary(row, answering=self._sessions.is_answering(session_id), context=self._sessions.context_of(session_id))
        turns = [_turn_view(turn_row) for turn_row in turn_rows]
        return JSONResponse({"session": summary.model_dump(mode="json"), "turns": [turn.model_dump(mode="json") for turn in turns]})

    @router.get("/sessions/{session_id}/transcript")
    async def session_transcript(self, session_id: str) -> JSONResponse:
        """Every message this session's turns produced, run by run, in the order they were said.

        A turn still in flight has no projected rows yet, so what the person said into it (the
        message that opened it, and every steer) rides at the end as live request rows. A reload
        mid-turn shows them their own words instead of a conversation with its tail missing.
        """
        session_uuid = _uuid_or_none(session_id)
        if session_uuid is None:
            return _no_such_session()
        if not await self._projection.session_exists(session_uuid):
            return _no_such_session()
        rows = await self._projection.messages(session_uuid)
        messages = [_transcript_message(row) for row in rows]
        messages.extend(live_prompt_messages(self._sessions, session_id, base_idx=len(messages)))
        return JSONResponse({"messages": [message.model_dump(mode="json") for message in messages]})

    @router.patch("/sessions/{session_id}")
    async def patch_session(self, session_id: str, body: SessionPatch) -> JSONResponse:
        """Rename and/or archive a session on its own session log, then refresh what it projects."""
        try:
            runtime = await self._sessions.get(session_id)
        except KeyError:
            return _no_such_session()
        if body.title is not None:
            runtime.session_log.record(SessionRenamed(session_id=session_id, title=body.title))
        if body.archived is not None:
            runtime.session_log.record(SessionArchived(session_id=session_id, archived=body.archived))
        await self._projection.refresh_live(
            session_state.timeline(runtime), runtime.store, project_root=session_state.project_root(runtime), only_session_id=session_id
        )
        self._topics.publish("sessions")
        row = await self._projection.session(UUID(session_id))
        if row is None:  # pragma: no cover (refresh_live just wrote this row)
            return _no_such_session()
        summary = _summary(row, answering=self._sessions.is_answering(session_id), context=self._sessions.context_of(session_id))
        return JSONResponse({"session": summary.model_dump(mode="json")})

    @router.delete("/sessions/{session_id}", status_code=204)
    async def delete_session(self, session_id: str) -> Response:
        """Remove a session everywhere: its session log record, its projected rows, and its conversation in the record.

        Refused while the session is answering. The pool lets the runtime go the moment the timeline
        records the deletion, so nothing keeps chatting into a session that is gone.

        A session whose project folder is not on this machine any more has no timeline to write the
        deletion on and no runtime to reach a record through, so only its projected rows go. It comes
        back only if that folder does and the projection is rebuilt from it. A session opened over a
        store that cannot delete a conversation still answers 204 with its rows gone.
        """
        try:
            runtime = await self._sessions.get(session_id)
        except KeyError:
            return _no_such_session()
        except session_state.MissingProjectRootError:
            await self._projection.delete_session_rows(session_id)
            return Response(status_code=204)
        if self._sessions.is_answering(session_id):
            return JSONResponse({"detail": "a turn is in flight in this session"}, status_code=409)
        runtime.session_log.record(SessionDeleted(session_id=session_id))
        self._sessions.drop(session_id)
        await self._projection.delete_session_rows(session_id)
        if isinstance(runtime.store, ConversationStore):
            await runtime.store.delete_conversation(session_id)
        return Response(status_code=204)

    @router.get("/flags")
    async def list_flags(self, target_kind: str | None = None, target: str | None = None, session_id: str | None = None) -> JSONResponse:
        """Every filed grievance, newest first, filtered by whatever a caller already knows about it."""
        session_uuid = _uuid_or_none(session_id) if session_id is not None else None
        if session_id is not None and session_uuid is None:
            return JSONResponse({"flags": []})
        rows = await self._projection.flags(target_kind=target_kind, target=target, session_id=session_uuid)
        flags = [
            FlagView(
                id=str(row.id),
                session_id=str(row.session_id),
                target_kind=row.target_kind,
                target=row.target,
                note=row.note,
                tool_call_id=row.tool_call_id,
                source=row.source,
                created_at=row.created_at,
            )
            for row in rows
        ]
        return JSONResponse({"flags": [flag.model_dump(mode="json") for flag in flags]})

    @router.get("/projects")
    async def list_projects(self) -> JSONResponse:
        """Every project the registry of projects knows, each with its live `missing` and `running_sessions` facts."""
        await self._reconcile()
        views = [view for project_id in _known_project_ids() if (view := await self._project_view(project_id)) is not None]
        return JSONResponse({"projects": [view.model_dump(mode="json") for view in views]})

    @router.post("/projects")
    async def register_project(self, body: ProjectRegisterRequest) -> JSONResponse:
        """Open a folder as a project: its workspace, a default config, and a place on the registry of projects."""
        root = _existing_directory(body.root_path)
        if root is None:
            return JSONResponse({"detail": f"{body.root_path!r} is not a directory"}, status_code=400)
        project_id = project_id_of(root)
        project_registry.register(root, project_id=project_id)
        await self._reconcile()
        view = await self._project_view(project_id)
        if view is None:  # pragma: no cover (reconcile just wrote this row)
            return JSONResponse({"detail": "registration failed"}, status_code=500)
        return JSONResponse({"project": view.model_dump(mode="json")})

    @router.patch("/projects/{project_id}")
    async def rename_project(self, project_id: str, body: ProjectRename) -> JSONResponse:
        """Set the project's display name, in its own config file."""
        await self._reconcile()
        view = await self._project_view(project_id)
        if view is None:
            return JSONResponse({"detail": _UNKNOWN_PROJECT}, status_code=404)
        project_registry.set_display_name(Path(view.root_path), body.name)
        await self._reconcile()
        renamed = await self._project_view(project_id)
        if renamed is None:  # pragma: no cover (reconcile just wrote this row)
            return JSONResponse({"detail": _UNKNOWN_PROJECT}, status_code=404)
        return JSONResponse({"project": renamed.model_dump(mode="json")})

    @router.delete("/projects/{project_id}", status_code=204)
    async def delete_project(self, project_id: str) -> Response:
        """Unregister a project: files untouched, projected rows kept, refused while it is answering.

        Existence is checked against the registry of projects (plus the checkout row) before anything
        else, the same resolution PATCH uses, so an id that was never registered is refused. The
        conflict check reads the pool's own held runtimes, never the projection, so it sees a turn that
        just started.
        """
        if project_id not in _known_project_ids():
            return JSONResponse({"detail": _UNKNOWN_PROJECT}, status_code=404)
        answering = any(
            runtime.session_id is not None
            and self._sessions.project_id_of(runtime) == project_id
            and self._sessions.is_answering(runtime.session_id)
            for runtime in self._sessions.held_sessions()
        )
        if answering:
            return JSONResponse({"detail": "a session in this project is answering"}, status_code=409)
        project_registry.unregister(project_id)
        return Response(status_code=204)

    async def _reconcile(self) -> None:
        """Bring the projection's `projects` table up to date with the registry of projects and the checkout."""
        triples = [
            (project.project_id, project.root_path, project_registry.display_name(Path(project.root_path)))
            for project in project_registry.registered_projects()
        ]
        checkout_id = project_id_of(CHECKOUT)
        if not any(project_id == checkout_id for project_id, _, _ in triples):
            triples.append((checkout_id, str(CHECKOUT.resolve()), project_registry.display_name(CHECKOUT)))
        await self._projection.reconcile_projects(triples)

    async def _project_view(self, project_id: str) -> ProjectView | None:
        """One project's row as the shape a caller reads, or `None` when nothing projected it."""
        project_uuid = _uuid_or_none(project_id)
        if project_uuid is None:
            return None
        row = await self._projection.project(project_uuid)
        if row is None:
            return None
        running = sum(
            1
            for runtime in self._sessions.held_sessions()
            if runtime.session_id is not None
            and self._sessions.project_id_of(runtime) == project_id
            and self._sessions.is_answering(runtime.session_id)
        )
        return ProjectView(
            id=str(row.id), name=row.name, root_path=row.root_path, missing=_is_missing(row.root_path), running_sessions=running
        )
