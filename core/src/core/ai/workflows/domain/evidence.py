"""Evidence of what running a workflow did, and the trust level it earns, with no model in the verdict.

`RunEvidence` records what mechanically happened, read off run state and never self-reported.
An `Approval` is a person approving a workflow, and a `Grade` is one run's measured outcome.

`trust_level` folds a workflow's events into counts, and the level is derived from them, never
stored. Events are keyed on `spec_hash`, the workflow's content address, never on a library id,
which can change. `workflow_id` stays the only handle on records that carry no hash.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.ai.workflows.domain.outcome import Outcome
from core.ai.workflows.domain.parameter_values import WorkflowArguments
from core.cqrs.contracts import utcnow

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from core.ai.workflows.domain.vocabulary import Workflow


def require_spec_identity(spec_hash: str) -> None:
    """Refuse the empty hash as a question about a workflow.

    `""` marks a record that does not know what it ran, so asking about it would match every such
    record. A caller passing a record's own `spec_hash` back in meets it on a record written
    without a hash.

    Args:
        spec_hash: The content address a caller is about to ask about.

    Raises:
        ValueError: When `spec_hash` is empty.
    """
    if not spec_hash:
        msg = "an empty spec hash is not an identity. It marks a record that never said what it exercised"
        raise ValueError(msg)


class RunEvidence(BaseModel):
    """One run's structural outcome. The observer that watched it is the writer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["run"] = "run"
    workflow_id: str
    """Where the run was launched from: the library's address for the workflow at that moment.

    Provenance, never the key. An address belongs to one library and can change, so a lookup keys
    on `spec_hash`."""
    spec_hash: str = ""
    """The content address of the workflow this run exercised, `spec_identity(workflow)`.

    `""` means the record does not know what it ran, and a record written without a hash still
    parses. A lookup keeps such records apart from confirmed ones and never folds them into a level.

    A content address says what ran, not whose it was. Two teams writing byte-identical workflows
    share an identity, so in a shared evidence log one team's runs count for the other's."""
    workflow_version: int | None = None
    """Which version of the workflow this run exercised, where the runner knew.

    For a record carrying no hash, it answers what `spec_hash` would. Without either, an adapted
    child saved under its parent's address would inherit the parent's runs."""
    outcome: Literal["completed", "failed", "stopped"]
    """How the execution ended. `stopped` is a person ending the run on purpose, and confirms
    nothing, exactly like `failed`. `completed` says the graph reached its end, never that the work
    was done. `result` answers that."""
    result: Outcome | None = None
    """What the run's own facts say it did, classified when it ended.

    `None` means this record cannot say, as on a record written without a checked outcome. It is
    never filled in afterwards."""
    gates_approved: tuple[str, ...] = ()
    """Gates a person released the value of, read off run state and never self-reported."""
    gates_auto: tuple[str, ...] = ()
    """Gates passed automatically, releasing their value without a person deciding.

    Never a `gates_approved` entry. `trust_level` ignores this field, because a gate passed
    automatically is not an approval."""
    gates_orchestrated: tuple[str, ...] = ()
    """Orchestrator gates the host's own agent approved, read off run state and never self-reported.

    `trust_level` never reads this field. An orchestrator clearing a gate the workflow said it
    would clear is evidence, and still not a person's approval. The run itself counts like any
    other completed run. `gates_rejected` counts rejections by gate id, whoever refused."""
    gates_rejected: dict[str, int] = Field(default_factory=dict)
    """How many times a person refused each gate before this run ended, by gate id.

    Recorded by the gate wiring, never by the model. A gate that timed out counts here too. Empty
    means no gate was refused, or the runner did not record refusals, and the two read the same."""
    params: WorkflowArguments = Field(default_factory=dict)
    """What this run was given, as the JSON each parameter declares. A replay runs on these
    inputs. A record whose arguments were all text still parses, and still reads as that text."""
    produced: dict[str, str] = Field(default_factory=dict)
    """The shape of what each node produced, by node id, never the value itself."""
    runner: str = ""
    """Who ran it, in provenance terms (`app:studio`, `service:nightly`)."""
    at: datetime = Field(default_factory=utcnow)


