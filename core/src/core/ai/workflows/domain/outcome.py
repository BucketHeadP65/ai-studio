"""What a run actually did, derived from facts the run could not have written about itself.

Three records and one function. A `WorkItemFact` is one piece of the primary work settling, once,
under an identity that survives a rewind. A `MapBatch` is the set of identities a fan-out's current
input minted, so "did everything settle" has an answer rather than a guess. An `OutcomeCheck` is one
required gate asked whether the exact bytes in front of it now were accepted by somebody allowed to
accept them.

`observed_outcome` folds those into an `Outcome`. It reads no report counter and no produced value:
a non-empty result is not evidence that anything useful happened, and work that was skipped never
counts as work that was done. Anything it cannot establish stays `unknown`, which is a different
answer from `failed`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.ai.artifacts import ArtifactRef
from core.ai.workflows.domain.run_events import GateDecisionFact, RetryFeedback, ReviewedArtifactRef, ValueDigest

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from core.ai.workflows.domain.vocabulary import RequiredGateCheck, WorkflowResultSpec

Disposition = Literal["succeeded", "skipped", "failed"]
"""How one piece of the primary work ended. Skipped counts as neither a success nor a failure."""

OutcomeStatus = Literal["succeeded", "partial", "noop", "failed", "unknown"]
"""What the run's checked outcome adds up to. `unknown` is what nothing established, never a guess."""

DecisionOrigin = Literal["judge", "person", "orchestrator", "auto", "timeout", "unknown"]
"""Who a check observed deciding, including the two answers that are nobody: automode and silence."""

AcceptedSource = Literal["judge", "person", "orchestrator"]
"""The reviewers a result may declare it accepts. Automode and a timeout are never among them."""


class WorkItemFact(BaseModel):
    """One piece of one node's work, settled once, under an identity a rewind does not change."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    generation: int = Field(ge=0)
    """Which input the identity was minted under. A node that takes one input at a time uses zero."""
    item_key: str
    """Which piece of that input. `""` for a node that does its work exactly once."""
    disposition: Disposition
    retry_attempts: int = Field(ge=0, default=0)
    """How many times this identity was reworked before settling as it did. Never folded into the counts."""
    detail: str = ""
    """Why, in the words the failure or the skip came with. Empty for work that simply succeeded."""

    @property
    def identity(self) -> tuple[str, int, str]:
        """What makes this the same piece of work across a rewind."""
        return (self.node_id, self.generation, self.item_key)


@dataclass(frozen=True)
class MapBatch:
    """One resolved input to a node, and the identities it minted.

    `ordered_digest` is over the items in source order, so a reorder is a different input even when
    the same values come back. Rewinding onto an identical input keeps the generation and every key,
    which is what makes a repair a second attempt at the same work rather than new work.
    """

    generation: int
    ordered_digest: str
    items: tuple[str, ...]


@dataclass(frozen=True)
class GateReview:
    """What one gate's own `reviews` wire resolves to at the end of the run, and which reach it is on.

    `reviewed` is `None` when the value cannot be read back at all, because the node never produced
    or the value has no canonical spelling. A required gate over such a value cannot pass.
    """

    generation: int
    reviewed: ReviewedArtifactRef | None = None


class OutcomeCheck(BaseModel):
    """One required acceptance, asked of the exact value the run ended holding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: Literal["gate"] = "gate"
    required: bool = True
    passed: bool | None
    """`None` when the check could not be put, which is neither an acceptance nor a refusal."""
    expected_sources: tuple[AcceptedSource, ...]
    observed_source: DecisionOrigin
    review_generation: int | None = None
    reviewed_value_digest: ValueDigest | None = None
    feedback: str = ""
    requested_repair: str = ""
    reviewed: ReviewedArtifactRef | None = None
    detail: str


