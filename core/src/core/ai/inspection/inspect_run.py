"""The `InspectRun` command and its handler: one run, its watch's rules, leads stored, the inspector's queue fed.

The handler is what a worker serves under the `inspect_run` kind. It re-reads the run from the record as it stands, never
from the event that caused the command, so running it twice or late does no harm: leads are stored once, and a finding a
rule can decide alone is written the same way every time. When the watch says so it also asks the inspector to read the
whole run, in the scan lane.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.messages import ToolCallPart, ToolReturnPart

from core.ai.inspection.domain.taxonomy import TAXONOMY, Blame, Finding, Surface, Taxonomy
from core.ai.inspection.rules import RULES
from core.ai.inspection.rules_pass import RulesPass
from core.ai.transcripts.contracts import NOTHING_TO_READ, unreadable_reason

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.inspection.detectors import Inspected, Rules
    from core.ai.inspection.domain.facts import ToolFacts
    from core.ai.inspection.domain.leads import Lead
    from core.ai.inspection.domain.watches import Watch
    from core.ai.inspection.internal_contracts import LeadQueue, LeadsAndFindings
    from core.ai.inspection.rules_pass import RulesPassResult
    from core.ai.transcripts.contracts import TranscriptLoaders
    from core.cqrs.contracts import JobContext


class InspectRun(BaseModel):
    """Inspect one run under one watch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(description="The run, as `<scheme>:<locator>`.")
    watch: str = Field(description="The watch whose rules and inspector apply.")
    terminal_event: int | None = Field(default=None, ge=1, description="The committed terminal event this inspection must cover, if any.")


def direct_finding(lead: Lead, inspected: Inspected, taxonomy: Taxonomy) -> Finding | None:
    """The finding a rule can decide alone: a rule-caught symptom that allows one root cause landing on one surface kind.

    A symptom a model decides, alone or after a rule flags it, is never decided here whatever its root causes allow.

    Args:
        lead: The lead a rule recorded.
        inspected: The transcript the lead was seen in.
        taxonomy: The taxonomy the symptom is in.

    Returns:
        The finding, or None when a rule cannot decide this symptom alone.
    """
    symptom = taxonomy.symptom(lead.symptom)
    if symptom.caught_by != "rule" or len(symptom.possible_root_causes) != 1:
        return None
    (cause,) = symptom.possible_root_causes
    if len(cause.fix_lands_on) != 1:
        return None
    part = inspected.transcript.part_at(lead.spans[0])
    name = part.tool_name if isinstance(part, ToolCallPart | ToolReturnPart) else ""
    blame = Blame(what=cause, where=Surface(kind=cause.fix_lands_on[0], name=name), why=symptom.what_you_see)
    return Finding(symptom=symptom, evidence=lead.spans, blames=(blame,), found_by="rule")


def _rules_failed(source: str, outcome: RulesPassResult) -> RuntimeError:
    """Which rules raised over this run, as the failure the job carries."""
    named = ", ".join(f"{failure.rule} ({failure.message})" for failure in outcome.rule_failures)
    return RuntimeError(f"{source}: rules failed: {named}")


def _no_result(source: str, outcome: RulesPassResult) -> RuntimeError:
    """Why the inspection opened nothing for this source, as the failure the job carries."""
    failed = next((case for case in outcome.report.failures if case.name == source), None)
    return RuntimeError(f"{source}: {failed.error_message if failed is not None else 'the inspection produced no result'}")


@dataclass(frozen=True)
class InspectRunHandler:
    """Serves `InspectRun`: runs the watch's rules over the run, stores what they saw, feeds the inspector's lane."""

    watches: Mapping[str, Watch]
    facts: ToolFacts
    loaders: TranscriptLoaders
    store: LeadsAndFindings
    queue: LeadQueue
    registry: Rules = RULES
    taxonomy: Taxonomy = TAXONOMY

    async def __call__(self, payload: InspectRun, context: JobContext) -> str:
        """One line on what happened: leads stored, findings written, leads queued, leads already settled, whether a scan was issued.

        A lead that already has a finding is never decided again and never queued.

        `every_run` scans text-only runs as well as tool-using runs. `when_a_rule_led` scans runs with fresh leads and a
        stable sample of rule-free runs. A terminal command also scans a run whose earlier leads were already settled.
        `never` leaves every lead to the rules.

        A run the record no longer holds completes with "run gone". A run that recorded no step completes with "no steps".
        Any other reason the transcript cannot be read, and any rule that raised, fails the job so the job runner retries it
        and gives up visibly.

        Args:
            payload: The run to inspect and the watch it is inspected under.
            context: The job's id and attempt.

        Returns:
            The line.

        Raises:
            KeyError: when the watch is unknown, or names a rule the registry does not hold.
            ValueError: when the source is malformed, names a scheme no loader is registered for, or does not read as a transcript.
            RuntimeError: when a rule raised, or the inspection produced no result for the run.
        """
        watch = self.watches[payload.watch]
        unknown = next((name for name in watch.rules if name not in self.registry.by_key), None)
        if unknown is not None:
            msg = f"{watch.name}: unknown rule {unknown!r}"
            raise KeyError(msg)
        rules = list(watch.rules)
        try:
            transcript = await self.loaders.load(payload.source)
        except NOTHING_TO_READ as error:
            return f"{payload.source}: {unreadable_reason(error)}"
        rules_pass = RulesPass(
            name=f"{watch.name}:{context.job_id}",
            rules=rules,
            facts=self.facts,
            loaders=self.loaders,
            registry=self.registry,
            opened={payload.source: transcript},
        )
        outcome = await rules_pass.run([payload.source])
        if outcome.rule_failures:
            raise _rules_failed(payload.source, outcome)
        inspected = outcome.inspected.get(payload.source)
        if inspected is None:
            raise _no_result(payload.source, outcome)
        ids = await self.store.add_leads(watch.name, outcome.leads)
        once: dict[int, Lead] = {}
        for lead, lead_id in zip(outcome.leads, ids, strict=True):
            once.setdefault(lead_id, lead)
        settled = await self.store.settled(list(once))
        decided = 0
        undecided: list[int] = []
        for lead_id, lead in once.items():
            if lead_id in settled:
                continue
            finding = direct_finding(lead, inspected, self.taxonomy)
            if finding is None:
                undecided.append(lead_id)
                continue
            finding_id = await self.store.add_finding(finding, source=payload.source, lead_id=lead_id, found_by=finding.found_by, model=None)
            if finding_id is None:
                settled.add(lead_id)
            else:
                decided += 1
        led = bool(once) if payload.terminal_event is not None else decided > 0 or bool(undecided)
        sampled = not once and watch.samples(payload.source)
        queued, scanned = await self._feed_inspector(payload, watch, undecided, selected=led or sampled)
        return f"{payload.source}: {len(once)} leads, {decided} findings, {queued} queued, {len(settled)} settled, {'scan' if scanned else 'no scan'}"

    async def _feed_inspector(self, payload: InspectRun, watch: Watch, undecided: list[int], *, selected: bool) -> tuple[int, bool]:
        """Queue the undecided leads and scan the run when the watch reaches that far, answering the count and whether it scanned."""
        if watch.inspector == "never":
            return 0, False
        await self.queue.enqueue(undecided)
        scanned = watch.inspector == "every_run" or selected
        if scanned:
            await self.queue.scan_run(payload.source, watch.name, terminal_event=payload.terminal_event)
        return len(undecided), scanned
