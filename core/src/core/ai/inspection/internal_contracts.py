"""The inspection's own ports: its tables, the record's events it reads, and the job lanes it fills."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from core.ai.history.contracts import EventAtPositionRow
    from core.ai.inspection.domain.leads import Lead
    from core.ai.inspection.domain.taxonomy import Finding
    from core.ai.inspection.read_models import AttemptResolution, QuestionType, StoredFinding, StoredLead, StoredQuestion
    from core.ai.workflows.contracts import Tier
    from core.cqrs.contracts import Enqueued


class LeadsAndFindings(Protocol):
    """What the inspection job handlers need of a store."""

    async def add_leads(self, watch: str, leads: Sequence[Lead]) -> list[int]:
        """Store each lead once under `watch` and answer their ids, in the order given.

        Args:
            watch: The watch the leads were found under.
            leads: The leads to store.

        Returns:
            The lead ids, in the order given.
        """
        ...

    async def add_finding(
        self, finding: Finding, *, source: str, lead_id: int | None, found_by: str, model: str | None, question_id: int | None = None
    ) -> int | None:
        """Store one finding, pointing at the lead it settled and the question that decided it, and answer its id.

        A lead already holding a finding answers its id. An explicit dismissal answers None.

        Args:
            finding: The finding to store.
            source: The transcript source it is about.
            lead_id: The lead it settles, or None.
            found_by: Which kind of detector found it.
            model: The model that decided it, or None.
            question_id: The question that decided it, or None.

        Returns:
            The finding id, or None when a dismissal already settled the lead.
        """
        ...

    async def lead(self, lead_id: int) -> StoredLead | None:
        """One lead by id, or None.

        Args:
            lead_id: The lead id.

        Returns:
            The lead, or None when there is none.
        """
        ...

    async def resolve_lead(self, lead_id: int, resolution: AttemptResolution, reason: str) -> bool:
        """Record the resolution. Return False if the lead is gone or a finding or dismissal already settled it.

        Args:
            lead_id: The lead id.
            resolution: How the attempt ended.
            reason: Why it ended that way.

        Returns:
            True when the resolution was recorded.
        """
        ...

    async def settled(self, lead_ids: Sequence[int]) -> set[int]:
        """The ids among these that have a finding or an explicit dismissal.

        Args:
            lead_ids: The lead ids to check.

        Returns:
            The settled ids.
        """
        ...


class Questions(Protocol):
    """What a handler needs of the questions table: record one, then say how it ended."""

    async def add_question(
        self, *, kind: QuestionType, conversation_id: UUID, source: str, watch: str, lead_id: int | None, tier: Tier, model: str
    ) -> int:
        """Record that the inspector is being asked one thing, and answer the question's id.

        Args:
            kind: What kind of question it is.
            conversation_id: The conversation the inspector answers in.
            source: The transcript source asked about.
            watch: The watch the question is asked under.
            lead_id: The lead asked about, or None for a whole run.
            tier: The model tier the inspector runs at.
            model: The model the inspector runs on.

        Returns:
            The question id.
        """
        ...

    async def answer_question(self, question_id: int, answer: str) -> None:
        """Set the one line the handler ended that question with.

        Args:
            question_id: The question id.
            answer: The line the question ended with.
        """
        ...


class InspectionQueries(Protocol):
    """What a reader of the inspection needs: findings, the lead under one, and the question that decided it."""

    async def findings_newest(self, *, symptom: str | None, source: str | None, found_by: str | None, limit: int) -> list[StoredFinding]:
        """The newest findings matching every filter that is not None, newest first, at most `limit`.

        Args:
            symptom: Only findings of this symptom, or None.
            source: Only findings about this source, or None.
            found_by: Only findings by this kind of detector, or None.
            limit: The most findings to return.

        Returns:
            The findings.
        """
        ...

    async def finding(self, finding_id: int) -> StoredFinding | None:
        """One finding by id, or None.

        Args:
            finding_id: The finding id.

        Returns:
            The finding, or None when there is none.
        """
        ...

    async def lead(self, lead_id: int) -> StoredLead | None:
        """One lead by id, or None.

        Args:
            lead_id: The lead id.

        Returns:
            The lead, or None when there is none.
        """
        ...

    async def leads_of(self, source: str) -> list[StoredLead]:
        """Every lead seen in `source`, oldest first.

        Args:
            source: The transcript source.

        Returns:
            The leads.
        """
        ...

    async def findings_of(self, source: str) -> list[StoredFinding]:
        """Every finding decided about `source`, oldest first.

        Args:
            source: The transcript source.

        Returns:
            The findings.
        """
        ...

    async def question(self, question_id: int) -> StoredQuestion | None:
        """One question by id, or None.

        Args:
            question_id: The question id.

        Returns:
            The question, or None when there is none.
        """
        ...

    async def questions_of(self, source: str) -> list[StoredQuestion]:
        """Every question asked about `source`, oldest first.

        Args:
            source: The transcript source.

        Returns:
            The questions.
        """
        ...


class Cursors(Protocol):
    """Where a consumer of the record's events keeps the position it last handled."""

    async def position(self, consumer: str) -> int | None:
        """The consumer's position, or None when it has never advanced.

        Args:
            consumer: The consumer's name.

        Returns:
            The position, or None.
        """
        ...

    async def advance(self, consumer: str, position: int) -> None:
        """Set the consumer's position, creating its row the first time.

        Args:
            consumer: The consumer's name.
            position: The new position.
        """
        ...


