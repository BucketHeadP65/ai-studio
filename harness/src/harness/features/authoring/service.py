"""The authoring use cases the routes call: run one turn of the conversation, and keep the draft it produced.

A turn is refused when the session is unknown, busy or half answered.
A draft is refused unless it exists, parses as a workflow and validates against this run's steps.
"""

from __future__ import annotations

import asyncio
import weakref

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

import anyio
import yaml

from core.ai.history.contracts import StepUsageTap
from core.ai.workflows.contracts import (
    LibraryError,
    Lineage,
    Prediction,
    SavedWorkflow,
    SaveRefusedError,
    StepRegistry,
    Workflow,
    WorkflowIntent,
    WorkflowLibrary,
    adapt,
    intent_issues,
    moved_nodes,
    prepare,
    save,
)
from core.observability import get_logger
from pydantic import ValidationError
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness.step_persistence import continue_run

from harness.adapters.contracts import environment_stamps
from harness.domain.contracts import (
    DraftReadiness,
    DraftSettled,
    SessionStarted,
    TurnAbandoned,
    TurnFailed,
    TurnSettled,
    TurnStarted,
    WorkflowPresentationPatch,
    card_readiness,
    compose_prompt,
    effective_presentation,
    refusal_text,
)
from harness.features.authoring.agent import CHAT_BUDGET
from harness.features.authoring.domain import Draft, SourceRun
from harness.features.authoring.exceptions import (
    DraftNotSavableError,
    PartAnsweredError,
    PlainTurnError,
    SessionUnknownError,
    TurnEmptyError,
)
from harness.features.authoring.internal_contracts import Turn
from harness.features.authoring.pending_calls import answers_every_call, deferred_results, denying_unanswered, pending_calls
from harness.features.followme.contracts import CONSOLE_ACTS, VERBS
from harness.rendering import graph_payload
from harness.session import (
    TURN_CUT_OFF,
    TURN_FAILED,
    answerable,
    bind_workspace_root,
    draft_path,
    newest_run_id,
    refresh_projection,
    remember_draft,
    rescue_history,
    verdict_on,
    workspace_root,
)
from harness.settings import custom_root

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
    from pathlib import Path

    from core.ai.history.contracts import StepUsage
    from pydantic import JsonValue
    from pydantic_ai import AgentRunResult
    from pydantic_ai.messages import ModelMessage, UserContent
    from pydantic_ai.tools import DeferredToolResults
    from pydantic_ai.ui import NativeEvent

    from harness.domain.contracts import ConversationHistory, PageContext, Recorded
    from harness.features.authoring.domain import ToolResultIn
    from harness.session import Runtime, SessionPool, SessionRefresh, TurnHold
    from harness.settings import SelfAuthoringSettings

_logger = get_logger(__name__)

BUSY_MESSAGE = "a turn is already streaming. Wait for it to finish"

PART_ANSWERED = "answer every act this turn is waiting on, or send a message to move past them"
"""The refusal a continuation gets when it answers only some of the calls the run stopped for.
A caller answers every call, or sends a message, which closes what is waiting and starts a new turn."""

SUPERSEDED = "superseded, the person moved on"
"""What a call still waiting is told when a new message arrives instead of its answer."""

EMPTY_TURN = "a turn says something, or answers what the last one stopped for"
"""The refusal a request gets when it carries no words and no answers."""

CONTINUE_PROMPT = "Continue."
"""The prompt a turn is given when its typed text and its page context compose to nothing."""


