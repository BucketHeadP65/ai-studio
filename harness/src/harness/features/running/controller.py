"""The running routes: start a run, watch it, answer its gate, pause it to change the graph, and read ended runs.

Every run belongs to the session that started it, and its gate is answered in that session. The
unscoped routes act on the pool's default session. The session-scoped routes name their session in
the path and run the same handler bodies. Pause, stop and automode are refused on the Temporal
engine, where the run outlives this process.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

from core.ai.artifacts import MAX_CANONICAL_BYTES, ArtifactDocument, ArtifactRef, ArtifactUnavailableError, canonical_bytes, text_media
from core.ai.evaluation.contracts import OwnerReview, OwnerReviewConflictError, OwnerReviewRequest, OwnerReviewService
from core.ai.workflows.contracts import (
    Approval,
    FromParameter,
    LibraryError,
    Outcome,
    ResultArtifactSpec,
    Workflow,
    prepare,
    spec_identity,
    trust_level,
    unsatisfied_parameters,
    validate,
)
from core.observability import get_logger
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import JsonValue, TypeAdapter, ValidationError
from wireup import Injected
from wireup.integration.fastapi import WireupRoute

from harness.domain.contracts import DEFAULT_GATE_MODE, GATE_MODES, UNKNOWN_SESSION, AdmissionRefused, admitted_arguments, presentation_of
from harness.features.running import service
from harness.features.running.api_models import (
    ApproveRequest,
    AutomodeRequest,
    Decision,
    PinRequest,
    ResolveArtifactRequest,
    ResumeRequest,
    RunRequest,
    SteerStepIn,
)
from harness.features.running.conversations import StepConversation
from harness.features.running.domain.ended_run import RunArchiveRecord, producing_address
from harness.features.running.domain.progress import PendingReview
from harness.features.running.exceptions import (
    AutomodeUnavailableError,
    RunEngineUnavailableError,
    RunGraphRefusedError,
    RunStateRefusedError,
    WorkflowNotRunnableError,
)
from harness.features.running.records import RunCollaborators
from harness.features.running.spend import SpendView, view_of
from harness.features.running.spend_report import DEFAULT_SINCE_HOURS, SpendReport, SpendReporter, parse_scope
from harness.features.running.step_stream import replay_floor_chunk, steer_chunk
from harness.features.sessions.contracts import Sessions
from harness.rendering import graph_payload, render_for_review
from harness.session import Runtime, SessionBusyError

if TYPE_CHECKING:
    from core.cqrs.contracts import TopicSink

    from harness.features.running.internal_contracts import SpendLogQueries
    from harness.features.running.records import RunRecords
    from harness.settings import RunSettings

_logger = get_logger(__name__)

_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)

_UNKNOWN_RUN = "no run with that id here"

_LIVE_RESULT = Outcome(status="unknown", summary="This run has not ended, so there is nothing to check yet.")
"""What a run still in flight projects as, so a listing row has the same shape whatever it holds."""

_UNKNOWN_STEP_CONVERSATION = "no live step with that node and item here"

_ONE_AT_A_TIME = "this session is busy, so nothing was started in it"
"""The refusal for a start in a session that is busy with a chat turn or a run."""


@dataclass(frozen=True)
class _Refusal:
    """Why a reference could not be resolved, in the status the caller should read it as."""

    status: int
    message: str


def _refused(refusal: _Refusal) -> JSONResponse:
    return JSONResponse({"ok": False, "message": refusal.message}, status_code=refusal.status)


def _declared(record: RunArchiveRecord, artifact_id: str) -> ResultArtifactSpec | None:
    """The artifact `artifact_id` names in the snapshot of the workflow that actually ran."""
    try:
        spec = Workflow.model_validate(record.spec)
    except ValidationError:
        return None
    if spec.result is None:
        return None
    return next((artifact for artifact in spec.result.artifacts if artifact.id == artifact_id), None)


def _archived_value(record: RunArchiveRecord, artifact: ResultArtifactSpec) -> JsonValue | _Refusal:
    """The value this run fixed under one declared artifact, or why it cannot be read back."""
    wire = artifact.source
    if isinstance(wire, FromParameter):
        found: object | None = record.params.get(wire.parameter)
    else:
        found = record.values.get(wire.node if wire.turn is None else f"{wire.node}.{wire.turn}")
    if found is None:
        return _Refusal(404, "this run recorded no value under that artifact's source")
    if artifact.field is not None:
        if not isinstance(found, dict) or artifact.field not in found:
            return _Refusal(404, f"the archived value carries no field {artifact.field!r}")
        found = found[artifact.field]
    return _JSON_VALUE.validate_python(found)


def _reviewed_value(record: RunArchiveRecord, ref: ArtifactRef) -> JsonValue | _Refusal:
    """The value a gate reviewed at `ref`'s address, checked against the digest asking for it."""
    captured = next((kept for kept in record.reviewed if kept.ref.node_id == ref.node_id and kept.ref.turn == ref.turn), None)
    if captured is None:
        return _Refusal(
            404, "no artifact with that id is declared by the workflow this run ran, and no reviewed value sits at that address"
        )
    if captured.ref.value_digest != ref.value_digest:
        return _Refusal(409, "the reviewed value at that address has a different digest")
    return _JSON_VALUE.validate_json(captured.canonical)


