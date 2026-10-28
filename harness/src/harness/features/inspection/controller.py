"""The inspection read back over HTTP: what was found, what the inspector saw, and what one run cost it.

Every position is a span written `step.message.part`. A route renders one into the text of the part it points at,
so a reader never has to open the run themselves. A recorded conversation crosses the wire in the same five fields
a session's transcript does, so the same components draw it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from core.ai.inspection.contracts import (
    ATTRIBUTE_LEAD_KIND,
    INSPECT_RUN_KIND,
    SCAN_RUN_KIND,
    InspectionQueries,
    Taxonomy,
    Watch,
    inspect_run_key,
    label_of,
    outline_steps,
    plain_whys,
    scan_run_key,
    shown,
    step_at,
    text_of,
    turns_for,
)
from core.ai.transcripts.contracts import UNREADABLE, RunFacts, TranscriptLoaders, record_source, unreadable_reason
from core.cqrs.contracts import JobQueries
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse, ToolReturnPart
from wireup.integration.fastapi import WireupRoute

from harness.domain.contracts import ConversationMessages
from harness.features.inspection.api_models import (
    AskIn,
    BlameView,
    ConversationMessageView,
    EvidenceView,
    FindingDetail,
    FindingView,
    JobView,
    LeadView,
    QuestionView,
    RunOutline,
    RunOverview,
    StepOutlineView,
    TurnPage,
    TurnView,
)
from harness.features.inspection.internal_contracts import FollowUpAnswerer

if TYPE_CHECKING:
    from core.ai.inspection.contracts import StoredBlame, StoredFinding, StoredLead, StoredQuestion
    from core.ai.transcripts.contracts import RunEmptyError, Span, Transcript
    from core.cqrs.contracts import Job
    from pydantic_ai.messages import ModelMessage, ModelRequestPart, ModelResponsePart

JOBS_PER_KEY: Final = 5
"""How many attempts of one command a run's overview carries, newest first."""

LEADS_WITH_JOBS: Final = 50
"""How many of a run's leads the overview looks up the inspector's own commands for."""

MAX_FINDINGS: Final = 200
"""The most findings one listing answers, whatever the caller asked for."""


def _span_text(span: Span) -> str:
    """One position as the inspector writes it."""
    return f"{span.step}.{span.message}.{span.part}"


def _blame_views(blames: Sequence[StoredBlame], taxonomy: Taxonomy) -> list[BlameView]:
    """The stored blames as views, each field by name, with the taxonomy's sentence for what is wrong."""
    return [
        BlameView(
            root_cause=blame.root_cause,
            root_cause_text=_root_cause_text(blame.root_cause, taxonomy),
            surface_kind=blame.surface_kind,
            surface_name=blame.surface_name,
            why=blame.why,
        )
        for blame in blames
    ]


def _root_cause_text(name: str, taxonomy: Taxonomy) -> str:
    """The root cause's one sentence, empty when the taxonomy no longer holds a root cause by that name."""
    try:
        return taxonomy.root_cause(name).meaning
    except KeyError:
        return ""


def _evidence_kind(part: ModelRequestPart | ModelResponsePart) -> str:
    """The kind an evidence span is served with: `retried-return` for a tool return asking for its call again, else the part's kind."""
    return "retried-return" if isinstance(part, ToolReturnPart) and part.outcome == "retried" else part.part_kind


def _symptom_text(name: str, taxonomy: Taxonomy) -> str:
    """The symptom's one sentence as it appears in a transcript, empty when the taxonomy no longer holds it."""
    try:
        return taxonomy.symptom(name).what_you_see
    except KeyError:
        return ""