def settled_draft(  # noqa: PLR0913 (a settled draft is the workflow, the card and the author's intent in one flat contract, the one `check_draft` takes)
    workflow: Workflow,
    *,
    title: str,
    summary: str,
    applicability: str,
    existing: Draft,
    presentation: WorkflowPresentationPatch | None = None,
    intent: WorkflowIntent | None = None,
) -> Draft:
    """The draft a checked workflow leaves on screen: the workflow as YAML, plus the card fields.

    The YAML always comes from `workflow` itself, never from a string the caller holds.
    A blank field keeps its value from `existing`, so absent means unchanged.
    A presentation patch replaces only the words it carries and keeps the rest.

    Args:
        workflow: The workflow that passed validation.
        title: Human-facing name for the draft. Blank keeps `existing.title`.
        summary: One line for the workflow card. Blank keeps `existing.summary`.
        applicability: When to use it and when NOT to. Blank keeps `existing.applicability`.
        existing: The draft this turn started from. Its values fill every blank field.
        presentation: A change to the author's words. `None`, or one carrying nothing, keeps them.
        intent: What the person asked for. `None` keeps the intent `existing` records.

    Returns:
        The new draft. Its lineage is always the lineage of `existing`.
    """
    words = existing.presentation
    if presentation is not None and presentation.changes_anything():
        words = effective_presentation(existing.presentation, presentation)
    return Draft(
        spec_yaml=yaml.safe_dump(workflow.model_dump(mode="json"), sort_keys=False),
        title=title or existing.title,
        summary=summary or existing.summary,
        applicability=applicability or existing.applicability,
        # A lineage is derived, so no caller sets it here.
        lineage=existing.lineage,
        presentation=words,
        intent=intent if intent is not None else existing.intent,
    )


def draft_readiness(workflow: Workflow, draft: Draft, registry: StepRegistry) -> DraftReadiness:
    """Whether this draft is described well enough to save, and whether this run could execute it.

    Every save path reads this one answer, so the save route, the panel and the draft checker always agree.

    Args:
        workflow: The draft's workflow, already parsed.
        draft: The draft whose card is read.
        registry: The registry of steps this run can execute.

    Returns:
        The readiness the save refuses on.
    """
    return card_readiness(workflow, prepare(workflow, registry), title=draft.title, summary=draft.summary, presentation=draft.presentation)


async def adapt_saved(
    workflow_id: str, ops: Sequence[Mapping[str, JsonValue]], *, library: WorkflowLibrary, evidence: str = "", predictions: Sequence[Prediction] = ()
) -> tuple[Draft, Lineage]:
    """Adapt a saved workflow into a child draft by typed edits, and return the lineage they produced.

    The parent is never written to. The lineage must travel with the child to the library.
    The child inherits the parent's card, the author's words and the intent.

    Args:
        workflow_id: The saved workflow to adapt.
        ops: The edits to apply, as data.
        library: Where the parent is read from. It is never written to.
        evidence: Why this version exists, in prose. It travels to the library and nothing grades it.
        predictions: What the child is claimed to show once it runs, answered later from the run evidence.

    Returns:
        The child draft and its lineage.

    Raises:
        AdaptationRefusedError: If any op names something that is not there. Every op is checked before
            any is applied, so no half-adapted workflow exists.
    """
    workflow = await library.fetch_workflow(workflow_id)
    child, lineage = adapt(workflow, ops=tuple(ops), evidence=evidence, predictions=tuple(predictions))
    card = await _card(library, workflow_id)
    return (
        Draft(
            spec_yaml=yaml.safe_dump(child.model_dump(mode="json"), sort_keys=False),
            title=card.title if card is not None else "",
            summary=card.summary if card is not None else "",
            applicability=card.applicability if card is not None else "",
            lineage=lineage,
            presentation=card.presentation if card is not None else None,
            intent=card.intent if card is not None else None,
        ),
        lineage,
    )


