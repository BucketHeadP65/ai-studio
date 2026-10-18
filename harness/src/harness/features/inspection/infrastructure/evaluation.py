"""Run diagnostic evaluations through the inspector's recorded-evidence tools."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

import httpx

from core.ai.evaluation.contracts import EvaluationError, EvaluationInput, ResourceUsage, content_fingerprint
from core.ai.inspection.contracts import (
    SCAN_PROMPT,
    DiagnosticPrediction,
    DiagnosticResponse,
    InspectorDeps,
    Lead,
    NoIssue,
    NothingSeen,
    ScanUndecided,
    Undecided,
    candidates,
    inspector_agent,
    outline,
    resolve,
    resolve_scan,
    scan_agent,
    view_for,
)
from core.ai.transcripts.contracts import Transcript, TranscriptLoaders
from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_ai import capture_run_messages
from pydantic_ai.exceptions import AgentRunError, ModelAPIError, RunCancelled
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelRequest, ModelResponse
from pydantic_ai.usage import RunUsage, UsageLimits

from harness.adapters.contracts import priced_cost

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from core.ai.inspection.contracts import Finding
    from core.ai.transcripts.contracts import RunSummary
    from pydantic_ai.models import Model

type EvidenceAccess = Literal["complete", "messages_only"]
"""What a trial may read: the complete recorded evidence, or the messages without request instructions and tool definitions."""
TRIAL_REQUEST_LIMIT = 12
"""The most model requests one trial may make."""
TRIAL_TIMEOUT_SECONDS = 120.0
"""How long one trial may run, in seconds, before it fails as timed out."""


class CaseInput(BaseModel):
    """Recorded task evidence and an optional rule lead, without any evaluation labels."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transcript: Transcript
    lead: Lead | None = None

    @model_validator(mode="after")
    def lead_belongs_to_transcript(self) -> CaseInput:
        """Reject inconsistent lead sources or evidence positions before running a model.

        Returns:
            The same case input, unchanged.

        Raises:
            ValueError: when the lead names another source than the transcript, or cites a position outside it.
        """
        if self.lead is None:
            return self
        if self.lead.source != self.transcript.source:
            msg = "the lead and transcript must name the same source"
            raise ValueError(msg)
        try:
            for span in self.lead.spans:
                self.transcript.part_at(span)
        except IndexError as error:
            msg = "the lead cites a position outside its transcript"
            raise ValueError(msg) from error
        return self


def evidence_transcript(transcript: Transcript, access: EvidenceAccess) -> Transcript:
    """Select complete evidence or an ablation that hides request instructions and tool definitions.

    The ablation changes only what can be read. It preserves the task, behavior and outcomes.

    Args:
        transcript: The recorded transcript.
        access: Which evidence the trial may read.

    Returns:
        The same transcript for complete access, or a copy without request parameters and instructions.
    """
    if access == "complete":
        return transcript
    steps = tuple(
        step.model_copy(
            update={
                "parameters": None,
                "messages": tuple(
                    replace(message, instructions=None) if isinstance(message, ModelRequest) else message for message in step.messages
                ),
            }
        )
        for step in transcript.steps
    )
    return transcript.model_copy(update={"steps": steps})


@dataclass(frozen=True)
class CaseSource:
    """The single immutable transcript an isolated evaluation may reopen."""

    transcript: Transcript

    async def __call__(self, locator: str) -> Transcript:
        """Open the case's source, refusing an unrelated run.

        Args:
            locator: The part of the source after its scheme.

        Returns:
            The case's transcript.

        Raises:
            LookupError: when `locator` names another run than the case's own.
        """
        if locator != self.transcript.source.partition(":")[2]:
            msg = f"no evaluation transcript {locator!r}"
            raise LookupError(msg)
        return self.transcript

    async def before(self, source: str) -> list[RunSummary]:
        """No earlier runs are supplied by this isolated case.

        Args:
            source: The run as a transcript source, ignored.

        Returns:
            Always empty.
        """
        del source
        return []


def recorded_usage(usage: RunUsage, messages: Sequence[ModelMessage], *, complete: bool = True) -> ResourceUsage:
    """Count all requests and price recorded responses, preserving unknown costs.

    Args:
        usage: The trial's token and request counts.
        messages: The trial's recorded messages, whose responses are priced.
        complete: Whether every response of the trial was recorded. The cost is unknown when not.

    Returns:
        The trial's resource usage. Its cost is `None` unless the record is complete and holds at least one response, every one with a known price.
    """
    prices = [priced_cost(message) for message in messages if isinstance(message, ModelResponse)]
    known = [price for price in prices if price is not None]
    return ResourceUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        requests=usage.requests,
        cost_usd=float(sum(known)) if complete and prices and len(known) == len(prices) else None,
    )


