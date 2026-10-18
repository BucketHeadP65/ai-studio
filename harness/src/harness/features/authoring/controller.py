"""The authoring routes: send a turn, read what the agent remembers, forget it, steer a turn, keep a draft.

Every route names its session in the path, except the ones the control room's own drawer uses,
which act on the process's own session. Saving is a POST with no body, and saves the draft the
session already holds.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from core.ai.workflows.contracts import AdaptationRefusedError, LibraryError, Lineage, WorkflowIntent
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import JsonValue, ValidationError
from pydantic_ai.messages import ModelMessage, TextPart, UserPromptPart
from wireup import Injected
from wireup.integration.fastapi import WireupRoute

from harness import session as session_state
from harness.domain.contracts import (
    UNKNOWN_SESSION,
    ConversationForgotten,
    Recorded,
    SessionArchived,
    SessionDeleted,
    SessionEvents,
    SessionStarted,
    SessionType,
    TurnSteered,
    said,
    strip_page_context,
)
from harness.features.authoring.api_models import AdaptRequest, ChatRequest, RunAdaptRequest, SessionIn, SteerIn
from harness.features.authoring.domain import Draft
from harness.features.authoring.exceptions import DraftNotSavableError, PartAnsweredError, SessionUnknownError, TurnEmptyError
from harness.features.authoring.internal_contracts import Chat, EndedRuns
from harness.features.authoring.service import (
    BUSY_MESSAGE,
    EMPTY_TURN,
    PART_ANSWERED,
    adapt_from_run,
    adapt_saved,
    draft_payload,
    ensure_session_id,
    record_intent,
    reply_payload,
    save_draft,
)
from harness.features.authoring.wire import messages_of, streaming_response
from harness.features.sessions.contracts import Sessions, SessionSummary, UsageTotals
from harness.session import Runtime, SessionRefresh, verdict_on
from harness.settings import SelfAuthoringSettings, StateSettings


def _created_summary(pool: Sessions, runtime: Runtime, *, session_type: SessionType) -> dict[str, JsonValue]:
    """A just-opened session as a listing row: no turns, no usage, nobody answering into it yet."""
    session_id = runtime.session_id or ""
    opened = datetime.now(tz=UTC).replace(microsecond=0)
    return SessionSummary(
        id=session_id,
        project_id=pool.project_id_of(runtime),
        title=f"session {session_id[:8]}",
        started_at=opened,
        last_activity_at=opened,
        archived=False,
        answering=False,
        turn_count=0,
        usage=UsageTotals(),
        kind=session_type,
        cost_usd=Decimal(0),
        context=None,
    ).model_dump(mode="json")


def _master_session_id(events: Sequence[Recorded]) -> str | None:
    """The standing master session's id, or `None` when none is live on this checkout's timeline."""
    archived: dict[str, bool] = {}
    deleted: set[str] = set()
    masters: list[str] = []
    for record in events:
        match record.event:
            case SessionStarted(session_id=session_id, session_kind="master"):
                masters.append(session_id)
            case SessionArchived(session_id=session_id, archived=flag):
                archived[session_id] = flag
            case SessionDeleted(session_id=session_id):
                deleted.add(session_id)
            case _:
                pass
    return next((sid for sid in masters if sid not in deleted and not archived.get(sid, False)), None)


def _turns(history: Sequence[ModelMessage]) -> list[dict[str, str]]:
    """The dialogue only: what the person typed and what the agent said back, without tool calls or their returns."""
    turns: list[dict[str, str]] = []
    for message in history:
        for part in message.parts:
            if isinstance(part, UserPromptPart) and (asked := _asked(part.content)):
                turns.append({"who": "you", "text": asked})
            elif isinstance(part, TextPart) and (spoken := said(part.content)):
                turns.append({"who": "agent", "text": spoken})
    return turns


def _asked(content: object) -> str:
    """A user prompt's text, its page-context fence stripped. `content` is a string here, but the type allows attachments."""
    if isinstance(content, str):
        return strip_page_context(content).strip()
    if isinstance(content, Sequence):
        return strip_page_context(" ".join(item.strip() for item in content if isinstance(item, str))).strip()
    return ""