def run_evidence(source: SourceRun, feedback: str) -> str:
    """Why a child exists, as prose: the run behind it, what that run was observed to do, and the owner's words.

    Each fact is its own line and nothing here grades any of them.
    The outcome is quoted as the run's own checked answer, and a run with no checked outcome says so.

    Args:
        source: The run the child was adapted from.
        feedback: What the owner asked for. Blank leaves its line out.

    Returns:
        The evidence, one fact per line.
    """
    lines = [f"Adapted from run {source.run_id} of {source.workflow_id}."]
    if source.run_title:
        lines.append(f"That run was called {source.run_title!r}.")
    if source.outcome is None:
        lines.append("Observed outcome: unknown, that run predates checked outcomes.")
    else:
        lines.append(f"Observed outcome: {source.outcome.status}. {source.outcome.summary}")
        fixed = ", ".join(f"{artifact.id}@{artifact.value_digest}" for artifact in source.outcome.artifacts)
        if fixed:
            lines.append(f"Artifacts that run fixed: {fixed}")
    if feedback.strip():
        lines.append(f"What the owner asked for: {feedback.strip()}")
    return "\n".join(lines)


async def adapt_from_run(  # noqa: PLR0913 (the diff, the library, the run cited, the owner's words, the caller's own evidence and the claims are separate facts)
    workflow_id: str,
    ops: Sequence[Mapping[str, JsonValue]],
    *,
    library: WorkflowLibrary,
    source: SourceRun,
    feedback: str = "",
    evidence: str = "",
    predictions: Sequence[Prediction] = (),
) -> tuple[Draft, Lineage]:
    """Adapt a saved workflow as `adapt_saved` does, citing the run behind the change as the child's evidence.

    The child still has to validate and still has to be saved by a person.
    Neither the parent workflow nor the cited run is written to.

    Args:
        workflow_id: The saved workflow to adapt.
        ops: The edits to apply, as data.
        library: Where the parent is read from. It is never written to.
        source: The run behind the change, cited first in the evidence.
        feedback: What the owner asked for, attributed to the owner in the evidence.
        evidence: What the caller has to say about this version, kept below the run's citation.
        predictions: What the child is claimed to show once it runs, answered later from the run evidence.

    Returns:
        The child draft and its lineage.

    Raises:
        AdaptationRefusedError: If any op names something that is not there.
    """
    cited = run_evidence(source, feedback)
    said = evidence.strip()
    return await adapt_saved(
        workflow_id, ops, library=library, evidence=f"{cited}\n{said}" if said else cited, predictions=predictions
    )


async def _card(library: WorkflowLibrary, workflow_id: str) -> SavedWorkflow | None:
    """The parent's card, or `None` when the library cannot serve one."""
    fetch = getattr(library, "fetch_card", None)
    if fetch is None:
        return None
    try:
        return await fetch(workflow_id)
    except (LibraryError, KeyError, ValidationError):
        return None


async def _honest_about_what_moved(lineage: Lineage, workflow: Workflow, *, library: WorkflowLibrary) -> Lineage:
    """The lineage with `touched` widened to every node that differs from the parent, unchanged when the parent cannot be read."""
    locate, fetch = getattr(library, "locate", None), getattr(library, "fetch_workflow", None)
    if locate is None or fetch is None:
        return lineage
    try:
        parent_id = await locate(lineage.parent_name, lineage.parent_version)
        parent = await fetch(parent_id) if parent_id is not None else None
    except (LibraryError, KeyError, ValidationError):
        return lineage
    if parent is None:
        return lineage
    return lineage.model_copy(update={"touched": tuple(sorted(set(lineage.touched) | set(moved_nodes(parent, workflow))))})