@dataclass
class ObservedModel:
    """Reported model identities shared across paired trials, refusing an observed change."""

    names: set[str] = field(default_factory=set)
    revisions: set[str] = field(default_factory=set)

    def observe(self, messages: Sequence[ModelMessage]) -> None:
        """Retain provider-reported identities without pretending an alias is a pinned revision.

        Args:
            messages: A trial's recorded messages.

        Raises:
            ValueError: when the provider has reported more than one model name or more than one revision across the trials.
        """
        for message in messages:
            if not isinstance(message, ModelResponse):
                continue
            if message.model_name is not None:
                self.names.add(message.model_name)
            details = message.provider_details or {}
            revision = details.get("system_fingerprint") or details.get("model_version")
            if isinstance(revision, str):
                self.revisions.add(revision)
        if len(self.names) > 1 or len(self.revisions) > 1:
            msg = "the provider reported different model identities across paired trials"
            raise ValueError(msg)


def _prediction(findings: Sequence[Finding]) -> DiagnosticPrediction:
    return DiagnosticPrediction(
        verdict="issue",
        symptoms=tuple(sorted({finding.symptom.name for finding in findings})),
        causes=tuple(sorted({blame.what.name for finding in findings for blame in finding.blames})),
        reason="\n".join(finding.explanation or finding.symptom.what_you_see for finding in findings),
    )


@dataclass(frozen=True)
class InspectorPredictor:
    """The normal inspector with isolated evidence, bounded trials and optional recorded traces."""

    model: Model
    evidence: EvidenceAccess = "complete"
    trace_dir: Path | None = None
    request_limit: int = TRIAL_REQUEST_LIMIT
    timeout_seconds: float = TRIAL_TIMEOUT_SECONDS
    observed: ObservedModel = field(default_factory=ObservedModel)

    async def __call__(self, case: EvaluationInput, repetition: int) -> DiagnosticResponse:
        """Predict from the case input only and preserve usage when a provider or output fails.

        Args:
            case: The case's input, without its labels.
            repetition: Which trial of the case this is, counting from zero.

        Returns:
            The prediction and what the trial spent.

        Raises:
            EvaluationError: when the agent run, its output, the provider or the time limit fails, carrying the usage so far.
        """
        given = CaseInput.model_validate_json(case.input)
        opaque_source = f"evaluation:{content_fingerprint(case.input)}"
        transcript = evidence_transcript(given.transcript.model_copy(update={"source": opaque_source}), self.evidence)
        lead = given.lead.model_copy(update={"source": opaque_source}) if given.lead is not None else None
        source = CaseSource(transcript)
        deps = InspectorDeps(
            transcript=transcript,
            loaders=TranscriptLoaders(by_key={transcript.source.partition(":")[0]: source}),
            conversation=source,
            lead=lead,
        )
        usage = RunUsage()
        with capture_run_messages() as messages:
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    prediction = await self._predict(lead, deps, usage)
                    self.observed.observe(messages)
            except RunCancelled:
                raise
            except (AgentRunError, ValueError, httpx.HTTPError, TimeoutError) as error:
                complete = not isinstance(error, ModelAPIError | httpx.HTTPError | TimeoutError)
                raise EvaluationError(
                    str(error) or type(error).__name__, usage=recorded_usage(usage, messages, complete=complete)
                ) from error
            finally:
                if self.trace_dir is not None:
                    name = f"{content_fingerprint(case.case_id)[:16]}-{self.evidence}-{repetition}.json"
                    await asyncio.to_thread(_save_trace, self.trace_dir / name, messages)
        return DiagnosticResponse(prediction=prediction, usage=recorded_usage(usage, messages))

    async def _predict(self, lead: Lead | None, deps: InspectorDeps, usage: RunUsage) -> DiagnosticPrediction:
        """Use the same prompts, output shapes and validation as online inspection."""
        limits = UsageLimits(request_limit=self.request_limit)
        if lead is not None:
            attributing = inspector_agent(self.model)
            attribution = await attributing.run(view_for(lead, deps.transcript), deps=deps, usage=usage, usage_limits=limits)
            if isinstance(attribution.output, NoIssue):
                return DiagnosticPrediction(verdict="clean", reason=attribution.output.reason)
            if isinstance(attribution.output, Undecided):
                return DiagnosticPrediction(verdict="abstain", reason=attribution.output.reason)
            return _prediction([resolve(attribution.output, spans=lead.spans)])
        agent = scan_agent(self.model)
        prompt = SCAN_PROMPT.format(candidates=candidates(), outline=outline(deps.transcript))
        result = await agent.run(prompt, deps=deps, usage=usage, usage_limits=limits)
        seen = resolve_scan(result.output, deps.transcript)
        if isinstance(seen, ScanUndecided):
            return DiagnosticPrediction(verdict="abstain", reason=seen.reason)
        if not seen:
            return DiagnosticPrediction(
                verdict="clean", reason=result.output.reason if isinstance(result.output, NothingSeen) else "No issue seen."
            )
        return _prediction([finding for _, finding in seen])


def _save_trace(path: Path, messages: Sequence[ModelMessage]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ModelMessagesTypeAdapter.dump_json(list(messages), indent=2))
