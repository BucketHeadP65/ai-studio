"""The leads, findings, questions and cursors tables over asyncpg.

A lead is stored once per (source, symptom, spans digest), and storing it again answers the id it already has. A finding
carries its blames as plain names, so a reader needs no taxonomy to list them, and points at the lead it settled when it
settled one. A question is one thing the inspector was asked, recorded with the conversation its run is in.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, LiteralString
from uuid import UUID

from pydantic import TypeAdapter

from core.ai.history.contracts import Database, PositionRow, row_as, rows_as
from core.ai.inspection.domain.leads import Lead
from core.ai.inspection.domain.taxonomy import Finding
from core.ai.inspection.read_models import AttemptResolution, QuestionType, StoredBlame, StoredFinding, StoredLead, StoredQuestion
from core.ai.transcripts.contracts import Span
from core.ai.workflows.contracts import Tier

_SPANS: TypeAdapter[list[Span]] = TypeAdapter(list[Span])
_BLAMES: TypeAdapter[list[StoredBlame]] = TypeAdapter(list[StoredBlame])

INSERT_LEAD: Final[LiteralString] = """
    INSERT INTO leads (source, watch, symptom, spans, spans_digest, found_by)
    VALUES ($1, $2, $3, $4::jsonb, $5, $6)
    ON CONFLICT (source, symptom, spans_digest) DO NOTHING
    RETURNING lead_id
"""
SELECT_LEAD_ID: Final[LiteralString] = "SELECT lead_id FROM leads WHERE source = $1 AND symptom = $2 AND spans_digest = $3"
LOCK_LEAD: Final[LiteralString] = "SELECT lead_id FROM leads WHERE lead_id = $1 FOR UPDATE"
SELECT_DISMISSED_LEAD: Final[LiteralString] = "SELECT lead_id FROM leads WHERE lead_id = $1 AND resolution = 'dismissed'"
SELECT_LEADS: Final[LiteralString] = """
    SELECT lead_id, source, watch, symptom, spans, spans_digest, found_by, seen_at, attempted_at, refusal, resolution
    FROM leads WHERE source = $1 ORDER BY lead_id
"""
SELECT_LEAD: Final[LiteralString] = """
    SELECT lead_id, source, watch, symptom, spans, spans_digest, found_by, seen_at, attempted_at, refusal, resolution
    FROM leads WHERE lead_id = $1
"""
RESOLVE_LEAD: Final[LiteralString] = """
    UPDATE leads SET attempted_at = now(), resolution = $2, refusal = $3
    WHERE lead_id = $1 AND resolution <> 'dismissed'
        AND NOT EXISTS (SELECT 1 FROM findings WHERE findings.lead_id = leads.lead_id)
    RETURNING lead_id
"""
SELECT_POSITION: Final[LiteralString] = "SELECT position FROM cursors WHERE consumer = $1"
ADVANCE: Final[LiteralString] = """
    INSERT INTO cursors (consumer, position) VALUES ($1, $2)
    ON CONFLICT (consumer) DO UPDATE SET position = excluded.position
"""
INSERT_FINDING: Final[LiteralString] = """
    INSERT INTO findings (lead_id, source, symptom, spans, blames, found_by, model, question_id, explanation)
    VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7, $8, $9)
    ON CONFLICT (lead_id) DO NOTHING
    RETURNING finding_id
"""
SELECT_FINDING_ID: Final[LiteralString] = "SELECT finding_id FROM findings WHERE lead_id = $1"
SELECT_SETTLED: Final[LiteralString] = """
    SELECT lead_id FROM findings WHERE lead_id = ANY($1::bigint[])
    UNION SELECT lead_id FROM leads WHERE lead_id = ANY($1::bigint[]) AND resolution = 'dismissed'
"""
SELECT_FINDINGS: Final[LiteralString] = """
    SELECT finding_id, lead_id, source, symptom, spans, blames, found_by, model, question_id, decided_at, explanation
    FROM findings WHERE source = $1 ORDER BY finding_id
"""
SELECT_FINDING: Final[LiteralString] = """
    SELECT finding_id, lead_id, source, symptom, spans, blames, found_by, model, question_id, decided_at, explanation
    FROM findings WHERE finding_id = $1
"""
SELECT_FINDINGS_NEWEST: Final[LiteralString] = """
    SELECT finding_id, lead_id, source, symptom, spans, blames, found_by, model, question_id, decided_at, explanation
    FROM findings
    WHERE ($1::text IS NULL OR symptom = $1) AND ($2::text IS NULL OR source = $2) AND ($3::text IS NULL OR found_by = $3)
    ORDER BY finding_id DESC LIMIT $4