async def save_draft(
    draft: Draft, *, library: WorkflowLibrary, registry: StepRegistry, self_authoring: SelfAuthoringSettings, lineage: Lineage | None = None
) -> str:
    """Validate the draft and save it to the library, or refuse with the reason and write nothing.

    A draft whose card and author's words are not filled in is refused. This code never composes them.
    The draft's intent is saved with it, complete or not.

    Args:
        draft: The draft to save.
        library: The library the workflow is saved to.
        registry: The registry of steps this run can execute.
        self_authoring: The owner's reviewed custom-item root, which the environment stamps are read from,
            and the name the save is attributed to.
        lineage: The lineage saved with the workflow. `None` uses the draft's own lineage.

    Returns:
        The library's id for the saved workflow.

    Raises:
        DraftNotSavableError: If there is no draft, it is not a workflow, it does not validate, this
            run cannot execute it, the library refuses the write, or it is not described.
    """
    lineage = lineage or draft.lineage
    if not draft.spec_yaml.strip():
        msg = "no draft to save yet"
        raise DraftNotSavableError(msg)
    try:
        workflow = Workflow.model_validate(yaml.safe_load(draft.spec_yaml))
    except (ValidationError, yaml.YAMLError) as exc:
        raise DraftNotSavableError(str(exc)[:800]) from exc
    readiness = draft_readiness(workflow, draft, registry)
    if not readiness.described:
        raise DraftNotSavableError(refusal_text(readiness), readiness)
    if lineage is not None:
        lineage = await _honest_about_what_moved(lineage, workflow, library=library)
    try:
        return await save(
            workflow,
            library,
            registry,
            title=draft.title,
            summary=draft.summary,
            applicability=draft.applicability,
            saved_by=self_authoring.saved_by,
            # Read fresh at save time, so the stamp names the capabilities promoted now.
            environment=environment_stamps(custom_root(self_authoring)),
            lineage=lineage,
            presentation=draft.presentation,
            intent=draft.intent,
        )
    except SaveRefusedError as exc:
        raise DraftNotSavableError(str(exc)) from exc
    except LibraryError as exc:
        # The library can refuse a write the validator cannot see, such as re-minting a superseded version.
        raise DraftNotSavableError(str(exc)) from exc


def ensure_session_id(runtime: Runtime) -> str:
    """The runtime's session id, minting one and recording `SessionStarted` the first time.

    Call it before recording `TurnStarted`, so the session is on the session log before its first turn.
    Later calls answer the same id and record nothing.

    Args:
        runtime: The session whose id is read or minted.

    Returns:
        The session id.
    """
    if runtime.session_id is None:
        session_id = str(uuid4())
        runtime.session_log.record(SessionStarted(session_id=session_id))
        runtime.session_id = session_id
    return runtime.session_id


@dataclass(frozen=True)
class RescuedTurn:
    """The turn a rescue recovers, gathered as one thing the rescue is handed."""

    session_id: str
    """The conversation the turn ran in."""
    prompt: str
    """What the person asked, kept when there is nothing of the turn's own to rescue."""
    before_run_id: str | None
    """This session's newest run before the agent was asked to make one."""
    ended: str
    """How the turn ended, in the words the rescued history carries."""


async def rescue_turn(session: Runtime, turn: RescuedTurn, *, refresh: SessionRefresh | None) -> None:
    """Recover what this session's failed or cut-off turn did onto its history, from its own transcript.

    When the turn registered its own run, the history becomes that run's snapshot, open tool calls included.
    Otherwise, or when that snapshot cannot be read, the person's prompt is appended to the history.
    It may be called from inside the cancellation that ended the turn, and the history is written before
    that cancellation lands. Either way, `refresh` then brings the projection up to date.

    Args:
        session: The session the turn ran in.
        turn: The turn to recover.
        refresh: How the session's projected rows are brought up to date. `None` refreshes nothing.
    """
    messages: list[ModelMessage] | None = None
    with anyio.CancelScope(shield=True):
        run_id = await newest_run_id(session, conversation_id=turn.session_id)
        if run_id is not None and run_id != turn.before_run_id:
            try:
                messages = await continue_run(session.store, run_id=run_id, include_interrupted=True)
            except Exception as exc:  # noqa: BLE001 (a snapshot that cannot be read for any reason falls back to the prompt)
                _logger.warning("could not read this turn's own snapshot for rescue: {reason}", reason=str(exc))
                messages = None
    if messages is not None:
        session.conversation.history = rescue_history(messages, ended=turn.ended)
    else:
        session.conversation.history = [*session.conversation.history, ModelRequest(parts=[UserPromptPart(turn.prompt)])]
    refresh_projection(session, refresh)


