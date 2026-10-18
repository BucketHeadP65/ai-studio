"""The inspection's collaborators as lists: the record's events, a cursor, a store of leads and findings."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.ai.history.infrastructure.reads import TERMINAL_KINDS
from core.ai.inspection.contracts import StoredBlame, StoredFinding, StoredLead, StoredQuestion
from core.ai.inspection.dispatch import DISPATCH_KINDS

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from core.ai.history.contracts import EventAtPositionRow
    from core.ai.inspection.contracts import Finding, InspectionQueries, Lead
    from core.ai.inspection.internal_contracts import Questions
    from core.ai.inspection.read_models import AttemptResolution, QuestionType
    from core.ai.transcripts.contracts import RunFacts, RunSummary
    from core.ai.workflows.contracts import Tier

    from harness.features.inspection.domain import Answered
    from harness.features.inspection.internal_contracts import FollowUpAnswerer

AT = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


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
class FakeCursors:
    """Remembers each consumer's position."""

    positions: dict[str, int] = field(default_factory=dict)

    async def position(self, consumer: str) -> int | None:
        return self.positions.get(consumer)

    async def advance(self, consumer: str, position: int) -> None:
        self.positions[consumer] = position


@dataclass
class FakeInspectionStore(FakeCursors):
    """Leads and findings in lists, ids counted from 1, one finding per lead."""

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
            if existing is None:
                existing = StoredLead(
                    lead_id=len(self.leads) + 1,
                    source=found.source,
                    watch=watch,
                    symptom=found.symptom,
                    spans=list(found.spans),
                    spans_digest=found.spans_digest,
                    found_by=found.found_by,
                    seen_at=AT,
                )
                self.leads.append(existing)
            ids.append(existing.lead_id)
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
        stored = StoredFinding(
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
        self.findings.append(stored)
        return stored.finding_id

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
        wanted = set(lead_ids)
        return {row.lead_id for row in self.findings if row.lead_id in wanted} | {
            row.lead_id for row in self.leads if row.lead_id in wanted and row.resolution == "dismissed"
        }


@dataclass
class FakeRunsOfConversation:
    """The conversation's earlier runs as a list, by the source of the run under inspection."""

    by_source: dict[str, list[RunSummary]] = field(default_factory=dict)

    async def before(self, source: str) -> list[RunSummary]:
        return self.by_source.get(source, [])


@dataclass
class FakeRunFacts:
    """One summary line per run, by source."""

    by_source: dict[str, RunSummary] = field(default_factory=dict)

    async def summary_of(self, source: str) -> RunSummary | None:
        return self.by_source.get(source)


@dataclass
class FakeFollowUp:
    """Answers a scripted `Answered` for every question asked, or raises what it was told to, and keeps every ask."""

    answered: Answered | None = None
    raising: Exception | None = None
    asked: list[tuple[StoredQuestion, str]] = field(default_factory=list)

    async def answer(self, asked: StoredQuestion, text: str) -> Answered:
        self.asked.append((asked, text))
        if self.raising is not None:
            raise self.raising
        assert self.answered is not None, "script an Answered, or a raising, before FakeFollowUp is asked"
        return self.answered


_reads_proof: type[InspectionQueries] = FakeInspectionStore
"""Pyright fails this module the moment `FakeInspectionStore` drifts from the reader's port."""

_questions_proof: type[Questions] = FakeInspectionStore
"""And the moment it drifts from the questions the handlers write."""

_follow_up_proof: type[FollowUpAnswerer] = FakeFollowUp
"""And the moment `FakeFollowUp` drifts from the port a follow-up route takes."""

_facts_proof: type[RunFacts] = FakeRunFacts
"""And the moment `FakeRunFacts` drifts from the port the outline route takes."""