"""
INSERT_QUESTION: Final[LiteralString] = """
    INSERT INTO questions (conversation_id, kind, source, watch, lead_id, tier, model)
    VALUES ($1, $2, $3, $4, $5, $6, $7)
    RETURNING question_id
"""
ANSWER_QUESTION: Final[LiteralString] = "UPDATE questions SET answer = $2 WHERE question_id = $1"
SELECT_QUESTION: Final[LiteralString] = """
    SELECT question_id, conversation_id, kind, source, watch, lead_id, tier, model, asked_at, answer
    FROM questions WHERE question_id = $1
"""
SELECT_QUESTIONS: Final[LiteralString] = """
    SELECT question_id, conversation_id, kind, source, watch, lead_id, tier, model, asked_at, answer
    FROM questions WHERE source = $1 ORDER BY question_id
"""


@dataclass(frozen=True)
class LeadIdRow:
    """The id a lead statement answered with."""

    lead_id: int


@dataclass(frozen=True)
class FindingIdRow:
    """The id a finding statement answered with."""

    finding_id: int


@dataclass(frozen=True)
class QuestionIdRow:
    """The id a question statement answered with."""

    question_id: int


class InspectionStore:
    """The leads and findings tables over asyncpg."""

    def __init__(self, database: Database) -> None:
        """Open the store on `database`.

        Args:
            database: A database whose connections reach the inspection's tables.
        """
        self.database = database

    async def add_leads(self, watch: str, leads: Sequence[Lead]) -> list[int]:
        """Store each lead once under `watch` and answer their ids, in the order given.

        Args:
            watch: The watch the leads were found under.
            leads: The leads to store.

        Returns:
            The lead ids, in the order given.
        """
        ids: list[int] = []
        async with self.database.acquire() as connection, connection.transaction():
            for lead in leads:
                spans = _SPANS.dump_json(list(lead.spans)).decode()
                row = await connection.fetchrow(INSERT_LEAD, lead.source, watch, lead.symptom, spans, lead.spans_digest, lead.found_by)
                if row is None:
                    row = await connection.fetchrow(SELECT_LEAD_ID, lead.source, lead.symptom, lead.spans_digest)
                if row is None:
                    msg = f"lead {lead.symptom} at {lead.source} vanished between the insert and the lookup"
                    raise RuntimeError(msg)
                ids.append(row_as(LeadIdRow, row).lead_id)
        return ids

    async def add_finding(
        self, finding: Finding, *, source: str, lead_id: int | None, found_by: str, model: str | None, question_id: int | None = None
    ) -> int | None:
        """Store one finding, or answer the id of the finding its lead already holds.

        Args:
            finding: The finding to store.
            source: The transcript source it is about.
            lead_id: The lead it settles, or None.
            found_by: Which kind of detector found it.
            model: The model that decided it, or None.
            question_id: The question that decided it, or None.

        Returns:
            The finding id, or None when a dismissal already settled the lead.

        Raises:
            RuntimeError: when neither the insert nor the lookup answers an id.
        """
        spans = _SPANS.dump_json(list(finding.evidence)).decode()
        blames = _BLAMES.dump_json(
            [
                StoredBlame(root_cause=blame.what.name, surface_kind=blame.where.kind.value, surface_name=blame.where.name, why=blame.why)
                for blame in finding.blames
            ]
        ).decode()
        async with self.database.acquire() as connection, connection.transaction():
            if lead_id is not None:
                await connection.fetchrow(LOCK_LEAD, lead_id)
                if await connection.fetchrow(SELECT_DISMISSED_LEAD, lead_id) is not None:
                    return None
            row = await connection.fetchrow(
                INSERT_FINDING, lead_id, source, finding.symptom.name, spans, blames, found_by, model, question_id, finding.explanation
            )
            if row is None and lead_id is not None:
                row = await connection.fetchrow(SELECT_FINDING_ID, lead_id)
                if row is None:
                    msg = f"finding {finding.symptom.name} at {source} vanished between the insert and the lookup"
                    raise RuntimeError(msg)
        if row is None:
            msg = "the insert answered no id"
            raise RuntimeError(msg)
        return row_as(FindingIdRow, row).finding_id

    async def leads_of(self, source: str) -> list[StoredLead]:
        """Every lead seen in `source`, oldest first.

        Args:
            source: The transcript source.

        Returns:
            The leads.
        """
        async with self.database.acquire() as connection:
            return rows_as(StoredLead, await connection.fetch(SELECT_LEADS, source))

    async def findings_of(self, source: str) -> list[StoredFinding]:
        """Every finding decided about `source`, oldest first.

        Args:
            source: The transcript source.

        Returns:
            The findings.
        """
        async with self.database.acquire() as connection:
            return rows_as(StoredFinding, await connection.fetch(SELECT_FINDINGS, source))

    async def lead(self, lead_id: int) -> StoredLead | None:
        """One lead by id, or None.

        Args:
            lead_id: The lead id.

        Returns:
            The lead, or None when there is none.
        """
        async with self.database.acquire() as connection:
            row = await connection.fetchrow(SELECT_LEAD, lead_id)
        return None if row is None else row_as(StoredLead, row)

    async def resolve_lead(self, lead_id: int, resolution: AttemptResolution, reason: str) -> bool:
        """Record the resolution. Return False if the lead is gone or a finding or dismissal already settled it.

        Args:
            lead_id: The lead id.
            resolution: How the attempt ended.
            reason: Why it ended that way.

        Returns:
            True when the resolution was recorded.
        """
        async with self.database.acquire() as connection, connection.transaction():
            await connection.fetchrow(LOCK_LEAD, lead_id)
            return await connection.fetchrow(RESOLVE_LEAD, lead_id, resolution, reason) is not None

    async def settled(self, lead_ids: Sequence[int]) -> set[int]:
        """The ids among these that have a finding or an explicit dismissal.

        Args:
            lead_ids: The lead ids to check.

        Returns:
            The settled ids.
        """
        if not lead_ids:
            return set()
        async with self.database.acquire() as connection:
            rows = await connection.fetch(SELECT_SETTLED, list(lead_ids))
        return {row_as(LeadIdRow, row).lead_id for row in rows}

    async def finding(self, finding_id: int) -> StoredFinding | None:
        """One finding by id, or None.

        Args:
            finding_id: The finding id.

        Returns:
            The finding, or None when there is none.
        """
        async with self.database.acquire() as connection:
            row = await connection.fetchrow(SELECT_FINDING, finding_id)
        return None if row is None else row_as(StoredFinding, row)

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
        async with self.database.acquire() as connection:
            return rows_as(StoredFinding, await connection.fetch(SELECT_FINDINGS_NEWEST, symptom, source, found_by, limit))

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

        Raises:
            RuntimeError: when the insert answers no id.
        """
        async with self.database.acquire() as connection:
            row = await connection.fetchrow(INSERT_QUESTION, conversation_id, kind, source, watch, lead_id, tier, model)
        if row is None:
            msg = f"the question about {source} answered no id"
            raise RuntimeError(msg)
        return row_as(QuestionIdRow, row).question_id

    async def answer_question(self, question_id: int, answer: str) -> None:
        """Set the one line the handler ended that question with.

        Args:
            question_id: The question id.
            answer: The line the question ended with.
        """
        async with self.database.acquire() as connection:
            await connection.execute(ANSWER_QUESTION, question_id, answer)

    async def question(self, question_id: int) -> StoredQuestion | None:
        """One question by id, or None.

        Args:
            question_id: The question id.

        Returns:
            The question, or None when there is none.
        """
        async with self.database.acquire() as connection:
            row = await connection.fetchrow(SELECT_QUESTION, question_id)
        return None if row is None else row_as(StoredQuestion, row)

    async def questions_of(self, source: str) -> list[StoredQuestion]:
        """Every question asked about `source`, oldest first.

        Args:
            source: The transcript source.

        Returns:
            The questions.
        """
        async with self.database.acquire() as connection:
            return rows_as(StoredQuestion, await connection.fetch(SELECT_QUESTIONS, source))

    async def position(self, consumer: str) -> int | None:
        """The consumer's position, or None when it has never advanced.

        Args:
            consumer: The consumer's name.

        Returns:
            The position, or None.
        """
        async with self.database.acquire() as connection:
            row = await connection.fetchrow(SELECT_POSITION, consumer)
        return None if row is None else row_as(PositionRow, row).position

    async def advance(self, consumer: str, position: int) -> None:
        """Set the consumer's position, creating its row the first time.

        Args:
            consumer: The consumer's name.
            position: The new position.
        """
        async with self.database.acquire() as connection:
            await connection.execute(ADVANCE, consumer, position)