def plain_error(text: str) -> str:
    """One plain sentence for the chat bubble, made from an error's raw text.

    It drops the provider's JSON body. Record the raw text in the session log, not this sentence.

    Args:
        text: The raw error text, a status line followed by the provider's body.

    Returns:
        The sentence, at most 200 characters.
    """
    lowered = text.lower()
    if "insufficient balance" in lowered or "402" in lowered.split("body:")[0]:
        return "The model provider refused the request, its account has no balance left. Top up the provider or switch the model in the harness station."
    first = text.split("body:", maxsplit=1)[0].splitlines()[0].strip().rstrip(",")
    return first[:200] if first else "The turn failed."


def deferred_names(requests: DeferredToolRequests) -> tuple[str, ...]:
    """The tools a deferred run waits on, the browser's verbs first, then the acts awaiting approval.

    Args:
        requests: The deferred run's output.

    Returns:
        The tool names, in that order.
    """
    return tuple(part.tool_name for part in (*requests.calls, *requests.approvals))


def settle_deferred(session: Runtime, result: AgentRunResult[str | DeferredToolRequests], requests: DeferredToolRequests) -> tuple[str, ...]:
    """Fold a run that stopped for the browser into the conversation, keeping its messages and nothing about the draft.

    The pending calls stay at the tail of the history, where the continuation's results answer them.
    The draft, the draft file on disk and the panel payload stay as the previous settle left them.

    Args:
        session: The session the run belongs to.
        result: The run that stopped.
        requests: The run's output, already narrowed to its deferred requests.

    Returns:
        The names of the tools the run waits on.
    """
    session.conversation.history = list(result.all_messages())
    return deferred_names(requests)


def draft_payload(session: Runtime) -> dict[str, JsonValue]:
    """The draft on screen, derived on the spot: what it is, whether it holds, and its graph.

    Every field describes the draft being shown, never the checker's last verdict.
    `issues` is what this run cannot execute, and `readiness` is what the card still owes, read as the save reads it.
    `intent_issues` names what the person has not yet answered about their intent.

    Args:
        session: The session whose draft is shown.

    Returns:
        The payload. `graph` and `readiness` are `None` while the draft does not parse as a workflow,
        and `intent_issues` is empty while no intent is recorded.
    """
    draft = session.conversation.draft
    workflow, issues = verdict_on(draft.spec_yaml, session.registry)
    return {
        "draft": draft.model_dump(mode="json"),
        "issues": [*issues],
        "valid": workflow is not None and not issues,
        "graph": graph_payload(workflow, prepare(workflow, session.registry).registry, {}) if workflow is not None else None,
        # `null` while nothing parses, which is a weaker statement than a card that reads as not described.
        "readiness": draft_readiness(workflow, draft, session.registry).model_dump(mode="json") if workflow is not None else None,
        "intent_issues": list(intent_issues(draft.intent)) if draft.intent is not None else [],
    }


def record_intent(session: Runtime, intent: WorkflowIntent) -> Draft:
    """Put `intent` on this session's draft, replacing whatever it recorded before, and answer the draft.

    The intent lands whole, never field by field. Nothing else on the draft changes and the workflow is not re-read.

    Args:
        session: The session whose draft changes.
        intent: What the person asked for.

    Returns:
        The changed draft.
    """
    draft = session.conversation.draft.model_copy(update={"intent": intent})
    session.conversation.draft = draft
    return draft


def reply_payload(session: Runtime, message: str) -> dict[str, JsonValue]:
    """The one payload every chat answer carries, successful or not: the message beside the draft on screen.

    Args:
        session: The session whose draft is shown.
        message: The answer's text.

    Returns:
        `message` beside the fields of `draft_payload`.
    """
    return {"message": message, **draft_payload(session)}