class Approval(BaseModel):
    """A person approved the workflow. The author is the point."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["approval"] = "approval"
    workflow_id: str
    """Where the workflow was kept when it was approved. Provenance, and `spec_hash` is the key."""
    spec_hash: str = ""
    """The content address of the workflow that was approved, `spec_identity(workflow)`.

    `""` means this approval cannot say which workflow it was about, as on a record written
    without a hash."""
    workflow_version: int | None = None
    """Which version was approved. Approving v1 is not approving what v1 became."""
    by: str
    at: datetime = Field(default_factory=utcnow)


class Grade(BaseModel):
    """One run's measured outcome: evidence of quality, beside evidence of completion.

    Who judged is part of the evidence. Which judge types move a level is the owner's policy,
    never this module's. Only `structural` grades have a producer, and the other types parse when
    one is written.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["grade"] = "grade"
    workflow_id: str
    """Where the graded run was launched from. Provenance, exactly as on `RunEvidence`."""
    spec_hash: str = ""
    """The content address of the workflow the graded run exercised. `""` means unknown, not unanimous."""
    workflow_version: int | None = None
    """Which version the graded run exercised, where the grader knew. `None` cannot count for a
    particular version and is excluded from a version-scoped fold."""
    judge_kind: Literal["structural", "llm-judge", "human"]
    judge: str
    """The judge's signature: `recall/groundtruth-research-v1`, a model id + rubric hash, a person."""
    measure: str
    """What was measured: `evidence-recall`."""
    score: float = Field(ge=0.0, le=1.0)
    graded_run_at: datetime | None = None
    """The `at` of the RunEvidence this grade is about, where the grader knew it."""
    at: datetime = Field(default_factory=utcnow)


class TrustLevel(BaseModel):
    """What the evidence log adds up to for one workflow. The level is derived, never assigned.

    The record holds the grades, but `level` reads none of them. Completion is not quality, and which
    judge types may move a level belongs to whoever owns that policy.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs_with_evidence: int = 0
    """Completed runs whose checked outcome confirmed the work happened. The only count a level reads."""
    unconfirmed_runs: int = 0
    """Completed runs whose checked outcome confirmed nothing: unknown, partial, or no result
    recorded. They are not evidence the workflow works."""
    failed_runs: int = 0
    last_evidence_at: datetime | None = None
    """When this workflow last produced evidence: the most recent completed run whose checked
    outcome confirmed. A completed run that confirmed nothing does not advance it, so this is the
    age of the confirmation, never the age of the last run."""
    approved_by: tuple[str, ...] = ()
    structural_grades: int = 0
    """How many grades with `judge_kind="structural"` folded into this record. No other judge
    type is counted."""
    structural_mean: float | None = None
    """The mean structural score, or `None` when nothing has graded this workflow, never a mean
    of zero."""
    structural_last: float | None = None
    """The most recent structural score by `at`."""

    @property
    def level(self) -> Literal["unverified", "machine-confirmed", "approved"]:
        """The trust level these counts earn: approved, machine-confirmed or unverified."""
        if self.approved_by:
            return "approved"
        if self.runs_with_evidence:
            return "machine-confirmed"
        return "unverified"


def trust_level(
    events: Iterable[RunEvidence | Approval | Grade], *, version: int | None = None, spec_hash: str | None = None
) -> TrustLevel:
    """Fold one workflow's events into the record its trust level derives from.

    Each filter keeps only events about that exact workflow and drops an event that cannot say.
    `spec_hash` needs no `version` beside it, because a version bump changes the hash.

    Args:
        events: Whatever the evidence log offered for this workflow.
        version: Ask about one version. An event that did not record which version it exercised
            is excluded. Omit it to ask whether the workflow has ever run successfully.
        spec_hash: Ask about one exact workflow, by content address. An event carrying no hash is
            excluded.

    Returns:
        The counts, grades and approvals the level is derived from.

    Raises:
        ValueError: When `spec_hash` is empty.
    """
    if spec_hash is not None:
        require_spec_identity(spec_hash)
        events = [event for event in events if event.spec_hash == spec_hash]
    if version is not None:
        events = [event for event in events if event.workflow_version == version]
    completed = unconfirmed = failed = 0
    last: datetime | None = None
    approved: list[str] = []
    structural: list[tuple[datetime, float]] = []
    for event in events:
        match event:
            case RunEvidence(outcome="completed") if _is_confirmed(event.result):
                completed += 1
                last = event.at if last is None or event.at > last else last
            case RunEvidence(outcome="completed"):
                unconfirmed += 1
            case RunEvidence(outcome="failed" | "stopped"):
                failed += 1
            case Approval():
                approved.append(event.by)
            case Grade(judge_kind="structural"):
                structural.append((event.at, event.score))
            case Grade():
                pass  # nothing counts a judge type other than structural
    return TrustLevel(
        runs_with_evidence=completed,
        unconfirmed_runs=unconfirmed,
        failed_runs=failed,
        last_evidence_at=last,
        approved_by=tuple(approved),
        structural_grades=len(structural),
        structural_mean=sum(score for _, score in structural) / len(structural) if structural else None,
        structural_last=max(structural, key=lambda scored: scored[0])[1] if structural else None,
    )


def _is_confirmed(result: Outcome | None) -> bool:
    """Whether a completed run's checked outcome says the declared work happened: `succeeded` or `noop`."""
    return result is not None and result.status in ("succeeded", "noop")


