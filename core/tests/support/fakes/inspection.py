"""Builders for the transcripts the rule tests read, and fakes for what the InspectRun handler talks to."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryInfo,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator

from core.ai.history.infrastructure.reads import TERMINAL_KINDS
from core.ai.inspection.detectors import Inspected, InspectionContext
from core.ai.inspection.dispatch import DISPATCH_KINDS
from core.ai.inspection.domain.taxonomy import TAXONOMY
from core.ai.inspection.read_models import AttemptResolution, StoredBlame, StoredFinding, StoredLead, StoredQuestion
from core.ai.transcripts.contracts import Step, Transcript
from core.cqrs.contracts import Enqueued

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from uuid import UUID

    from pydantic_ai.messages import ToolRetryCause

    from core.ai.history.contracts import EventAtPositionRow
    from core.ai.inspection.domain.leads import Lead
    from core.ai.inspection.domain.taxonomy import Finding
    from core.ai.inspection.read_models import QuestionType
    from core.ai.transcripts.contracts import RunSummary
    from core.ai.workflows.contracts import Tier

AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

type Outcome = Literal["success", "failed", "denied", "interrupted"]


def question(text: str = "What is the weather in Paris?") -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text, timestamp=AT)])


def call(tool_call_id: str, tool_name: str = "lookup_city", args: Mapping[str, object] | None = None) -> ModelResponse:
    return ModelResponse(
        parts=[ToolCallPart(tool_name=tool_name, args=dict(args or {"name": "Paris"}), tool_call_id=tool_call_id)], timestamp=AT
    )


def result(
    tool_call_id: str, content: str = "sunny and mild", *, outcome: Outcome = "success", tool_name: str = "lookup_city"
) -> ModelRequest:
    return ModelRequest(
        parts=[ToolReturnPart(tool_name=tool_name, content=content, tool_call_id=tool_call_id, timestamp=AT, outcome=outcome)]
    )


def retried(
    tool_call_id: str,
    content: str = "name must be a string",
    *,
    cause: ToolRetryCause | None = "validation_error",
    tool_name: str = "lookup_city",
) -> ModelRequest:
    """The call's answer asking for it again: the schema rejected it by default, `cause="model_retry"` when the tool refused it."""
    return ModelRequest(
        parts=[
            ToolReturnPart(
                tool_name=tool_name,
                content=content,
                tool_call_id=tool_call_id,
                timestamp=AT,
                outcome="retried",
                retry=None if cause is None else RetryInfo(cause=cause),
            )
        ]
    )


def answer(text: str = "It is sunny in Paris.", *, finish_reason: Literal["stop", "length"] = "stop") -> ModelResponse:
    return ModelResponse(parts=[TextPart(content=text)], timestamp=AT, finish_reason=finish_reason)


def transcript(*messages: ModelMessage, source: str = "test:run", durations_ms: Mapping[str, int] | None = None) -> Transcript:
    """One step holding `messages` in order."""
    return Transcript(
        source=source, steps=(Step(messages=messages, ordinals=tuple(range(len(messages)))),), durations_ms=dict(durations_ms or {})
    )


class Opens:
    """A loader that answers with the transcripts it was given, by locator."""

    def __init__(self, **by_locator: Transcript) -> None:
        self.by_locator = by_locator
        self.opened: list[str] = []

    async def __call__(self, locator: str) -> Transcript:
        self.opened.append(locator)
        try:
            return self.by_locator[locator]
        except KeyError as exc:
            msg = f"no run {locator!r}"
            raise LookupError(msg) from exc


async def leads_of(rule: Evaluator[str, Inspected, None], given: Transcript) -> list[Lead]:
    """Run one rule over one transcript the way an inspection does, holding every lead to its symptom's part kind."""

    async def load(source: str) -> Inspected:
        return Inspected(transcript=given)

    dataset = Dataset[str, Inspected, None](name="test", cases=[Case(name=given.source, inputs=given.source)], evaluators=[rule])
    report = await dataset.evaluate(load, progress=False)
    (case,) = report.cases
    assert case.evaluator_failures == []
    assert case.output is not None
    for found in case.output.leads:
        shows_in = TAXONOMY.symptom(found.symptom).shows_in
        for span in found.spans:
            kind = given.part_at(span).part_kind
            assert kind == shows_in, f"{found.found_by} led at {span} on a {kind} part, but {found.symptom} shows in a {shows_in}"
    return case.output.leads


@dataclass
class RuleThatRaises(Evaluator[str, Inspected, None]):
    """A rule that raises instead of reading the transcript."""

    message: str = "boom"

    def evaluate(self, ctx: InspectionContext) -> dict[str, int]:
        """Never answers: it raises what it was built with."""
        raise RuntimeError(self.message)


# fakes for the InspectRun handler