class RecordEvents(Protocol):
    """The record's events as a consumer reads them."""

    async def events_after(self, position: int, *, limit: int) -> list[EventAtPositionRow]:
        """The events past `position` a watch acts on, oldest first, at most `limit`.

        Args:
            position: The last position already read.
            limit: The most events to return.

        Returns:
            The events.
        """
        ...

    async def last_position(self) -> int:
        """The record's current position, 0 when it holds no event.

        Returns:
            The position.
        """
        ...

    async def ended_since(self, since: datetime, *, limit: int, after_position: int = 0) -> list[EventAtPositionRow]:
        """Terminal events at or after `since` past `after_position`, in increasing position order, at most `limit`.

        Args:
            since: The earliest end time to include.
            limit: The most events to return.
            after_position: Only events past this position.

        Returns:
            The ending events.
        """
        ...


class InspectRuns(Protocol):
    """The rules lane, as the dispatch fills it."""

    async def inspect_run(self, source: str, watch: str, *, terminal_event: int | None = None) -> Enqueued:
        """Issue `InspectRun(source, watch)`, or answer the one already in flight.

        Args:
            source: The transcript source of the run.
            watch: The watch it runs under.
            terminal_event: The event that ended the run, when one did.

        Returns:
            The job id, and whether this call created it.
        """
        ...


class SweptLanes(InspectRuns, Protocol):
    """The rules lane, as the sweep fills it and asks it."""

    async def inspected(self, watch: str, source: str, *, terminal_event: int | None = None) -> bool:
        """Whether a successful command under this watch processed the requested terminal event.

        Args:
            watch: The watch.
            source: The transcript source of the run.
            terminal_event: The terminal event to look for, or None for any.

        Returns:
            True when such a command succeeded.
        """
        ...


class LeadQueue(Protocol):
    """Where the inspector's work goes: one command per lead, and one per run to read whole."""

    async def enqueue(self, lead_ids: Sequence[int]) -> None:
        """Hand these leads to the inspector, in this order.

        Args:
            lead_ids: The lead ids.
        """
        ...

    async def scan_run(self, source: str, watch: str, *, terminal_event: int | None = None) -> Enqueued:
        """Ask the inspector to read this whole run under this watch, or answer the one already in flight.

        Args:
            source: The transcript source of the run.
            watch: The watch it runs under.
            terminal_event: The event that ended the run, when one did.

        Returns:
            The job id, and whether this call created it.
        """
        ...