def _resolved(record: RunArchiveRecord, ref: ArtifactRef) -> ArtifactDocument | _Refusal:
    """The document `ref` addresses in one archived run, by its declared artifact or else a gate's reviewed value, or a refusal."""
    artifact = _declared(record, ref.id)
    if artifact is None:
        found = _reviewed_value(record, ref)
    else:
        node_id, turn = producing_address(artifact.source)
        if (ref.node_id, ref.turn) != (node_id, turn):
            return _Refusal(409, f"artifact {ref.id!r} is produced by {node_id!r}, not by the node this reference names")
        found = _archived_value(record, artifact)
    if isinstance(found, _Refusal):
        return found
    if text_media(ref.media_type) and not isinstance(found, str):
        return _Refusal(415, f"media type {ref.media_type!r} carries a string, and the archived value is not one")
    payload = canonical_bytes(found)
    if len(payload) > MAX_CANONICAL_BYTES:
        return _Refusal(413, f"the canonical value is {len(payload)} bytes, past the {MAX_CANONICAL_BYTES} a reference may address")
    try:
        return ArtifactDocument(ref=ref, value=found)
    except ValidationError:
        return _Refusal(409, "the archived value no longer matches the digest this reference addresses")


class ArtifactResolutionRefusedError(ArtifactUnavailableError):
    """Why one owner-review artifact reference could not be resolved, in the status a caller reads it as."""

    def __init__(self, refusal: _Refusal) -> None:
        """Carry the refusal's message and its HTTP status.

        Args:
            refusal: Why the reference could not be resolved, and the status to answer with.
        """
        super().__init__(refusal.message)
        self.status = refusal.status


@dataclass(frozen=True)
class RunArtifactResolver:
    """`ArtifactResolver` over one already-fetched run's own archived record.

    It answers by the rules `POST /api/runs/history/{run_id}/artifacts/resolve` answers with, and
    refuses a reference naming another run than `run_id`.
    """

    record: RunArchiveRecord
    run_id: str

    async def resolve(self, ref: ArtifactRef) -> ArtifactDocument:
        """The document `ref` addresses in this resolver's own run.

        Args:
            ref: The reference to resolve.

        Returns:
            The document holding the value the reference addresses.

        Raises:
            ArtifactResolutionRefusedError: When the reference names another run or the archived record cannot answer it.
                Its `status` is the HTTP status to answer with.
        """
        if ref.run_id != self.run_id:
            raise ArtifactResolutionRefusedError(_Refusal(409, "this reference belongs to another run than the one being reviewed"))
        answer = _resolved(self.record, ref)
        if isinstance(answer, _Refusal):
            raise ArtifactResolutionRefusedError(answer)
        return answer


_REVIEW_SOURCE = "app:harness"
"""The route source stamped into every owner review this deployment's own API accepts."""


# The acts below answer a `JSONResponse` and carry their own HTTP guard. The routes and the
# follow-me console both call these same functions, so an act performed either way meets the same
# guard, writes the same run event and refuses in the same words.


def run_automode(session: Runtime, body: AutomodeRequest, run: RunSettings) -> JSONResponse:
    """Set `session`'s live automode on or off.

    On sets `gate_mode` to `"auto"` and off sets it to `"wait"`. It is refused with a 409 on
    the Temporal engine, where automode is chosen at launch, and when nothing is running.

    Args:
        session: The session whose run is changed.
        body: Whether automode is on.
        run: The run settings, which name the engine.

    Returns:
        The route's answer: `ok` and `auto_approve`, or a 409 refusal.
    """
    try:
        service.set_session_automode(session, run, on=body.on)
    except RunStateRefusedError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
    return JSONResponse({"ok": True, "auto_approve": body.on})


async def run_pause(session: Runtime, run: RunSettings) -> JSONResponse:
    """Cancel `session`'s running graph, leaving its run log exactly as a restart would.

    It is refused with a 409 on the Temporal engine and when nothing is running.

    Args:
        session: The session whose run is paused.
        run: The run settings, which name the engine.

    Returns:
        The route's answer: `ok` and the paused workflow id, or a 409 refusal.
    """
    try:
        await service.pause_session_run(session, run)
    except RunStateRefusedError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
    return JSONResponse({"ok": True, "paused": session.active.workflow_id})