def settle_turn(session: Runtime, result: AgentRunResult[str | DeferredToolRequests], *, draft_file: Path) -> dict[str, JsonValue]:
    """Fold a finished run into conversation state and the one JSON shape every entry answers with.

    Every entry settles a turn here, so the draft, the draft file on disk and the panel's payload agree.
    The reply text is the message, verbatim. The draft is whatever `check_draft` settled mid-turn,
    written to `draft_file` when the checker's last verdict passed.

    Args:
        session: The session the run belongs to.
        result: The finished run.
        draft_file: Where a checked draft is kept on disk.

    Returns:
        The reply payload.

    Raises:
        TypeError: If the run stopped for the browser. `settle_deferred` folds that one.
    """
    session.conversation.history = list(result.all_messages())
    reply = result.output
    if isinstance(reply, DeferredToolRequests):
        msg = "a deferred run is settled by settle_deferred, never as a reply"
        raise TypeError(msg)
    checked = session.conversation.last_workflow if not session.conversation.last_issues else None
    if checked is not None:
        remember_draft(session.conversation.draft, path=draft_file)
        session.session_log.record(DraftSettled(name=checked.name, version=checked.version))
    return reply_payload(session, reply)


@dataclass
class _Ending:
    """How one turn ended: what it settled, and whether the session log already carries that ending."""

    payload: Mapping[str, JsonValue] | None = None
    """What the panel reads, filled when the run ends and read when the stream asks for it."""
    recorded: bool = False
    """Whether the settle or the failure is already on the session log, so a close does not record a second."""


@dataclass(frozen=True)
class _Asked:
    """What one turn was asked, once the refusals before it are past."""

    history: Sequence[ModelMessage]
    """What the model last saw in this conversation, read from the record."""
    text: str
    """What the person typed. Empty is a continuation rather than a turn of its own."""
    results: DeferredToolResults | None
    """The browser's answers to the calls the previous turn stopped for, checked against them."""
    context: PageContext | None
    """The page the person is looking at, folded into the prompt."""


