"""The inspector's lane: one stored lead, one finding or a recorded refusal.

The tiers are asked cheapest first. A tier that cannot tell passes to the next. A tier whose names the taxonomy refuses
is asked once more with the refusal in front of it. When no tier decides, the lead stays a lead and says why.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from core.ai.inspection.domain.leads import Lead
from core.ai.inspection.domain.taxonomy import TAXONOMY, Finding, Taxonomy
from core.ai.inspection.inspector import InspectorDeps, NoIssue, TierAgent, Undecided, Unresolved, ask_twice, attribute, model_name
from core.ai.transcripts.contracts import UNREADABLE, unreadable_reason

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from pydantic_ai import Agent

    from core.ai.inspection.inspector import Attribution
    from core.ai.inspection.internal_contracts import LeadsAndFindings, Questions
    from core.ai.inspection.lanes import AttributeLead
    from core.ai.transcripts.contracts import RunsOfConversation, TranscriptLoaders
    from core.cqrs.contracts import JobContext


@dataclass(frozen=True)
class AttributeLeadHandler:
    """What a job runner serves under the `attribute_lead` kind."""

    store: LeadsAndFindings
    loaders: TranscriptLoaders
    agents: Sequence[TierAgent[Attribution | Undecided | NoIssue]]
    conversation: RunsOfConversation
    questions: Questions
    taxonomy: Taxonomy = TAXONOMY

    async def __call__(self, payload: AttributeLead, context: JobContext) -> str:
        """One line on what happened: the finding stored, or why the lead stays a lead.

        Each tier is one question of its own, recorded before it is asked and answered with the line it produced.

        Args:
            payload: The lead to attribute.
            context: The job's id and attempt. It is not read.

        Returns:
            The line.
        """
        del context
        stored = await self.store.lead(payload.lead_id)
        if stored is None:
            return f"lead {payload.lead_id}: gone"
        already_settled = f"lead {payload.lead_id}: already settled"
        if payload.lead_id in await self.store.settled([payload.lead_id]):
            return already_settled
        lead = Lead(source=stored.source, symptom=stored.symptom, spans=tuple(stored.spans), found_by=stored.found_by)
        try:
            transcript = await self.loaders.load(lead.source)
        except UNREADABLE as error:
            words = unreadable_reason(error)
            recorded = await self.store.resolve_lead(payload.lead_id, "undecided", words)
            return f"lead {payload.lead_id}: {words}" if recorded else already_settled
        deps = InspectorDeps(transcript=transcript, loaders=self.loaders, conversation=self.conversation)
        reasons: list[str] = []
        unresolved: list[Unresolved] = []
        question_id: int | None = None
        for tier_agent in self.agents:
            conversation_id = uuid4()
            asked_of = model_name(tier_agent.agent)
            question_id = await self.questions.add_question(
                kind="attribute",
                conversation_id=conversation_id,
                source=lead.source,
                watch=stored.watch,
                lead_id=payload.lead_id,
                tier=tier_agent.tier,
                model=asked_of,
            )
            answer = await self._ask(tier_agent.agent, lead, deps, conversation_id)
            if isinstance(answer, Finding):
                finding_id = await self.store.add_finding(
                    answer, source=lead.source, lead_id=payload.lead_id, found_by="inspector", model=asked_of, question_id=question_id
                )
                line = f"lead {payload.lead_id}: finding {finding_id} by {asked_of}" if finding_id is not None else already_settled
                await self.questions.answer_question(question_id, line)
                return line
            if isinstance(answer, NoIssue):
                recorded = await self.store.resolve_lead(payload.lead_id, "dismissed", answer.reason)
                line = f"lead {payload.lead_id}: dismissed ({answer.reason})" if recorded else already_settled
                await self.questions.answer_question(question_id, line)
                return line
            unresolved.append(answer)
            await self.questions.answer_question(question_id, answer.reason)
            reasons.append(f"{asked_of}: {answer.reason}")
        reason = "; ".join(reasons)
        resolution = "invalid" if unresolved and all(answer.resolution == "invalid" for answer in unresolved) else "undecided"
        recorded = await self.store.resolve_lead(payload.lead_id, resolution, reason)
        if not recorded and question_id is not None:
            await self.questions.answer_question(question_id, already_settled)
        return f"lead {payload.lead_id}: {resolution} ({reason})" if recorded else already_settled

    async def _ask(
        self, agent: Agent[InspectorDeps, Attribution | Undecided | NoIssue], lead: Lead, deps: InspectorDeps, conversation_id: UUID
    ) -> Finding | NoIssue | Unresolved:
        """One tier's answer: a finding, or why it gave none. Refused names are asked about once more, in the same conversation."""
        answer = await ask_twice(
            lambda refusal: attribute(agent, lead, deps=deps, conversation_id=conversation_id, taxonomy=self.taxonomy, refusal=refusal)
        )
        if isinstance(answer, Undecided):
            return Unresolved("undecided", f"undecided: {answer.reason}")
        return answer