async def _save(session: Runtime, self_authoring: SelfAuthoringSettings) -> JSONResponse:
    """Save `session`'s draft into its library, answering a refusal with its field-by-field findings."""
    try:
        workflow_id = await save_draft(session.conversation.draft, library=session.library, registry=session.registry, self_authoring=self_authoring)
    except DraftNotSavableError as exc:
        # The field-by-field reading goes with the sentence, so a form can put each finding beside its own box.
        readiness = exc.readiness.model_dump(mode="json") if exc.readiness is not None else None
        return JSONResponse({"ok": False, "detail": str(exc), "readiness": readiness}, status_code=400)
    return JSONResponse({"ok": True, "id": workflow_id})


class AuthoringController:
    """One conversation and the draft it produces: the turn, sessions, memory, the master doc, save."""

    router = APIRouter(prefix="/api", tags=["authoring"], route_class=WireupRoute)

    def __init__(  # noqa: PLR0913, PLR0917 (one collaborator per constructor parameter, each read by a route below)
        self,
        sessions: Sessions,
        refresh: SessionRefresh,
        session_log: SessionEvents,
        state: StateSettings,
        self_authoring: SelfAuthoringSettings,
        ended_runs: EndedRuns,
    ) -> None:
        """Hold what the routes read.

        Args:
            sessions: The sessions this process keeps open.
            refresh: How a session's projected rows are brought up to date after a route changes it.
            session_log: The session log the routes read and record to.
            state: The state home, where the process's own session keeps its draft.
            self_authoring: The custom-item root and the name a save is attributed to.
            ended_runs: The ended runs an adaptation may cite.
        """
        self._sessions = sessions
        self._refresh = refresh
        self._session_log = session_log
        self._state = state
        self._self_authoring = self_authoring
        self._ended_runs = ended_runs

    @router.post("/sessions/{session_id}/messages", response_model=None)
    async def send_message(self, session_id: str, body: ChatRequest, request: Request, chat: Injected[Chat]) -> Response:
        """One turn in `session_id`, streamed as the Vercel AI data-stream protocol.

        Sessions answer in parallel. A second turn in the same session is refused with 409 for
        exactly as long as the first one runs. The body carries the new message only, and what the
        model saw before it comes from the record. A body with nothing to say and nothing to answer
        is refused with 422. A body that says `design_only` gets a turn offered no verb that starts
        a run or changes a standing schedule.
        """
        try:
            turn = await chat.send_message(
                session_id, messages_of(body), context=body.context, tool_results=body.tool_results, design_only=body.design_only
            )
        except SessionUnknownError:
            return JSONResponse({"detail": UNKNOWN_SESSION}, status_code=404)
        except session_state.SessionBusyError:
            return JSONResponse({"detail": BUSY_MESSAGE}, status_code=409)
        except PartAnsweredError:
            return JSONResponse({"detail": PART_ANSWERED}, status_code=409)
        except TurnEmptyError:
            return JSONResponse({"detail": EMPTY_TURN}, status_code=422)
        return streaming_response(body, request.headers.get("accept"), turn)

    @router.post("/sessions")
    async def open_session(self, body: SessionIn | None = None) -> JSONResponse:
        """Open a fresh session, the New chat button, and answer with the row a listing shows.

        The very next request may name the session. The row is built from what the pool holds, and
        the projection later overwrites it from the session log. An unknown `project_id` is 404.
        """
        pool = self._sessions
        project_id = body.project_id if body is not None else None
        session_type = body.kind if body is not None else "chat"
        root = await pool.root_of_project(project_id) if project_id else None
        if project_id and root is None:
            return JSONResponse({"detail": f"no project {project_id!r} here"}, status_code=404)
        runtime = await pool.create(root, session_kind=session_type)
        session_state.refresh_projection(runtime, self._refresh)
        return JSONResponse({"session": _created_summary(pool, runtime, session_type=session_type)})

    @router.get("/master")
    async def master_session(self) -> JSONResponse:
        """The follow-me companion's one session, created on the first request and answered by the same id after that.

        Every answer also opens the session in this process, so its context reading is available right away.
        """
        pool = self._sessions
        existing = _master_session_id(self._session_log.events())
        if existing is not None:
            await pool.get(existing)
            return JSONResponse({"session_id": existing})
        runtime = await pool.create(None, session_kind="master")
        session_state.refresh_projection(runtime, self._refresh)
        return JSONResponse({"session_id": runtime.session_id or ""})

    @router.post("/sessions/{session_id}/steer")
    async def steer_session(self, session_id: str, body: SteerIn) -> JSONResponse:
        """Hand a message to the turn `session_id` is part-way through, without ending it.

        The text reaches the model inside the running turn: at its next model request, or, when the
        turn was about to end, as a continuation that addresses it. 409 when nothing is in flight,
        which tells the caller to send an ordinary turn instead.
        """
        pool = self._sessions
        try:
            await pool.get(session_id)
        except KeyError:
            return JSONResponse({"detail": UNKNOWN_SESSION}, status_code=404)
        text = body.message.strip()
        if not text:
            return JSONResponse({"detail": "nothing to steer with"}, status_code=400)
        runtime = pool.steering(session_id)
        if runtime is None:
            return JSONResponse({"detail": "nothing is in flight in this session"}, status_code=409)
        runtime.steer_inbox.append(body.message)
        runtime.live_prompts.append(body.message)
        runtime.session_log.record(TurnSteered(chars=len(body.message), session_id=session_id))
        return JSONResponse({"ok": True})

    @router.post("/sessions/{session_id}/interrupt")
    async def interrupt_session(self, session_id: str) -> JSONResponse:
        """Stop the turn this session is part-way through. The session takes a new message at once.

        The cancelled turn records itself abandoned as it unwinds. 409 when nothing is in flight.
        """
        pool = self._sessions
        try:
            await pool.get(session_id)
        except KeyError:
            return JSONResponse({"detail": UNKNOWN_SESSION}, status_code=404)
        if not pool.interrupt(session_id):
            return JSONResponse({"detail": "nothing is in flight in this session"}, status_code=409)
        return JSONResponse({"ok": True})

    @router.get("/sessions/{session_id}/conversation")
    async def session_conversation(self, session_id: str) -> JSONResponse:
        """What this session remembers and the draft it is holding, so a reload shows both."""
        try:
            runtime = await self._sessions.get(session_id)
        except KeyError:
            return JSONResponse({"detail": UNKNOWN_SESSION}, status_code=404)
        return JSONResponse({"turns": _turns(runtime.conversation.history), **draft_payload(runtime)})

    @router.get("/conversation")
    async def conversation(self) -> JSONResponse:
        """What the agent currently remembers, and which session is remembering it.

        The turns include whatever boot resumed from the last run. A read never creates a session:
        `session_id` is null until a conversation is started (`POST /conversation`) or resumed at boot.
        """
        session = self._sessions.default
        return JSONResponse({"turns": _turns(session.conversation.history), "session_id": session.session_id})

    @router.post("/conversation")
    async def start_conversation(self) -> JSONResponse:
        """Start the process's conversation explicitly, so a read never writes.

        Idempotent: a second call answers the same id and records nothing more. The id this answers
        is what the next turn is sent to.
        """
        return JSONResponse({"session_id": ensure_session_id(self._sessions.default)})

    @router.delete("/conversation")
    async def forget(self) -> JSONResponse:
        """Start a new conversation. The transcript on disk stays, but the next boot no longer resumes it."""
        session = self._sessions.default
        self._session_log.record(ConversationForgotten(session_id=session.session_id))
        self._draft_file(session).unlink(missing_ok=True)
        session.conversation.history = []
        session.conversation.draft = Draft()
        session.conversation.last_workflow = None
        session.conversation.last_issues = ()
        session.context_usage = None
        session.session_id = None
        return JSONResponse({"ok": True})

    @router.delete("/sessions/{session_id}/conversation")
    async def forget_session(self, session_id: str) -> JSONResponse:
        """Start this session over: history and draft gone, the session itself still who it was."""
        try:
            runtime = await self._sessions.get(session_id)
        except (KeyError, session_state.MissingProjectRootError):
            return JSONResponse({"detail": UNKNOWN_SESSION}, status_code=404)
        runtime.session_log.record(ConversationForgotten(session_id=runtime.session_id))
        self._draft_file(runtime).unlink(missing_ok=True)
        runtime.conversation.history = []
        runtime.conversation.draft = Draft()
        runtime.conversation.last_workflow = None
        runtime.conversation.last_issues = ()
        runtime.context_usage = None
        session_state.refresh_projection(runtime, self._refresh)
        return JSONResponse({"ok": True})

    @router.post("/adapt")
    async def adapt_saved_workflow(self, body: AdaptRequest) -> JSONResponse:
        """Turn a saved workflow plus a diff into the draft on screen, keeping the link to its parent.

        Nothing is saved: this produces a proposal the person still reads and still presses Save on.
        404 for a workflow this library cannot read, 400 for a diff that names something the parent
        does not have.
        """
        session = self._sessions.default
        try:
            draft, lineage = await adapt_saved(
                body.workflow_id, body.ops, library=session.library, evidence=body.evidence, predictions=body.predictions
            )
        except (LibraryError, KeyError, ValidationError) as exc:
            return JSONResponse({"ok": False, "detail": f"could not read {body.workflow_id!r}: {exc}"}, status_code=404)
        except AdaptationRefusedError as exc:
            return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
        return self._proposed(session, draft, lineage)

    @router.post("/sessions/{session_id}/adapt")
    async def adapt_in_session(self, session_id: str, body: RunAdaptRequest) -> JSONResponse:
        """Turn a saved workflow, a diff, and the run that prompted it into `session_id`'s own draft.

        Reuse, with what it is reused from written down: the child records its parent, the ops that
        made it, the run the person is adapting because of, what that run was observed to do, their
        own reason, and whatever `evidence` the caller sent, which the citation is added to rather
        than replaces. The parent is read and never written, and the cited run is read and never
        written. Nothing is saved into the library and no run starts: this produces a proposal the
        person still reads and still presses Save on.

        404 for a session nothing knows, a workflow this library cannot read, or a source run
        nothing here archived. 400 for a diff that names something the parent does not have.
        """
        try:
            runtime = await self._sessions.get(session_id)
        except (KeyError, session_state.MissingProjectRootError):
            return JSONResponse({"ok": False, "detail": UNKNOWN_SESSION}, status_code=404)
        source = self._ended_runs.find(body.source_run_id) if body.source_run_id else None
        if body.source_run_id and source is None:
            return JSONResponse({"ok": False, "detail": f"no run {body.source_run_id!r} is archived here"}, status_code=404)
        try:
            if source is None:
                draft, lineage = await adapt_saved(
                    body.workflow_id, body.ops, library=runtime.library, evidence=body.evidence, predictions=body.predictions
                )
            else:
                draft, lineage = await adapt_from_run(
                    body.workflow_id,
                    body.ops,
                    library=runtime.library,
                    source=source,
                    feedback=body.feedback,
                    evidence=body.evidence,
                    predictions=body.predictions,
                )
        except (LibraryError, KeyError, ValidationError) as exc:
            return JSONResponse({"ok": False, "detail": f"could not read {body.workflow_id!r}: {exc}"}, status_code=404)
        except AdaptationRefusedError as exc:
            return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
        return self._proposed(runtime, draft, lineage)

    def _proposed(self, session: Runtime, draft: Draft, lineage: Lineage) -> JSONResponse:
        """Put `draft` on `session`'s screen, checked, and answer it the way a settled turn does."""
        session.conversation.draft = draft
        session_state.remember_draft(draft, path=self._draft_file(session))
        workflow, issues = verdict_on(draft.spec_yaml, session.registry)
        session.conversation.last_workflow = workflow if not issues else None
        session.conversation.last_issues = tuple(issues)
        return JSONResponse({"ok": True, "lineage": lineage.model_dump(mode="json"), **reply_payload(session, "")})

    @router.put("/sessions/{session_id}/intent")
    async def record_session_intent(self, session_id: str, body: WorkflowIntent) -> JSONResponse:
        """Record what the person says they asked for onto this session's draft, and answer the draft.

        Records only. Nothing is validated against the spec, nothing is saved into the library, and
        no run or schedule is started: an intent is the request in the person's words, kept beside
        the proposal so a reload reads back what they last said. The body is the whole statement
        every time, never a patch of one field.
        """
        try:
            runtime = await self._sessions.get(session_id)
        except KeyError:
            return JSONResponse({"detail": UNKNOWN_SESSION}, status_code=404)
        draft = record_intent(runtime, body)
        session_state.remember_draft(draft, path=self._draft_file(runtime))
        return JSONResponse({"ok": True, **draft_payload(runtime)})

    @router.post("/save")
    async def save_current(self) -> JSONResponse:
        """The save button: a deliberate human act, refused unless the draft validates."""
        return await _save(self._sessions.default, self._self_authoring)

    @router.post("/sessions/{session_id}/save")
    async def save_session(self, session_id: str) -> JSONResponse:
        """The save button aimed at one session: saves THAT session's draft, and no other's."""
        try:
            runtime = await self._sessions.get(session_id)
        except KeyError:
            return JSONResponse({"detail": UNKNOWN_SESSION}, status_code=404)
        return await _save(runtime, self._self_authoring)

    def _draft_file(self, session: Runtime) -> Path:
        """Where `session` keeps its draft: its own file, or the state home's `draft.json` for the process's own session."""
        return session.draft_file if session.draft_file is not None else self._state.draft