def _finding_view(found: StoredFinding, lead: StoredLead | None, taxonomy: Taxonomy) -> FindingView:
    """One finding as a row, carrying the detector of the lead under it when there is one."""
    return FindingView(
        finding_id=found.finding_id,
        lead_id=found.lead_id,
        question_id=found.question_id,
        source=found.source,
        symptom=found.symptom,
        symptom_text=_symptom_text(found.symptom, taxonomy),
        found_by=found.found_by,
        model=found.model,
        decided_at=found.decided_at,
        blames=_blame_views(found.blames, taxonomy),
        lead_found_by=None if lead is None else lead.found_by,
        explanation=found.explanation or plain_whys([blame.why for blame in found.blames]),
    )


def _lead_view(lead: StoredLead) -> LeadView:
    """One lead as a row, its spans written out."""
    return LeadView(
        lead_id=lead.lead_id,
        source=lead.source,
        watch=lead.watch,
        symptom=lead.symptom,
        spans=[_span_text(span) for span in lead.spans],
        found_by=lead.found_by,
        seen_at=lead.seen_at,
        attempted_at=lead.attempted_at,
        refusal=lead.refusal,
        resolution=lead.resolution,
    )


def _question_view(asked: StoredQuestion) -> QuestionView:
    """One question as a row."""
    return QuestionView(
        question_id=asked.question_id,
        conversation_id=str(asked.conversation_id),
        kind=asked.kind,
        source=asked.source,
        watch=asked.watch,
        lead_id=asked.lead_id,
        tier=asked.tier,
        model=asked.model,
        asked_at=asked.asked_at,
        answer=asked.answer,
    )