class ChatService:
    """One turn of one session: the hold that keeps it alone, the run, and the settle it records.

    The agent writes the conversation record itself. This service decides which session answers, what it is told
    and what the browser already answered, and records the turn's ending on the session log and in the projection.
    """

    def __init__(self, pool: SessionPool, history: ConversationHistory, refresh: SessionRefresh) -> None:
        """Keep the pool, the conversation record and the projection refresh.

        Args:
            pool: The sessions this process keeps open.
            history: The conversation record a rebuilt session starts from.
            refresh: How a settled turn brings its session's projected rows up to date.
        """
        self._pool = pool
        self._history = history
        self._refresh = refresh

    async def send_message(
        self,
        session_id: str,
        messages: Sequence[ModelMessage],
        *,
        context: PageContext | None = None,
        tool_results: Sequence[ToolResultIn] = (),
        design_only: bool = False,
    ) -> Turn:
        """Hold the session, load what the model last saw, and answer with the turn.

        Every refusal is raised before anything is recorded. The session stays held until the turn's
        events end or are closed.

        Args:
            session_id: The session the turn runs in.
            messages: This request's own messages only.
            context: The page the person is looking at, folded into the prompt.
            tool_results: The browser's answers to the calls the previous turn stopped for.
            design_only: Whether this turn is offered no verb that starts a run or changes a standing
                schedule. It applies to this turn only.

        Returns:
            The turn in flight.

        Raises:
            SessionUnknownError: No session has that id.
            SessionBusyError: A turn is already in flight in that session.
            PartAnsweredError: The request answers only some of the calls the previous turn stopped for.
            TurnEmptyError: The request says nothing and answers nothing.
        """
        try:
            hold = await self._pool.begin_turn(session_id)
        except KeyError as error:
            raise SessionUnknownError(session_id) from error
        hold.runtime.design_only = design_only
        try:
            asked = await self._asked(hold.runtime, session_id, messages, tool_results, context)
        except Exception:
            hold.release()
            raise
        readings: list[StepUsage] = []
        ending = _Ending()
        events = self._turn(hold, asked, readings=readings, ending=ending)

        async def settle() -> Mapping[str, JsonValue] | None:
            return ending.payload

        return Turn(events=_TurnEvents(events, hold), usage=readings, settle=settle)

    async def _asked(
        self,
        session: Runtime,
        session_id: str,
        messages: Sequence[ModelMessage],
        tool_results: Sequence[ToolResultIn],
        context: PageContext | None,
    ) -> _Asked:
        """This turn's own facts, with every refusal made before a word of it is recorded."""
        text = _asked_text(messages)
        history = await self._history_for(session, session_id, resuming=bool(tool_results))
        results = self._answers(history, text, tool_results)
        if not text.strip() and not tool_results:
            raise TurnEmptyError(EMPTY_TURN)
        return _Asked(history=history, text=text, results=results, context=context)

    async def _history_for(self, session: Runtime, session_id: str, *, resuming: bool) -> list[ModelMessage]:
        """What this turn is given before its own message: the session's live conversation, or the record when a fresh turn finds it empty."""
        live = list(session.conversation.history)
        if resuming or live:
            return live
        return await self._history.history_for_next_run(conversation_id=session_id)

    def _answers(self, history: Sequence[ModelMessage], asked: str, tool_results: Sequence[ToolResultIn]) -> DeferredToolResults | None:
        """The browser's answers to what the last turn stopped for, with a typed message superseding every call still waiting."""
        pending = pending_calls(history)
        results = deferred_results(tool_results, pending, verbs=VERBS, console_acts=CONSOLE_ACTS)
        if pending and asked.strip():
            results = denying_unanswered(results, pending, verbs=VERBS, console_acts=CONSOLE_ACTS, reason=SUPERSEDED)
        if pending and (results is None or not answers_every_call(results, pending)):
            raise PartAnsweredError(PART_ANSWERED)
        return results

    async def _turn(self, hold: TurnHold, asked: _Asked, *, readings: list[StepUsage], ending: _Ending) -> AsyncGenerator[NativeEvent]:
        """The run, with its ending recorded on this session's log and the session released however it ends."""
        session = hold.runtime
        session_id = ensure_session_id(session)
        # A steer a dead turn never picked up must not surface inside this unrelated one.
        session.steer_inbox.clear()
        session.live_prompts = [asked.text] if asked.text.strip() else []
        started = session.session_log.record(
            TurnStarted(entry="spa", chars=len(asked.text), session_id=session_id, prompt_excerpt=asked.text[:300] or None)
        )
        composed = compose_prompt(asked.text, asked.context, session_kind=session.session_kind)
        # Only the emptiness is judged on the trimmed text. What the person typed is sent as they typed
        # it, so a pasted block keeps the indentation it was pasted with.
        prompt = composed if composed.strip() else CONTINUE_PROMPT
        # Read before the run starts. The rescue compares against it to tell whether this turn registered a run.
        before_run_id = await newest_run_id(session, conversation_id=session_id)
        model = session.agent.model
        try:
            with bind_workspace_root(workspace_root(session)), answerable():
                async with session.agent.run_stream_events(
                    prompt,
                    deps=session,
                    message_history=list(asked.history),
                    deferred_tool_results=asked.results,
                    conversation_id=session_id,
                    usage_limits=UsageLimits(request_limit=CHAT_BUDGET),
                    capabilities=list(session.custom_capabilities.load_active()),
                    # `None` when the agent's own model is not a `Model` instance, which runs that model unchanged.
                    model=StepUsageTap(model, readings) if isinstance(model, Model) else None,
                ) as events:
                    async for event in events:
                        if isinstance(event, AgentRunResultEvent):
                            ending.payload = await self._settle(session, session_id, event.result, started)
                            ending.recorded = True
                        yield event
        except (UsageLimitExceeded, UnexpectedModelBehavior, ModelHTTPError) as exc:
            error = str(exc)[:300]
            session.session_log.record(
                TurnFailed(error=error, started_seq=started.seq if started is not None else None, prompt_excerpt=asked.text[:300] or None)
            )
            ending.recorded = True
            await self._rescue(session, session_id, prompt, before_run_id, ended=TURN_FAILED.format(error=error))
            raise PlainTurnError(plain_error(str(exc))) from exc
        finally:
            if not ending.recorded and started is not None:
                session.session_log.record(TurnAbandoned(started_seq=started.seq))
                await self._rescue(session, session_id, prompt, before_run_id, ended=TURN_CUT_OFF)
            # After the rescue, never before: the session is handed to the next turn only once this
            # one has finished writing its own history onto it.
            hold.release()

    async def _settle(
        self, session: Runtime, session_id: str, result: AgentRunResult[str | DeferredToolRequests], started: Recorded | None
    ) -> Mapping[str, JsonValue] | None:
        """Fold the finished run into the session and record the settle, answering `None` for a run that stopped for the browser."""
        started_seq = started.seq if started is not None else None
        output = result.output
        if isinstance(output, DeferredToolRequests):
            pending = settle_deferred(session, result, output)
            run_id = await newest_run_id(session, conversation_id=session_id)
            session.session_log.record(TurnSettled(run_id=run_id, started_seq=started_seq, pending=pending))
            refresh_projection(session, self._refresh)
            return None
        payload = settle_turn(session, result, draft_file=draft_path(session))
        run_id = await newest_run_id(session, conversation_id=session_id)
        session.session_log.record(TurnSettled(run_id=run_id, started_seq=started_seq))
        refresh_projection(session, self._refresh)
        return payload

    async def _rescue(self, session: Runtime, session_id: str, prompt: str, before_run_id: str | None, *, ended: str) -> None:
        """Recover this turn's own history onto the session, and refresh what the panel reads."""
        await rescue_turn(
            session, RescuedTurn(session_id=session_id, prompt=prompt, before_run_id=before_run_id, ended=ended), refresh=self._refresh
        )