async def run_stop(session: Runtime, run: RunSettings, records: RunRecords) -> JSONResponse:
    """Cancel `session`'s running graph and end the run for good, unlike a pause.

    It is refused with a 409 when nothing is running, and on the Temporal engine unless the run is
    paused, which is stopped on either engine because its run log is in this process.

    Args:
        session: The session whose run is stopped.
        run: The run settings, which name the engine.
        records: Where the stopped run is recorded and archived.

    Returns:
        The route's answer: `ok` and the stopped workflow id, or a 409 refusal.
    """
    try:
        await service.stop_session_run(session, run, records)
    except RunStateRefusedError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
    return JSONResponse({"ok": True, "stopped": session.active.workflow_id})


async def run_resume(session: Runtime, body: ResumeRequest | None, records: RunRecords, topics: TopicSink) -> JSONResponse:
    """Relaunch `session`'s waiting run.

    The graph is the body's `spec_yaml`, else the draft the session settled on, else the workflow
    the waiting run was launched from. A run a restart paused skips the draft and resumes its own
    logged workflow. Whichever it is, it is validated before the run relaunches. A run a restart paused
    keeps its logged gate mode, any other keeps the session's live one.

    Args:
        session: The session whose run resumes.
        body: The graph to resume with, or `None` to fall back.
        records: Where the resumed run is recorded.
        topics: Where the resumed run announces that it moved.

    Returns:
        The route's answer: the running workflow id with the replayed and withdrawn nodes, or a 400 or 409 refusal.
    """
    try:
        resumed = await service.resume_session_run(
            session, spec_yaml=body.spec_yaml if body is not None else "", records=records, topics=topics
        )
    except RunStateRefusedError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
    except (RunGraphRefusedError, WorkflowNotRunnableError) as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    return JSONResponse(
        {"ok": True, "running": resumed.workflow_id, "replayed": list(resumed.replayed), "withdrawn": list(resumed.withdrawn)}
    )


async def run_decide(session: Runtime, body: Decision, run: RunSettings) -> JSONResponse:
    """Settle the gate `session` is waiting at, on whichever engine is holding it.

    Args:
        session: The session whose run is waiting.
        body: The verdict and any feedback.
        run: The run settings, which name the engine.

    Returns:
        The route's answer: `ok` and `approved`, or a 409 refusal when nothing is waiting.
    """
    try:
        await service.decide_session_gate(session, run, approve=body.approve, feedback=body.feedback)
    except RunStateRefusedError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
    return JSONResponse({"ok": True, "approved": body.approve})


def _no_such_session() -> JSONResponse:
    """The one refusal every session-scoped route gives for an id nothing knows."""
    return JSONResponse({"ok": False, "message": UNKNOWN_SESSION}, status_code=404)


async def _start(  # noqa: PLR0911 (each refusal a launch can meet is its own early return with its own status)
    session: Runtime, body: RunRequest, run: RunCollaborators
) -> JSONResponse:
    """Start `body`'s workflow in `session` once the library, the environment and the arguments agree, or refuse."""
    if session.active.task is not None and not session.active.task.done():
        return JSONResponse({"ok": False, "message": f"{session.active.workflow_id} is still running"}, status_code=409)
    if (paused := session.active.paused_run_refusal()) is not None:
        return JSONResponse({"ok": False, "message": paused}, status_code=409)
    try:
        spec = await session.library.fetch_workflow(body.workflow_id)
    except (LibraryError, ValidationError) as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=404)
    report = validate(spec, session.registry)
    if not report.ok:
        refusal = WorkflowNotRunnableError(body.workflow_id, issues=report.issues)
        return JSONResponse({"ok": False, "message": str(refusal)}, status_code=400)
    unsatisfied = unsatisfied_parameters(spec, body.arguments)
    if unsatisfied:
        return JSONResponse({"ok": False, "message": f"fill in: {', '.join(unsatisfied)}"}, status_code=400)
    try:
        arguments = await admitted_arguments(
            spec, body.arguments, presentation=await presentation_of(session.library, body.workflow_id), names=run.knowledge_bases
        )
    except AdmissionRefused as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

    try:
        await service.start(
            session,
            records=run.records,
            topics=run.topics,
            spec=spec,
            workflow_id=body.workflow_id,
            params=arguments,
            gate_mode=body.gate_mode,
            run=run.run_settings,
            run_title=body.run_title,
        )
    except AutomodeUnavailableError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
    except RunEngineUnavailableError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
    return JSONResponse({"ok": True, "running": body.workflow_id, "session_id": session.session_id})