def _job_view(job: Job) -> JobView:
    """One command as a row: what it was, how it went, what it left behind."""
    return JobView(
        id=job.id,
        kind=job.kind,
        key=job.key,
        state=job.state,
        attempts=job.attempts,
        result=job.result,
        last_error=job.last_error,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


def _model_of(transcript: Transcript) -> str | None:
    """The model the run's last recorded request went to, or None when no step recorded one."""
    return next((step.model for step in reversed(transcript.steps) if step.model is not None), None)


def conversation_views(messages: Sequence[ModelMessage]) -> list[ConversationMessageView]:
    """Every message of a recorded conversation as the SPA's chat reads one.

    Args:
        messages: The conversation's messages, in order.

    Returns:
        One view per message, in the same order, each indexed from zero.
    """
    payloads = ModelMessagesTypeAdapter.dump_python(list(messages), mode="json")
    rendered: list[ConversationMessageView] = []
    for idx, (message, payload) in enumerate(zip(messages, payloads, strict=True)):
        said = [text for text in (text_of(part) for part in message.parts) if text]
        rendered.append(
            ConversationMessageView(
                run_id=message.run_id or "",
                idx=idx,
                kind="response" if isinstance(message, ModelResponse) else "request",
                payload=payload,
                text="\n".join(said),
            )
        )
    return rendered


def _not_here(what: str) -> JSONResponse:
    """The one refusal every id-scoped route of this slice gives."""
    return JSONResponse({"ok": False, "message": f"no {what} with that id here"}, status_code=404)


def _unreadable(source: str, error: RunEmptyError | LookupError | FileNotFoundError | ValueError) -> JSONResponse:
    """The one refusal a route gives when the run at `source` cannot be opened."""
    return JSONResponse({"ok": False, "message": f"{source}: {unreadable_reason(error)}"}, status_code=404)


class InspectionController:
    """The inspection's own reads: findings, one finding with its evidence, one question, one run."""

    router = APIRouter(prefix="/api/inspection", tags=["inspection"], route_class=WireupRoute)

    def __init__(
        self,
        store: InspectionQueries,
        taxonomy: Taxonomy,
        loaders: TranscriptLoaders,
        messages: ConversationMessages,
        jobs: JobQueries,
        watches: Sequence[Watch],
        follow_up: FollowUpAnswerer,
        facts: RunFacts,
    ) -> None:
        """Hold what every route reads.

        Args:
            store: The inspection's tables, read only.
            taxonomy: The taxonomy the findings' sentences come from.
            loaders: How a source opens.
            messages: The record's conversations.
            jobs: The reads over the commands.
            watches: The watches a run's overview looks the inspector's commands up under.
            follow_up: What answers one more question to the inspector.
            facts: One agent run's own facts in the record.
        """
        self._store = store
        self._taxonomy = taxonomy
        self._loaders = loaders
        self._messages = messages
        self._jobs = jobs
        self._watches = watches
        self._follow_up = follow_up
        self._facts = facts

    @router.get("/findings")
    async def list_findings(
        self, *, symptom: str | None = None, source: str | None = None, found_by: str | None = None, limit: int = 50
    ) -> JSONResponse:
        """Every finding matching the filters given, newest first. An absent filter matches everything."""
        rows = await self._store.findings_newest(symptom=symptom, source=source, found_by=found_by, limit=min(max(limit, 1), MAX_FINDINGS))
        listed = [_finding_view(row, await self._lead_of(row), self._taxonomy) for row in rows]
        return JSONResponse({"findings": [view.model_dump(mode="json") for view in listed]})

    @router.get("/findings/{finding_id}")
    async def finding_detail(self, finding_id: int) -> JSONResponse:
        """One finding, the lead under it, and every span rendered as the text the detector pointed at."""
        found = await self._store.finding(finding_id)
        if found is None:
            return _not_here("finding")
        lead = await self._lead_of(found)
        detail = FindingDetail(
            finding=_finding_view(found, lead, self._taxonomy),
            lead=None if lead is None else _lead_view(lead),
            evidence=await self._evidence(found.source, found.spans),
        )
        return JSONResponse(detail.model_dump(mode="json"))

    @router.get("/findings/{finding_id}/turn")
    async def finding_turn(self, finding_id: int) -> JSONResponse:
        """The turn under investigation: for each step the finding cites, its messages as the chat draws them, and which ones hold a cited part."""
        found = await self._store.finding(finding_id)
        if found is None:
            return _not_here("finding")
        try:
            transcript = await self._loaders.load(found.source)
        except UNREADABLE as error:
            return _unreadable(found.source, error)
        page = TurnPage(
            turns=[
                TurnView(step=turn.step, messages=conversation_views(turn.messages), cited=list(turn.cited), first=turn.first)
                for turn in turns_for(transcript, found.spans)
            ]
        )
        return JSONResponse(page.model_dump(mode="json"))

    @router.get("/questions/{question_id}")
    async def question_detail(self, question_id: int) -> JSONResponse:
        """One question, and the conversation it was answered in: the view, every tool call and return, the answer."""
        asked = await self._store.question(question_id)
        if asked is None:
            return _not_here("question")
        recorded = await self._messages.conversation_messages(conversation_id=str(asked.conversation_id))
        return JSONResponse(
            {
                "question": _question_view(asked).model_dump(mode="json"),
                "messages": [view.model_dump(mode="json") for view in conversation_views(recorded)],
            }
        )

    @router.post("/questions/{question_id}/ask")
    async def ask_the_inspector(self, question_id: int, body: AskIn) -> JSONResponse:
        """Continue one question's conversation: the same tier, the same tools, the same run, one more answer.

        The answer is recorded under that same conversation, so asking again sees this exchange too.
        """
        asked = await self._store.question(question_id)
        if asked is None:
            return _not_here("question")
        try:
            answered = await self._follow_up.answer(asked, body.text)
        except UNREADABLE as error:
            return _unreadable(asked.source, error)
        return JSONResponse({"answer": answered.answer, "conversation_id": str(answered.conversation_id), "model": answered.model})

    @router.get("/runs/{run_id}")
    async def run_overview(self, run_id: str) -> JSONResponse:
        """Everything the inspection did to one run: its leads, its findings, its questions, its commands."""
        source = record_source(run_id)
        leads = await self._store.leads_of(source)
        findings = await self._store.findings_of(source)
        questions = await self._store.questions_of(source)
        by_lead = {lead.lead_id: lead for lead in leads}
        overview = RunOverview(
            source=source,
            leads=[_lead_view(lead) for lead in leads],
            findings=[
                _finding_view(row, by_lead.get(row.lead_id) if row.lead_id is not None else None, self._taxonomy) for row in findings
            ],
            questions=[_question_view(row) for row in questions],
            jobs=await self._jobs_of(source, [lead.lead_id for lead in leads]),
        )
        return JSONResponse(overview.model_dump(mode="json"))

    @router.get("/runs/{run_id}/outline")
    async def run_outline(self, run_id: str) -> JSONResponse:
        """The run in one screen: one entry per step with how many messages it added and what happened, and the record's facts about it."""
        source = record_source(run_id)
        try:
            transcript = await self._loaders.load(source)
        except UNREADABLE as error:
            return _unreadable(source, error)
        summary = await self._facts.summary_of(source)
        view = RunOutline(
            source=source,
            agent=summary.agent if summary is not None else None,
            started_at=summary.started_at if summary is not None else None,
            ended_at=summary.ended_at if summary is not None else None,
            ended=summary.last_event_type if summary is not None else None,
            model=_model_of(transcript),
            tool_calls=len(transcript.calls()),
            steps=[
                StepOutlineView(step=line.step, messages=line.messages, summary=", ".join(line.pieces) or "nothing new")
                for line in outline_steps(transcript)
            ],
        )
        return JSONResponse(view.model_dump(mode="json"))

    @router.get("/runs/{run_id}/steps/{step}")
    async def run_step(self, run_id: str, step: int) -> JSONResponse:
        """The messages the run added at one step, as the chat draws them, none cited."""
        source = record_source(run_id)
        try:
            transcript = await self._loaders.load(source)
        except UNREADABLE as error:
            return _unreadable(source, error)
        try:
            turn = step_at(transcript, step)
        except ValueError as error:
            return JSONResponse({"ok": False, "message": str(error)}, status_code=404)
        view = TurnView(step=turn.step, messages=conversation_views(turn.messages), cited=[], first=turn.first)
        return JSONResponse(view.model_dump(mode="json"))

    async def _lead_of(self, found: StoredFinding) -> StoredLead | None:
        """The lead a finding settled, or None when it settled none."""
        return None if found.lead_id is None else await self._store.lead(found.lead_id)

    async def _evidence(self, source: str, spans: Sequence[Span]) -> list[EvidenceView]:
        """Every span with the part it points at rendered for a person, or with why that part could not be read."""
        try:
            transcript = await self._loaders.load(source)
        except UNREADABLE as error:
            words = unreadable_reason(error)
            return [EvidenceView(span=_span_text(span), label=f"step {span.step}", kind="unreadable", text=words) for span in spans]
        rendered: list[EvidenceView] = []
        for span in spans:
            try:
                part = transcript.part_at(span)
            except IndexError:
                rendered.append(
                    EvidenceView(span=_span_text(span), label=f"step {span.step}", kind="unreadable", text="this span is not in the run")
                )
            else:
                rendered.append(EvidenceView(span=_span_text(span), label=label_of(span, part), kind=_evidence_kind(part), text=shown(part)))
        return rendered

    async def _jobs_of(self, source: str, lead_ids: Sequence[int]) -> list[JobView]:
        """Every command the inspection issued about this run: both run lanes per watch, and the newest leads' lane."""
        found: list[JobView] = []
        for watch in self._watches:
            for job_type, key in ((INSPECT_RUN_KIND, inspect_run_key(watch.name, source)), (SCAN_RUN_KIND, scan_run_key(watch.name, source))):
                found.extend(_job_view(job) for job in await self._jobs.recent_jobs(job_type, key, limit=JOBS_PER_KEY))
        for lead_id in lead_ids[-LEADS_WITH_JOBS:]:
            found.extend(_job_view(job) for job in await self._jobs.recent_jobs(ATTRIBUTE_LEAD_KIND, str(lead_id), limit=JOBS_PER_KEY))
        return sorted(found, key=lambda job: job.id)