class _TurnEvents:
    """One turn's events, with the session given back however the stream ends, even when it is never drawn."""

    def __init__(self, events: AsyncGenerator[NativeEvent], hold: TurnHold) -> None:
        self._events = events
        self._hold = hold
        self._unstarted = weakref.finalize(self, hold.release)
        self._unstarted.atexit = False

    def __aiter__(self) -> AsyncIterator[NativeEvent]:
        return self

    async def __anext__(self) -> NativeEvent:
        """The turn's next event, the hold handed over to the stream itself before asking for it."""
        if self._unstarted.detach() is not None:
            self._hold.driven_by(asyncio.current_task())
        try:
            return await self._events.__anext__()
        except BaseException:
            self._hold.release()
            raise

    async def aclose(self) -> None:
        """Close the turn early, which ends its run and gives the session back."""
        if self._unstarted.detach() is not None:
            self._hold.release()
            return
        await self._events.aclose()


def _asked_text(messages: Sequence[ModelMessage]) -> str:
    """What the person typed in this request: every text part of its newest user message with text, joined by newlines."""
    for message in reversed(list(messages)):
        typed = [piece for part in message.parts if isinstance(part, UserPromptPart) for piece in _typed_pieces(part.content)]
        if typed:
            return "\n".join(typed)
    return ""


def _typed_pieces(content: str | Sequence[UserContent]) -> list[str]:
    """The text of one user prompt part: the whole of it, or every string among its pieces."""
    if isinstance(content, str):
        return [content] if content else []
    return [piece for piece in content if isinstance(piece, str)]