def _state(session: Runtime, since: int) -> JSONResponse:
    """`session`'s run as the graph polls it: the live sets, and every transition after `since`."""
    running = session.active.task is not None and not session.active.task.done()
    events = session.active.progress.since(since)
    return JSONResponse(
        {
            "running": running,
            "workflow": session.active.workflow_id,
            "result": session.active.result,
            "active": sorted(session.active.progress.running),
            "done": list(session.active.progress.done),
            "failed": sorted(session.active.progress.failed),
            "waiting_at": session.active.pending.gate_id if session.active.pending else None,
            "events": events,
            "seq": len(session.active.progress.events),
            "gate_mode": session.active.gate_mode,
            # Read off `gate_mode`, which both an automode flip and a launch set.
            "auto_approve": session.active.gate_mode == "auto",
        }
    )


def _values(session: Runtime) -> JSONResponse:
    """`session`'s per-node outputs so far: live during the run, and still here after it ends."""
    return JSONResponse(
        {
            "workflow": session.active.workflow_id,
            "values": {node_id: render_for_review(value) for node_id, value in session.active.values.items()},
        }
    )


async def _step_frames(conversation: StepConversation, since: int) -> AsyncIterator[str]:
    """The floor frame read off the first replayed chunk, then every chunk from `since`, then the closing `[DONE]` frame."""
    # The floor comes from the first chunk replay delivers. One computed earlier can go stale,
    # because eviction can advance before the replay starts. A closed stream evicts nothing more.
    floor_sent = False
    async for index, chunk in conversation.stream.replay(since):
        if not floor_sent:
            yield replay_floor_chunk(index, since)
            floor_sent = True
        yield chunk
    if not floor_sent:
        yield replay_floor_chunk(conversation.stream.floor(since), since)
    yield "data: [DONE]\n\n"


def _gate_payload(pending: PendingReview) -> dict[str, object]:
    """One waiting gate as the reviewer's card: what to read, and what a rejection costs."""
    return {
        "gate_id": pending.gate_id,
        "workflow": pending.workflow,
        "reviewing": pending.reviewing,
        "rejection_reruns": pending.rejection_reruns,
    }


def _live_envelope(session: Runtime, spend: SpendLogQueries) -> dict[str, object]:
    """A run still in flight, in the shape `_envelope` gives an ended one, with identity and timing only."""
    return {
        "run_id": session.session_id or "",
        "workflow_id": session.active.workflow_id,
        "run_title": session.active.run_title,
        "outcome": "live",
        "failure": "",
        "started_at": session.active.started_at,
        "ended_at": "",
        "runner": "",
        "session_id": session.session_id or "",
        "params": {},
        "gates_approved": [],
        "gates_rejected": {},
        "gates_auto": [],
        "gates_orchestrated": [],
        "pinned": False,
        "result": _LIVE_RESULT.model_dump(mode="json"),
        "spend": _spend_of(session.active.run_id, spend),
    }


def _envelope(record: RunArchiveRecord, spend: SpendLogQueries) -> dict[str, object]:
    """One archived run without its heavy fields, as a listing reads it, a missing checked outcome read as unknown."""
    return {
        **record.model_dump(mode="json", exclude={"spec", "values", "events", "conversations", "reviewed"}),
        "result": record.result.model_dump(mode="json"),
        "spend": view_of(spend.find(record.run_id)),
    }


def _spend_of(run_id: str | None, spend: SpendLogQueries) -> SpendView | None:
    """The run's spend as the wire carries it, or `None` when the spend log holds none for it."""
    return None if run_id is None else view_of(spend.find(run_id))