@dataclass
class FakeLeadsAndFindings:
    """Remembers every lead and finding it was given, in order, with ids counted from 1."""

    leads: list[StoredLead] = field(default_factory=list)
    findings: list[StoredFinding] = field(default_factory=list)
    refusals: dict[int, str] = field(default_factory=dict)
    questions: list[StoredQuestion] = field(default_factory=list)

    async def add_leads(self, watch: str, leads: Sequence[Lead]) -> list[int]:
        ids: list[int] = []
        for found in leads:
            existing = next(
                (
                    row
                    for row in self.leads
                    if row.source == found.source and row.symptom == found.symptom and row.spans_digest == found.spans_digest
                ),
                None,
            )
            if existing is not None:
                ids.append(existing.lead_id)
                continue
            row = StoredLead(
                lead_id=len(self.leads) + 1,
                source=found.source,
                watch=watch,
                symptom=found.symptom,
                spans=list(found.spans),
                spans_digest=found.spans_digest,
                found_by=found.found_by,
                seen_at=AT,
            )
            self.leads.append(row)
            ids.append(row.lead_id)
        return ids

    async def add_finding(
        self, finding: Finding, *, source: str, lead_id: int | None, found_by: str, model: str | None, question_id: int | None = None
    ) -> int | None:
        if lead_id is not None:
            if any(row.lead_id == lead_id and row.resolution == "dismissed" for row in self.leads):
                return None
            settled = next((row for row in self.findings if row.lead_id == lead_id), None)
            if settled is not None:
                return settled.finding_id
        row = StoredFinding(
            finding_id=len(self.findings) + 1,
            lead_id=lead_id,
            source=source,
            symptom=finding.symptom.name,
            spans=list(finding.evidence),
            blames=[
                StoredBlame(root_cause=blame.what.name, surface_kind=blame.where.kind.value, surface_name=blame.where.name, why=blame.why)
                for blame in finding.blames
            ],
            found_by=found_by,
            model=model,
            decided_at=AT,
            question_id=question_id,
            explanation=finding.explanation,
        )
        self.findings.append(row)
        return row.finding_id

    async def add_question(
        self, *, kind: QuestionType, conversation_id: UUID, source: str, watch: str, lead_id: int | None, tier: Tier, model: str
    ) -> int:
        row = StoredQuestion(
            question_id=len(self.questions) + 1,
            conversation_id=conversation_id,
            kind=kind,
            source=source,
            watch=watch,
            lead_id=lead_id,
            tier=tier,
            model=model,
            asked_at=AT,
        )
        self.questions.append(row)
        return row.question_id

    async def answer_question(self, question_id: int, answer: str) -> None:
        self.questions = [replace(row, answer=answer) if row.question_id == question_id else row for row in self.questions]

    async def question(self, question_id: int) -> StoredQuestion | None:
        return next((row for row in self.questions if row.question_id == question_id), None)

    async def questions_of(self, source: str) -> list[StoredQuestion]:
        return [row for row in self.questions if row.source == source]

    async def finding(self, finding_id: int) -> StoredFinding | None:
        return next((row for row in self.findings if row.finding_id == finding_id), None)

    async def findings_newest(self, *, symptom: str | None, source: str | None, found_by: str | None, limit: int) -> list[StoredFinding]:
        matched = [
            row
            for row in reversed(self.findings)
            if (symptom is None or row.symptom == symptom)
            and (source is None or row.source == source)
            and (found_by is None or row.found_by == found_by)
        ]
        return matched[:limit]

    async def leads_of(self, source: str) -> list[StoredLead]:
        return [row for row in self.leads if row.source == source]

    async def findings_of(self, source: str) -> list[StoredFinding]:
        return [row for row in self.findings if row.source == source]

    async def lead(self, lead_id: int) -> StoredLead | None:
        return next((row for row in self.leads if row.lead_id == lead_id), None)

    async def resolve_lead(self, lead_id: int, resolution: AttemptResolution, reason: str) -> bool:
        if lead_id in await self.settled([lead_id]) or not any(row.lead_id == lead_id for row in self.leads):
            return False
        self.refusals[lead_id] = reason
        self.leads = [
            replace(row, attempted_at=AT, refusal=reason, resolution=resolution) if row.lead_id == lead_id else row for row in self.leads
        ]
        return True

    async def settled(self, lead_ids: Sequence[int]) -> set[int]:
        have = {row.lead_id for row in self.findings if row.lead_id is not None}
        have.update(row.lead_id for row in self.leads if row.resolution == "dismissed")
        return {lead_id for lead_id in lead_ids if lead_id in have}


@dataclass
class FakeCursors:
    """Remembers each consumer's position."""

    positions: dict[str, int] = field(default_factory=dict)

    async def position(self, consumer: str) -> int | None:
        return self.positions.get(consumer)

    async def advance(self, consumer: str, position: int) -> None:
        self.positions[consumer] = position


@dataclass
class FakeLeadQueue:
    """Remembers the lead ids handed to the inspector and the runs it was asked to read whole."""

    queued: list[int] = field(default_factory=list)
    scans: list[tuple[str, str]] = field(default_factory=list)
    scan_terminals: list[int | None] = field(default_factory=list)

    async def enqueue(self, lead_ids: Sequence[int]) -> None:
        self.queued.extend(lead_ids)

    async def scan_run(self, source: str, watch: str, *, terminal_event: int | None = None) -> Enqueued:
        self.scans.append((source, watch))
        self.scan_terminals.append(terminal_event)
        return Enqueued(job_id=len(self.scans), created=True)


@dataclass
class FakeRecordEvents:
    """The record's events as a list, read the way the dispatch and the sweep read them."""

    events: list[EventAtPositionRow] = field(default_factory=list)

    async def events_after(self, position: int, *, limit: int) -> list[EventAtPositionRow]:
        return [found for found in self.events if found.id > position and found.kind in DISPATCH_KINDS][:limit]

    async def last_position(self) -> int:
        return max((found.id for found in self.events), default=0)

    async def ended_since(self, since: datetime, *, limit: int, after_position: int = 0) -> list[EventAtPositionRow]:
        return sorted(
            (found for found in self.events if found.kind in TERMINAL_KINDS and found.at >= since and found.id > after_position),
            key=lambda found: found.id,
        )[:limit]


@dataclass
class FakeRunsOfConversation:
    """The conversation's earlier runs as a list, by the source of the run under inspection."""

    by_source: dict[str, list[RunSummary]] = field(default_factory=dict)

    async def before(self, source: str) -> list[RunSummary]:
        return self.by_source.get(source, [])