class Outcome(BaseModel):
    """A run's checked outcome: what settled, what was accepted, and what can be quoted afterwards.

    Separate from the execution's own outcome, which says only whether the graph reached its end.
    A run can complete and have done nothing useful, and a run can fail with most of its work done.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: OutcomeStatus
    summary: str
    attempted: int = 0
    """How many pieces of the primary work settled this generation. Always the sum of the three below."""
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    retry_attempts: int = 0
    """How much rework the settled pieces took, counted apart so a repair never inflates the work."""
    primary_input: int | None = None
    """How many pieces the primary work was handed. `None` when the run never got that far."""
    generation: int | None = None
    """Which input the counts belong to. Earlier generations are kept as audit and never counted."""
    checks: tuple[OutcomeCheck, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()


_NO_RESULT = "This workflow declares no result, so there is nothing to check."
_NO_PRIMARY = "The primary work never took an input, so nothing about it can be established."
_INCOMPLETE = "Part of the primary work never settled, so what it did cannot be established."
_CONTRADICTED = "Two facts disagree about the same piece of work, so nothing about it can be established."
_UNFINISHED = "The run did not complete, so its checked outcome cannot be established."
_UNACCEPTED = "The value this run ended holding was not accepted by a reviewer the result requires."


def observed_outcome(  # noqa: PLR0913 (each argument is a separate fact of the run, and a holder object would only rename the arity)
    result: WorkflowResultSpec | None,
    *,
    status: Literal["completed", "failed", "stopped"],
    facts: Sequence[WorkItemFact],
    primary: MapBatch | None,
    decisions: Sequence[GateDecisionFact],
    reviews: Mapping[str, GateReview],
    artifacts: Sequence[ArtifactRef] = (),
    exhausted: RetryFeedback | None = None,
) -> Outcome:
    """Classify what one run did, from its own facts alone.

    Args:
        result: What the workflow declared a caller may select. `None` is a workflow that declared
            nothing, which can only be `unknown`.
        status: How the execution ended, as the observer saw it.
        facts: Every disposition this run settled, of every generation. Earlier generations are
            audit and are never counted.
        primary: The input the primary work is currently on, and the identities it owes a
            disposition. `None` when the primary never took one.
        decisions: Every decision every gate received, in the order they arrived.
        reviews: Gate id -> what that gate's own `reviews` wire resolves to in final state, and
            which reach it is on. A required gate missing here can pass nothing.
        artifacts: The references minted over this run's fixed values.
        exhausted: The rejection that spent a gate's last reach, when one ended the run. Once the
            current generation has settled, the run's outcome is then `failed`.

    Returns:
        The checked outcome. `unknown` wherever the facts do not settle the question.
    """
    minted = tuple(artifacts)
    if result is None:
        return Outcome(status="unknown", summary=_NO_RESULT, artifacts=minted)
    checks = tuple(
        _check(gate, decisions=decisions, review=reviews.get(gate.gate_id), exhausted=exhausted) for gate in result.required_gates
    )
    if primary is None:
        return Outcome(status="unknown", summary=_NO_PRIMARY, checks=checks, artifacts=minted)
    seen = _Seen(primary_input=len(primary.items), generation=primary.generation, checks=checks, artifacts=minted)
    settled = _settled(facts, node_id=result.primary_work, generation=primary.generation)
    if settled is None:
        return seen.outcome("unknown", _CONTRADICTED)
    counted = seen.counting(settled.values())
    if set(primary.items) - set(settled):
        return counted.outcome("unknown", _INCOMPLETE)
    return _judged(result, status=status, seen=counted, exhausted=exhausted)


def _judged(
    result: WorkflowResultSpec,
    *,
    status: Literal["completed", "failed", "stopped"],
    seen: _Seen,
    exhausted: RetryFeedback | None,
) -> Outcome:
    """The answer once every piece of the current generation has settled."""
    if exhausted is not None:
        return seen.outcome("failed", _refused(exhausted))
    if status != "completed":
        return seen.outcome("unknown", _UNFINISHED)
    if any(check.passed is not True for check in seen.checks):
        return seen.outcome("unknown", _UNACCEPTED)
    return seen.outcome(_classify(result, seen), _said(result, seen))


@dataclass(frozen=True)
class _Seen:
    """What the classifier has established so far, so every exit builds the same shaped answer."""

    primary_input: int
    generation: int
    checks: tuple[OutcomeCheck, ...]
    artifacts: tuple[ArtifactRef, ...]
    attempted: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    retry_attempts: int = 0

    def counting(self, settled: Iterable[WorkItemFact]) -> _Seen:
        """The same record with this generation's dispositions folded in."""
        facts = list(settled)
        return replace(
            self,
            attempted=len(facts),
            succeeded=sum(1 for fact in facts if fact.disposition == "succeeded"),
            skipped=sum(1 for fact in facts if fact.disposition == "skipped"),
            failed=sum(1 for fact in facts if fact.disposition == "failed"),
            retry_attempts=sum(fact.retry_attempts for fact in facts),
        )

    def outcome(self, status: OutcomeStatus, summary: str) -> Outcome:
        """This record as the answer, under the status and words the classifier chose."""
        return Outcome(
            status=status,
            summary=summary,
            attempted=self.attempted,
            succeeded=self.succeeded,
            skipped=self.skipped,
            failed=self.failed,
            retry_attempts=self.retry_attempts,
            primary_input=self.primary_input,
            generation=self.generation,
            checks=self.checks,
            artifacts=self.artifacts,
        )