class RunningController:
    """Every run belongs to a session: what is running, what is paused, what it has spent."""

    router = APIRouter(prefix="/api", tags=["running"], route_class=WireupRoute)

    def __init__(self, sessions: Sessions, run: RunCollaborators) -> None:
        """Hold the pool every route resolves a session in, and what a run reaches beyond its arguments.

        Args:
            sessions: Every session this process holds open, its default the one an unscoped route acts on.
            run: Where a run's records land, where it says it moved, which engine runs it, and the
                pool a run's own settlement turn takes the session's hold in.
        """
        self._sessions = sessions
        self._run = run

    async def _session_for(self, session_id: str) -> Runtime | None:
        """The pooled runtime for `session_id`, or `None` when no timeline holds such a session."""
        try:
            return await self._sessions.get(session_id)
        except KeyError:
            return None

    async def _start_once(self, session: Runtime, body: RunRequest) -> JSONResponse:
        """Start `body`'s workflow in `session` while holding the session's turn, so two starts at once cannot both pass."""
        session_id = session.session_id
        if session_id is None:
            return await _start(session, body, self._run)
        try:
            hold = await self._sessions.begin_turn(session_id)
        except SessionBusyError:
            return JSONResponse({"ok": False, "message": _ONE_AT_A_TIME}, status_code=409)
        except KeyError:
            return _no_such_session()
        try:
            return await _start(session, body, self._run)
        finally:
            hold.release()

    @router.get("/run/limits")
    async def run_limits(self) -> JSONResponse:
        """What this deployment holds a run to, read before anybody starts one.

        `brakes` lists every spend limit a run is held to, each with the window it is counted
        over. An empty list means no spend limit is configured, never a spend limit of zero. `gate_modes`
        lists what a launch may ask for here, and `default_gate_mode` is what a launch that asks
        for nothing runs under.
        """
        offered = [mode for mode in GATE_MODES if mode != "orchestrate" or self._run.run_settings.run_engine != "temporal"]
        return JSONResponse(
            {
                "brakes": [limit.model_dump(mode="json") for limit in self._run.spend_limits],
                "gate_modes": offered,
                "default_gate_mode": DEFAULT_GATE_MODE,
            }
        )

    @router.post("/run")
    async def start_run(self, body: RunRequest) -> JSONResponse:
        """Run a saved workflow in a fresh runner session of its own.

        The run belongs to no chat, so it never writes into a chat's session log. The reply carries
        `session_id`, under which the run's state and gate are read (`/api/sessions/{id}/run/state`).
        It answers as soon as the run starts.
        """
        session = await self._sessions.create(None, session_kind="runner")
        return await self._start_once(session, body)

    @router.post("/sessions/{session_id}/run")
    async def session_start_run(self, session_id: str, body: RunRequest) -> JSONResponse:
        """Run a saved workflow in `session_id`, which is then the session that owns it."""
        session = await self._session_for(session_id)
        if session is None:
            return _no_such_session()
        return await self._start_once(session, body)

    @router.post("/run/automode")
    async def set_automode(self, body: AutomodeRequest) -> JSONResponse:
        """Flip automode on the run in flight: from here on, every gate it reaches passes itself."""
        return run_automode(self._sessions.default, body, self._run.run_settings)

    @router.post("/sessions/{session_id}/run/automode")
    async def session_set_automode(self, session_id: str, body: AutomodeRequest) -> JSONResponse:
        """Flip automode on `session_id`'s run in flight."""
        session = await self._session_for(session_id)
        if session is None:
            return _no_such_session()
        return run_automode(session, body, self._run.run_settings)

    @router.post("/run/pause")
    async def pause_run(self) -> JSONResponse:
        """Stop the run where it stands, keeping its run log, so the graph can be edited underneath it.

        Nothing is archived, because a paused run has not ended. It resumes exactly as a run stopped
        by a restart does. Refused on the Temporal engine.
        """
        return await run_pause(self._sessions.default, self._run.run_settings)

    @router.post("/sessions/{session_id}/run/pause")
    async def session_pause_run(self, session_id: str) -> JSONResponse:
        """Stop `session_id`'s run where it stands, keeping its run log."""
        session = await self._session_for(session_id)
        if session is None:
            return _no_such_session()
        return await run_pause(session, self._run.run_settings)

    @router.post("/run/stop")
    async def stop_run(self) -> JSONResponse:
        """End the run for good. Unlike a pause, nothing is left to resume.

        The run is archived with `outcome: "stopped"`, like a completed or failed run, and like a
        failure it earns no trust. Refused on the Temporal engine.
        """
        return await run_stop(self._sessions.default, self._run.run_settings, self._run.records)

    @router.post("/sessions/{session_id}/run/stop")
    async def session_stop_run(self, session_id: str) -> JSONResponse:
        """End `session_id`'s run for good."""
        session = await self._session_for(session_id)
        if session is None:
            return _no_such_session()
        return await run_stop(session, self._run.run_settings, self._run.records)

    @router.post("/run/resume")
    async def resume_run(self, body: ResumeRequest | None = None) -> JSONResponse:
        """Resume the paused run against an edited graph. The work it had already settled replays.

        The graph is the body's if it sent one, else the session's settled draft, else the workflow
        the run was launched from. A run a restart paused resumes its own logged workflow instead of
        the draft. Whichever it is, it is validated here before the run resumes.
        """
        return await run_resume(self._sessions.default, body, self._run.records, self._run.topics)

    @router.post("/sessions/{session_id}/run/resume")
    async def session_resume_run(self, session_id: str, body: ResumeRequest | None = None) -> JSONResponse:
        """Resume `session_id`'s paused run against an edited graph."""
        session = await self._session_for(session_id)
        if session is None:
            return _no_such_session()
        return await run_resume(session, body, self._run.records, self._run.topics)

    @router.get("/run/state")
    async def run_state(self, since: int = 0) -> JSONResponse:
        """Where the run is, step by step. Polled by the graph so the boxes can light up.

        `since` is a cursor into the transition log: passing the `seq` last seen gets every transition
        in between, including a step that started and finished inside one polling gap. Pass 0 for the
        whole history, which is what a page reloaded mid-run wants.
        """
        return _state(self._sessions.default, since)

    @router.get("/sessions/{session_id}/run/state")
    async def session_run_state(self, session_id: str, since: int = 0) -> JSONResponse:
        """Where `session_id`'s run is, step by step."""
        session = await self._session_for(session_id)
        if session is None:
            return _no_such_session()
        return _state(session, since)

    @router.get("/run/values")
    async def run_values(self) -> JSONResponse:
        """What each node of the run produced, rendered for reading. Opened from a node on the graph."""
        return _values(self._sessions.default)

    @router.get("/sessions/{session_id}/run/values")
    async def session_run_values(self, session_id: str) -> JSONResponse:
        """What each node of `session_id`'s run produced, rendered for reading."""
        session = await self._session_for(session_id)
        if session is None:
            return _no_such_session()
        return _values(session)

    @router.get("/sessions/{session_id}/run/steps")
    async def session_run_steps(self, session_id: str) -> JSONResponse:
        """Every agentic step with a step conversation in `session_id`'s run, in the order each one opened.

        `settled` is true for a step conversation that has answered at least once and is not executing now. A
        step running again reads `executing` and keeps its last messages until the new turn settles.
        """
        session = await self._session_for(session_id)
        if session is None:
            return _no_such_session()
        steps = [
            {
                "node": node,
                "item": item,
                "executing": conversation.executing,
                "settled": bool(conversation.messages) and not conversation.executing,
            }
            for (node, item), conversation in session.active.conversations.conversations.items()
        ]
        return JSONResponse({"steps": steps})

    @router.get("/sessions/{session_id}/run/steps/{node}/stream", response_model=None)
    async def session_run_step_stream(
        self, session_id: str, node: str, item: str = "", since: Annotated[int, Query(ge=0)] = 0
    ) -> JSONResponse | StreamingResponse:
        """One step's own live stream: a floor frame, then the chunks from `since`, then the tail until the stream closes.

        `since` is the count of chunks already read, so a reconnect asks for only what it missed. The
        first frame is always `data-replay-floor`, naming the index the chunks that follow start from:
        `since` when the buffer still holds it, else the oldest chunk kept. The last frame is
        `[DONE]`, sent once the run has ended.
        """
        session = await self._session_for(session_id)
        if session is None:
            return _no_such_session()
        conversation = session.active.conversations.conversations.get((node, item))
        if conversation is None:
            return JSONResponse({"ok": False, "message": _UNKNOWN_STEP_CONVERSATION}, status_code=404)
        return StreamingResponse(_step_frames(conversation, since), media_type="text/event-stream")

    @router.post("/sessions/{session_id}/run/steps/{node}/steer")
    async def session_steer_step(self, session_id: str, node: str, body: SteerStepIn, item: str = "") -> JSONResponse:
        """Hand a message to one running step's conversation, without ending the step.

        The step reads the message before its next model request, and anybody tailing its stream sees
        it as a `data-steer` frame. A step conversation that is not executing right now answers 409.
        """
        session = await self._session_for(session_id)
        if session is None:
            return _no_such_session()
        conversation = session.active.conversations.conversations.get((node, item))
        if conversation is None:
            return JSONResponse({"ok": False, "message": _UNKNOWN_STEP_CONVERSATION}, status_code=404)
        if not conversation.executing:
            return JSONResponse({"ok": False, "message": "this step is not running right now"}, status_code=409)
        conversation.steer_inbox.append(body.message)
        conversation.stream.append(steer_chunk(body.message, datetime.now(UTC).isoformat()))
        return JSONResponse({"ok": True})

    @router.get("/pending")
    async def pending_review(self) -> JSONResponse:
        """Whether a run is waiting at a gate for this person. Polled by the browser."""
        pending = self._sessions.default.active.pending
        if pending is None:
            return JSONResponse({"pending": None})
        return JSONResponse({"pending": _gate_payload(pending)})

    @router.get("/runs")
    async def live_runs(self) -> JSONResponse:
        """Every session with a run in flight, one card each. Read by the right-hand panel.

        Only the sessions this process holds are read, so a run another process started does not
        appear.
        """
        return JSONResponse({"runs": [view for session in self._sessions.held_sessions() if (view := self._run_view(session)) is not None]})

    def _run_view(self, session: Runtime) -> dict[str, object] | None:
        """`session`'s run as a card while it is running, waiting at a gate or paused, else `None`."""
        state = service.run_phase(session)
        if state is None:
            return None
        pending = session.active.pending
        return {
            "session_id": session.session_id,
            "workflow_id": session.active.workflow_id,
            "run_title": session.active.run_title,
            "state": state,
            "active": sorted(session.active.progress.running),
            "done": list(session.active.progress.done),
            "failed": sorted(session.active.progress.failed),
            "waiting_at": pending.gate_id if pending is not None else None,
            "gate": _gate_payload(pending) if pending is not None else None,
            "started_at": session.active.started_at,
            "spend": _spend_of(session.active.run_id, self._run.records.spend),
        }

    @router.get("/workflows/{workflow_id}/history")
    async def workflow_history(self, workflow_id: str, limit: int = 50) -> JSONResponse:
        """`workflow_id`'s ended runs, newest first. A run of it still in flight leads as `outcome: "live"`."""
        live = [
            _live_envelope(session, self._run.records.spend)
            for session in self._sessions.held_sessions()
            if session.active.workflow_id == workflow_id and service.run_phase(session) is not None
        ]
        ended = [_envelope(record, self._run.records.spend) for record in self._run.records.archive.list_for(workflow_id, limit)]
        return JSONResponse({"runs": [*live, *ended]})

    @router.post("/workflows/{workflow_id}/approve")
    async def approve_workflow(self, workflow_id: str, body: ApproveRequest) -> JSONResponse:
        """Record a person approving a saved workflow, in the evidence log a run writes to.

        No agent tool offers this, because only a person approves a workflow. `by` must name who is
        approving. Answers with the trust record this exact version now carries, in the shape the
        library card and the workflow detail route both read.
        """
        session = self._sessions.default
        by = body.by.strip()
        if not by:
            return JSONResponse(
                {"ok": False, "message": "an approval needs a name. Say who approves this workflow"}, status_code=400
            )
        try:
            spec = await session.library.fetch_workflow(workflow_id)
        except (LibraryError, ValidationError) as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=404)
        if body.note:
            _logger.info("workflow {workflow_id} approved by {by}: {note}", workflow_id=workflow_id, by=by, note=body.note)
        else:
            _logger.info("workflow {workflow_id} approved by {by}", workflow_id=workflow_id, by=by)
        evidence = self._run.records.evidence
        spec_hash = spec_identity(spec)
        await evidence.append(Approval(workflow_id=workflow_id, spec_hash=spec_hash, workflow_version=spec.version, by=by))
        history = await evidence.history_for(spec_hash=spec_hash, workflow_id=workflow_id)
        record = trust_level(history.matched)
        return JSONResponse(
            {
                "ok": True,
                "trust": {
                    "level": record.level,
                    "runs_with_evidence": record.runs_with_evidence,
                    "failed_runs": record.failed_runs,
                    "approved_by": list(record.approved_by),
                    "structural_grades": record.structural_grades,
                    "structural_mean": record.structural_mean,
                    "structural_last": record.structural_last,
                },
            }
        )

    @router.get("/runs/recent")
    async def recent_runs(self, limit: int = 20) -> JSONResponse:
        """Ended runs across every workflow, newest first, for the fleet's Recent lane."""
        return JSONResponse({"runs": [_envelope(record, self._run.records.spend) for record in self._run.records.archive.recent(limit)]})

    @router.get("/runs/history/{run_id}")
    async def run_history_record(self, run_id: str) -> JSONResponse:
        """One ended run, whole: the envelope, every value rendered, and the graph it ran as.

        The answer is the record itself, not wrapped in `{"runs": [...]}`.
        """
        record = self._run.records.archive.get(run_id)
        if record is None:
            return JSONResponse({"ok": False, "message": _UNKNOWN_RUN}, status_code=404)
        owner_reviews = await self._run.records.reviews.for_run(run_id)
        return JSONResponse(self._full_record(record, owner_reviews))

    def _full_record(self, record: RunArchiveRecord, owner_reviews: tuple[OwnerReview, ...]) -> dict[str, object]:
        """The archived record with `values` rendered, the graph of the workflow as it ran (or `None`), and its reviews."""
        graph: dict[str, object] | None = None
        try:
            spec = Workflow.model_validate(record.spec)
        except ValidationError:
            spec = None
        if spec is not None:
            ready = prepare(spec, self._sessions.default.registry)
            graph = graph_payload(spec, ready.registry, {})
        return {
            **record.model_dump(mode="json", exclude={"reviewed"}),
            "result": record.result.model_dump(mode="json"),
            "values": {node_id: render_for_review(value) for node_id, value in record.values.items()},
            "graph": graph,
            "spend": view_of(self._run.records.spend.find(record.run_id)),
            "owner_reviews": [review.model_dump(mode="json") for review in owner_reviews],
        }

    @router.get("/spend")
    async def spend(
        self, scope: str, reporter: Injected[SpendReporter], subject: str = "", since_hours: int = DEFAULT_SINCE_HOURS
    ) -> JSONResponse:
        """What something cost, from the telemetry, for AI Factory Studio.

        `available` says whether the telemetry answered. True carries the report. False carries the
        reason and where each run's spend can still be read. A bad scope, subject or window is a 400
        naming the fix.
        """
        try:
            answer = await reporter.report(parse_scope(scope), subject, since_hours)
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)
        return JSONResponse({"available": isinstance(answer, SpendReport), **answer.model_dump(mode="json")})

    @router.post("/runs/history/{run_id}/artifacts/resolve")
    async def resolve_artifact(self, run_id: str, body: ResolveArtifactRequest) -> JSONResponse:
        """The value one reference addresses, out of that run's own immutable record.

        Read-only and fixed: the reference says which run, which node and which bytes, and this
        answers with those bytes or refuses. A reference naming another run than the path is a 409,
        a run or a value nothing archived is a 404, bytes that disagree with the digest are a 409,
        a value the declared media type cannot carry is a 415, and one past the canonical ceiling
        is a 413. There is no URL form of this question, and no live state is ever read.
        """
        if body.ref.run_id != run_id:
            return _refused(_Refusal(409, "this reference belongs to another run than the one asked about"))
        record = self._run.records.archive.get(run_id)
        if record is None:
            return JSONResponse({"ok": False, "message": _UNKNOWN_RUN}, status_code=404)
        answer = _resolved(record, body.ref)
        if isinstance(answer, _Refusal):
            return _refused(answer)
        return JSONResponse(answer.model_dump(mode="json"))

    @router.post("/runs/history/{run_id}/review")
    async def review_run(self, run_id: str, body: OwnerReviewRequest) -> JSONResponse:
        """Record one owner's accept or reject decision over artifacts this run actually fixed.

        Every reference is resolved and confirmed against this run's own archived record before
        anything is written, by the exact rules `artifacts/resolve` above answers with: **409**
        for a reference naming another run than the path or bytes whose digest disagrees, **404**
        for a run nothing archived or an artifact this run never fixed, **415** for a text media
        type over a value that is not a string, **413** for a value past the canonical ceiling.
        A review id already filed under a different request, run or route source is also a
        **409**. An identical retry (same review id, same request) returns the very event
        first minted, even once the artifacts it named can no longer be resolved. `actor` is
        whoever the request says decided: text the caller supplies, never verified identity.
        """
        record = self._run.records.archive.get(run_id)
        if record is None:
            return JSONResponse({"ok": False, "message": _UNKNOWN_RUN}, status_code=404)
        reviews_service = OwnerReviewService(RunArtifactResolver(record, run_id), self._run.records.reviews)
        try:
            result = await reviews_service.review(run_id, _REVIEW_SOURCE, body, clock=lambda: datetime.now(UTC))
        except OwnerReviewConflictError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
        except ArtifactResolutionRefusedError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=exc.status)
        except ArtifactUnavailableError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
        return JSONResponse(result.model_dump(mode="json"))

    @router.patch("/runs/history/{run_id}")
    async def pin_run(self, run_id: str, body: PinRequest) -> JSONResponse:
        """Keep `run_id` past the retention sweep, or let it go again."""
        if not self._run.records.archive.set_pinned(run_id, pinned=body.pinned):
            return JSONResponse({"ok": False, "message": _UNKNOWN_RUN}, status_code=404)
        return JSONResponse({"ok": True, "pinned": body.pinned})

    @router.delete("/runs/history/{run_id}")
    async def delete_run(self, run_id: str) -> JSONResponse:
        """Remove `run_id` outright, pinned or not."""
        if not self._run.records.archive.delete(run_id):
            return JSONResponse({"ok": False, "message": _UNKNOWN_RUN}, status_code=404)
        return JSONResponse({"ok": True})

    @router.post("/decide")
    async def decide(self, body: Decision) -> JSONResponse:
        """Approve or reject the run waiting at a gate in the default session.

        On the Temporal engine the verdict is sent to the run as a signal, so it answers a gate this
        process was not running when the run reached it.
        """
        return await run_decide(self._sessions.default, body, self._run.run_settings)

    @router.post("/sessions/{session_id}/decide")
    async def session_decide(self, session_id: str, body: Decision) -> JSONResponse:
        """Approve or reject the run waiting in `session_id`, and in no other session."""
        session = await self._session_for(session_id)
        if session is None:
            return _no_such_session()
        return await run_decide(session, body, self._run.run_settings)