def approved_gates(workflow: Workflow, values: Mapping[str, object]) -> tuple[str, ...]:
    """Which of the workflow's person gates released their value in this run's state.

    An approved gate wrote its pass-through value under its own id, and a rejected or waiting one
    wrote nothing. Judge and orchestrator gates are left out, because neither is a person.

    Args:
        workflow: The workflow the run executed.
        values: The run's state, each node's value by node id.

    Returns:
        The ids of the approved person gates, in node order.
    """
    return tuple(node.id for node in workflow.nodes if node.kind == "gate" and node.id in values)


def orchestrated_gates(workflow: Workflow, values: Mapping[str, object]) -> tuple[str, ...]:
    """Which of the workflow's orchestrator gates released their value in this run's state.

    It reads the run state by the same rule as `approved_gates`, and fills
    `RunEvidence.gates_orchestrated`.

    Args:
        workflow: The workflow the run executed.
        values: The run's state, each node's value by node id.

    Returns:
        The ids of the approved orchestrator gates, in node order.
    """
    return tuple(node.id for node in workflow.nodes if node.kind == "orchestrator-gate" and node.id in values)


class EvidenceHistory(BaseModel):
    """What an evidence log holds about one workflow, split by what can be confirmed as its own.

    A record that carries no content address is kept apart in `legacy`, never merged into
    `matched` and never dropped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    matched: tuple[RunEvidence | Approval | Grade, ...] = ()
    """Events carrying this workflow's content address, wherever they were filed. A level may be
    folded from this bucket only, and a workflow keeps these runs when it moves."""
    legacy: tuple[RunEvidence | Approval | Grade, ...] = ()
    """Events at the address asked about carrying no content address. They may be this workflow's,
    an adapted child's, or a namesake's, and nothing here can tell. They are reported, never counted."""

    @property
    def basis(self) -> Literal["content", "address-only", "none"]:
        """What a level folded from `matched` would rest on.

        `content`: something carries this workflow's hash. `address-only`: nothing does, but the
        address holds history nobody can attribute, so `unverified` means unconfirmed rather than
        never run. `none`: nothing is recorded either way.
        """
        if self.matched:
            return "content"
        return "address-only" if self.legacy else "none"