def _classify(result: WorkflowResultSpec, seen: _Seen) -> OutcomeStatus:
    """What the settled dispositions of a completed run add up to."""
    if seen.attempted == 0:
        return "noop" if result.empty_primary == "noop" else "unknown"
    if seen.succeeded == seen.attempted:
        return "succeeded"
    if seen.succeeded == 0:
        return "failed"
    return "partial"


def _said(result: WorkflowResultSpec, seen: _Seen) -> str:
    """The classification in words, always naming what was left out."""
    if seen.attempted == 0:
        if result.empty_primary == "noop":
            return "There was nothing to do, and this workflow says that is a result."
        return "The primary work was handed nothing, and this workflow does not say that is a result."
    return f"{seen.succeeded} of {seen.attempted} pieces of the primary work succeeded ({seen.skipped} skipped, {seen.failed} failed)."


def _refused(exhausted: RetryFeedback) -> str:
    """A spent ceiling in words: who refused what, and what they asked for."""
    return (
        f"Gate {exhausted.gate_id!r} refused this run's last reach and {exhausted.target_node!r} had no repair left. "
        f"What was asked for: {exhausted.requested_repair}"
    )


def _settled(facts: Sequence[WorkItemFact], *, node_id: str, generation: int) -> dict[str, WorkItemFact] | None:
    """This generation's dispositions by item key, or `None` when two facts for one identity disagree."""
    settled: dict[str, WorkItemFact] = {}
    for fact in facts:
        if fact.node_id != node_id or fact.generation != generation:
            continue
        standing = settled.get(fact.item_key)
        if standing is not None and standing.disposition != fact.disposition:
            return None
        if standing is None or fact.retry_attempts >= standing.retry_attempts:
            settled[fact.item_key] = fact
    return settled


def _check(
    gate: RequiredGateCheck, *, decisions: Sequence[GateDecisionFact], review: GateReview | None, exhausted: RetryFeedback | None
) -> OutcomeCheck:
    """Ask one required gate whether the value the run ended holding was accepted, and by whom."""
    gate_id, expected = gate.gate_id, gate.accepted_sources
    if exhausted is not None and exhausted.gate_id == gate_id:
        return OutcomeCheck(
            id=gate_id,
            passed=False,
            expected_sources=expected,
            observed_source=exhausted.source,
            review_generation=exhausted.attempt,
            reviewed_value_digest=exhausted.reviewed.value_digest,
            feedback=exhausted.feedback,
            requested_repair=exhausted.requested_repair,
            reviewed=exhausted.reviewed,
            detail=f"{exhausted.source} refused this gate's last reach.",
        )
    if review is None or review.reviewed is None:
        return OutcomeCheck(
            id=gate_id,
            passed=False,
            expected_sources=expected,
            observed_source="unknown",
            detail="Nothing this gate reviews can be read back, so no acceptance of it can be checked.",
        )
    latest = _latest(decisions, gate_id=gate_id, generation=review.generation)
    if latest is None:
        return OutcomeCheck(
            id=gate_id,
            passed=False,
            expected_sources=expected,
            observed_source="unknown",
            reviewed=review.reviewed,
            detail="This gate received no decision over the value the run ended holding.",
        )
    accepted = latest.verdict == "approve" and latest.source in expected and latest.reviewed_value_digest == review.reviewed.value_digest
    return OutcomeCheck(
        id=gate_id,
        passed=accepted,
        expected_sources=expected,
        observed_source=latest.source,
        review_generation=latest.review_generation,
        reviewed_value_digest=latest.reviewed_value_digest,
        reviewed=review.reviewed,
        detail=_decided(latest, accepted=accepted, expected=expected),
    )


def _decided(fact: GateDecisionFact, *, accepted: bool, expected: tuple[AcceptedSource, ...]) -> str:
    """One decision in words, saying which way the check fell and why."""
    if accepted:
        return f"{fact.source} accepted it on reach {fact.review_generation}."
    if fact.verdict != "approve":
        return f"{fact.source} answered {fact.verdict!r} on reach {fact.review_generation}."
    if fact.source not in expected:
        return f"{fact.source} released it, and this result accepts only: {', '.join(expected) or 'no reviewer'}."
    return "The acceptance on file is of other bytes than the ones the run ended holding."


def _latest(decisions: Sequence[GateDecisionFact], *, gate_id: str, generation: int) -> GateDecisionFact | None:
    """The last decision this gate received over its current reach, which after an escalation is the one that answered."""
    matching = [fact for fact in decisions if fact.gate_id == gate_id and fact.review_generation == generation]
    return matching[-1] if matching else None
