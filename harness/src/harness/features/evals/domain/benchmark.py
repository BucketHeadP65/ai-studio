"""The post-pilot benchmark's value types and rules: what it is refused on, and what it writes down."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Self

from core.ai.evaluation.contracts import (
    BudgetRefusal,
    BudgetRole,
    EvaluationLimits,
    EvaluationModel,
    Fingerprint,
    Nonempty,
    OtherCostObservation,
    ResourceUsage,
    StateRootIdentity,
    TaskGrade,
    Variant,
    content_fingerprint,
)
from core.fingerprints import content_of
from engram.evals.generation.contracts import CaseFreshness, CasePremise
from engram.evals.outcomes.contracts import CaseSeal, LabelExposure, MethodologyAudit
from pydantic import AwareDatetime, Field, model_validator

from harness.features.evals.domain.pilot import PilotParticipant

if TYPE_CHECKING:
    from core.ai.evaluation.contracts import RunIdentity
    from core.ai.workflows.contracts import OutcomeStatus


type OutcomeObjective = Literal["task-success", "measured-generative-model-cost"]
"""What a benchmark is asked to establish.

`task-success` reads the graded outcomes. `measured-generative-model-cost` reads the charges the
record actually settled, which is measured generative-model responses and nothing else: embedding
and third-party charges are separate observations and the rest of an invoice is not visible here.
"""

type EmpiricalStatus = Literal["blocked", "measured", "inconclusive"]
"""What a benchmark report may be read as.

`blocked` is nothing measured and no claim of any kind. `inconclusive` is something measured and
not enough of it to answer with. `measured` is every planned slot graded with no unresolved charge.
"""

type BenchmarkRefusal = Literal[
    "development_dataset_only",
    "custody_absent",
    "custody_unverified",
    "limits_absent",
    "pricing_unknown",
    "unmetered_model_path",
    "unbudgeted_model_path",
    "unverified_state_root",
    "no_cooperative_lock",
    "remote_temporal_worker",
    "identity_drift",
    "arms_identical",
    "no_declared_model_path",
    "no_declared_grader_path",
    "undeclared_model_path",
    "unstated_usage_source",
    "unrecorded_model_requests",
]
"""Why a benchmark stopped before it admitted anything. Each one is a rule, never an opinion."""

type BenchmarkModelType = Literal["live", "test_double", "unbuilt"]
"""What the measurement ran on.

`unbuilt` is a benchmark refused before any model was composed, which is not the same as one that
ran on stand-ins. A `test_double` spends nothing, so its figures are apparatus output rather than
evidence about a deployment. Only `live` reaches a provider.
"""

type UsageSource = Literal["this_record", "another_record"]
"""Where reported usage comes from, which decides whether this record can check it at all.

`this_record` means the requests are admitted here, so what is reported can be held against what the
record admitted. `another_record` means the figures were measured by some other record, such as a
runner reading a pilot's archived runs, and nothing here can check those against anything. That is an
exemption, so a participant always states its source and nothing infers it.
"""

type BenchmarkPosition = Literal["baseline", "candidate", "grader"]
"""The three positions a benchmark fills. Every model request of a sitting is made from one of them."""


class BenchmarkRefusalNote(EvaluationModel):
    """One rule that stopped a benchmark, named by the rule and by what it was looking at."""

    refusal: BenchmarkRefusal
    detail: Nonempty


class BenchmarkInputs(EvaluationModel):
    """The exact content every fingerprint of a benchmark is taken over.

    Each field is content, never a name for content. Two benchmarks whose `prompt` differs by a word
    have different prompt fingerprints, and a reader can see why.
    """

    workflow: Nonempty
    prompt: Nonempty
    model: Nonempty
    model_version: Nonempty
    tools: tuple[Nonempty, ...] = ()
    sources: tuple[Nonempty, ...] = ()
    grader: Nonempty


class BenchmarkFreeze(EvaluationModel):
    """Everything fixed before a benchmark may admit its first request.

    `limits` is `None` only for a benchmark whose every model is a stand-in. A live model with no
    limits is refused rather than frozen.
    """

    workflow_fingerprint: Fingerprint
    prompt_fingerprint: Fingerprint
    model: Nonempty
    model_version: Nonempty
    toolset_fingerprint: Fingerprint
    source_snapshot_fingerprint: Fingerprint
    grader_fingerprint: Fingerprint
    shared_budget_root: StateRootIdentity
    limits: EvaluationLimits | None = None


def freeze_benchmark(inputs: BenchmarkInputs, *, root: StateRootIdentity, limits: EvaluationLimits | None) -> BenchmarkFreeze:
    """Hash each part of a benchmark over its own actual content, and name the root it measures on.

    The toolset and the source snapshot are hashed over their sorted sets, so the same tools and the
    same sources in another order are the same benchmark.

    Args:
        inputs: The exact content to hash.
        root: The shared budget root the benchmark measures on.
        limits: The stop thresholds, or `None` for a benchmark whose every model is a stand-in.

    Returns:
        The frozen benchmark.
    """
    return BenchmarkFreeze(
        workflow_fingerprint=content_fingerprint(inputs.workflow),
        prompt_fingerprint=content_fingerprint(inputs.prompt),
        model=inputs.model,
        model_version=inputs.model_version,
        toolset_fingerprint=content_fingerprint(content_of(inputs.tools)),
        source_snapshot_fingerprint=content_fingerprint(content_of(inputs.sources)),
        grader_fingerprint=content_fingerprint(inputs.grader),
        shared_budget_root=root,
        limits=limits,
    )


def benchmark_cycle_id(freeze: BenchmarkFreeze, dataset_fingerprint: str) -> str:
    """The one cycle this exact benchmark spends under, derived from the benchmark itself.

    The same benchmark opened again is the same cycle, so the cycle stop threshold bounds the
    benchmark rather than one sitting of it. Change the workflow, the prompt, the model, the tools,
    the sources, the grader, the limits or a case, and it is a different benchmark with a cycle and
    a threshold of its own.

    Args:
        freeze: The frozen benchmark.
        dataset_fingerprint: The fingerprint of the cases it reads.

    Returns:
        The cycle id: `wa-benchmark-` then 32 hex characters.
    """
    return f"wa-benchmark-{content_fingerprint(f'{freeze.model_dump_json()}\n{dataset_fingerprint}')[:32]}"


class DevelopmentReplay(EvaluationModel):
    """The author-visible development set, read back so a reader can see what it does not establish."""

    kind: Literal["development_replay"] = "development_replay"
    dataset_path: Path
    stamp_path: Path


class SealedComparison(EvaluationModel):
    """A custodian's own cases and the custody record filed beside them."""

    kind: Literal["sealed_comparison"] = "sealed_comparison"
    dataset_path: Path
    custody_path: Path


type BenchmarkCases = Annotated[DevelopmentReplay | SealedComparison, Field(discriminator="kind")]
"""Which cases a benchmark reads. There is no third answer and no default."""


class BenchmarkCustody(EvaluationModel):
    """What an independent custodian files beside sealed cases, so the audit has something to check.

    `implementation_authors` is every actor with implementation knowledge, and an empty list is
    refused.
    """

    custodian: Nonempty
    implementation_frozen_at: AwareDatetime
    implementation_authors: tuple[Nonempty, ...] = Field(min_length=1)
    seals: tuple[CaseSeal, ...] = ()
    exposures: tuple[LabelExposure, ...] = ()
    freshness: tuple[CaseFreshness, ...] = ()
    premises: tuple[CasePremise, ...] = ()


class BenchmarkTrialRecord(EvaluationModel):
    """One slot as it was persisted, before any verdict was folded over it.

    A refusal and a failure are recorded here as facts of the slot. Neither is a grade and neither
    is ever folded into one. `grade` is what this slot was graded, after the candidate's checked
    status has been read against it, and it is `None` for a slot that ended before a grade.
    """

    case_id: Nonempty
    variant: Variant
    repetition: int = Field(ge=0)
    trial_id: Nonempty
    run_id: str = ""
    requests: int = Field(default=0, ge=0)
    attempts: int = Field(default=0, ge=0)
    measured_model_spent_usd: Decimal = Field(default=Decimal(0), ge=0)
    measured_model_overshoot_usd: Decimal = Field(default=Decimal(0), ge=0)
    unresolved_charge: bool = False
    refusal: BudgetRefusal | None = None
    failure: str = ""
    grade: TaskGrade | None = None


class DeclaredModelPath(EvaluationModel):
    """One path a benchmark declared, and whether this record could see it.

    `budgeted` is what the classifier found by looking at the model object, so a report names the
    roles the record actually admitted for rather than the roles somebody meant it to.
    """

    path: Nonempty
    role: BudgetRole
    budgeted: bool


class UsageDeclaration(EvaluationModel):
    """What one of the three positions said about itself: the paths it asks through, and whose record measured it.

    One reporting `another_record` is exempt from the check that holds its own figures against this
    record, and a reader can see exactly which one that was.
    """

    made_by: BenchmarkPosition
    declared_paths: tuple[Nonempty, ...] = ()
    usage_source: UsageSource


class OwnerAcceptanceRecord(EvaluationModel):
    """What an owner decided about one measured run, kept apart from how the run was graded.

    `unknown` is a run nobody reviewed. It never reads as either of the other two.
    """

    case_id: Nonempty
    run_id: Nonempty
    acceptance: Literal["accepted", "rejected", "unknown"]
    reviews: int = Field(default=0, ge=0)


class OutcomeStateReport(EvaluationModel):
    """What one benchmark command established, written once under its own name and never rewritten.

    `empirical_status` is the whole of what a reader may take from it, and `model_kind` says what it
    ran on: a `measured` report over `test_double` models is apparatus output, not evidence about a
    deployment, and `unbuilt` is a benchmark refused before any model existed. `recommendation` is
    always `none`: this command recommends nothing.

    `verdict`, `verdict_reasons`, both success rates, `regressions` and both usages are what the
    paired comparison concluded over every graded slot. A blocked report carries none of them, and a
    report that ran carries all of them.
    """

    report_id: Nonempty
    objective: OutcomeObjective
    empirical_status: EmpiricalStatus
    model_kind: BenchmarkModelType
    started_at: AwareDatetime
    dataset: Nonempty
    dataset_version: Nonempty
    dataset_fingerprint: Fingerprint
    freeze: BenchmarkFreeze
    custody: MethodologyAudit | None = None
    declared_model_paths: tuple[DeclaredModelPath, ...] = ()
    usage_declarations: tuple[UsageDeclaration, ...] = ()
    refusals: tuple[BenchmarkRefusalNote, ...] = ()
    trials: tuple[BenchmarkTrialRecord, ...] = ()
    owner_acceptance: tuple[OwnerAcceptanceRecord, ...] = ()
    graded_case_ids: tuple[Nonempty, ...] = ()
    ungraded_case_ids: tuple[Nonempty, ...] = ()
    cycle_measured_model_spent_usd: Decimal | None = None
    cycle_measured_model_overshoot_usd: Decimal = Field(default=Decimal(0), ge=0)
    unresolved_charge: bool = False
    other_costs: tuple[OtherCostObservation, ...] = ()
    verdict: Literal["accepted", "rejected"] | None = None
    verdict_reasons: tuple[str, ...] = ()
    baseline_success_rate: float | None = None
    candidate_success_rate: float | None = None
    regressions: tuple[Nonempty, ...] = ()
    baseline_usage: ResourceUsage | None = None
    candidate_usage: ResourceUsage | None = None
    statistical_claim: Literal["descriptive_only"] = "descriptive_only"
    recommendation: Literal["none"] = "none"

    @model_validator(mode="after")
    def blocked_means_nothing_ran(self) -> Self:
        """Refuse a report that says it was blocked while carrying trials, or measured while carrying none.

        Returns:
            The report, unchanged.

        Raises:
            ValueError: a blocked report carries trials, a verdict or no refusal, or a report that ran
                carries no trials or no verdict.
        """
        if self.empirical_status == "blocked" and self.trials:
            msg = "a blocked report records no trials, and these trials say something ran"
            raise ValueError(msg)
        if self.empirical_status != "blocked" and not self.trials:
            msg = "a report that is not blocked records the trials it read, and this one records none"
            raise ValueError(msg)
        if self.empirical_status == "blocked" and not self.refusals:
            msg = "a blocked report names the rule that blocked it"
            raise ValueError(msg)
        if (self.empirical_status == "blocked") != (self.verdict is None):
            msg = "a report that ran carries the comparison's verdict, and a blocked one carries none"
            raise ValueError(msg)
        return self

    @property
    def paid_benchmark_performed(self) -> bool:
        """Whether real money moved: live models, and at least one request the record admitted."""
        return self.model_kind == "live" and any(row.requests for row in self.trials)


def comparability_drift(freeze: BenchmarkFreeze, *, baseline: RunIdentity, candidate: RunIdentity) -> tuple[str, ...]:
    """Which facts a paired comparison holds equal are not equal here, named one by one.

    This refuses nothing. It names what drifted, so a refusal can be attributed to a field rather
    than reported as a comparison that did not happen.

    Args:
        freeze: The frozen benchmark both arms must match.
        baseline: The baseline arm's identity.
        candidate: The candidate arm's identity.

    Returns:
        One line per drifted fact, empty when nothing drifted.
    """
    drifted: list[str] = []
    if baseline.model != candidate.model:
        drifted.append(f"model: baseline {baseline.model}, candidate {candidate.model}")
    if baseline.model_version != candidate.model_version:
        drifted.append(f"model version: baseline {baseline.model_version}, candidate {candidate.model_version}")
    if baseline.conditions_fingerprint != candidate.conditions_fingerprint:
        drifted.append("execution conditions differ between the arms")
    if baseline.grader_fingerprint != candidate.grader_fingerprint:
        drifted.append("grader differs between the arms")
    for name, identity in (("baseline", baseline), ("candidate", candidate)):
        if identity.model != freeze.model or identity.model_version != freeze.model_version:
            drifted.append(f"{name} runs {identity.model} {identity.model_version} and the seal froze {freeze.model} {freeze.model_version}")
        if identity.grader_fingerprint != freeze.grader_fingerprint:
            drifted.append(f"{name} grader is not the grader the seal froze")
    return tuple(drifted)


class BenchmarkRequest(EvaluationModel):
    """One whole benchmark as the person starting it names it.

    `limits` is what the command's own flags carried. A benchmark whose models reach a provider is
    refused without them, and one running entirely on stand-ins does not need them.
    """

    objective: OutcomeObjective
    cases: BenchmarkCases
    inputs: BenchmarkInputs
    report_root: Path
    limits: EvaluationLimits | None = None
    participants: tuple[PilotParticipant, ...] = Field(min_length=1)
    run_engine: Literal["in_process", "temporal"] = "in_process"
    temporal_address: Nonempty = "localhost:7233"
    repetitions: int = Field(default=2, ge=2)


def checked_grade(grade: TaskGrade, *, variant: Variant, checked: OutcomeStatus | None) -> TaskGrade:
    """The grade a slot keeps once the candidate run's own checked status is read against it.

    A candidate run passes only when its checked outcome says `succeeded`. A pass over a candidate
    run with any other status, `noop` included, or with no checked outcome at all becomes a fail
    naming that status. A fail is never turned into a pass, and a baseline grade stays as the grader
    gave it.

    Args:
        grade: What the grader gave the slot.
        variant: Which arm the slot ran.
        checked: The candidate run's checked status, or `None` when it has none.

    Returns:
        The grade the slot keeps.
    """
    if variant == "baseline" or not grade.success or checked == "succeeded":
        return grade
    status = checked or "absent"
    return TaskGrade(
        success=False,
        reason=f"the run's checked status is {status}, and only a succeeded run passes. The grader had said: {grade.reason}",
        usage=grade.usage,
    )


def unrecorded_requests(*, who: str, reported: int, admitted: int, reads_this_record: bool) -> str:
    """Why this slot is refused after the fact, or an empty string when the two counts agree.

    A reporter claiming more requests than the record admitted made some of them where the record
    cannot see. This detects that and prevents nothing: those requests were already made, unmetered,
    and no trial limit bounded them. The refusal stops the run at the one slot it fires on. It reads
    one direction only, so reporting fewer requests than were made passes, including reporting none.

    Args:
        who: Who made the requests, as the refusal names them, such as "the grader".
        reported: How many model requests the participant reports.
        admitted: How many model requests the record admitted for it.
        reads_this_record: Whether the participant says its usage comes from this record. When it
            does not, nothing is checked.

    Returns:
        The refusal's detail, or an empty string when there is nothing to refuse.
    """
    if not reads_this_record or reported <= admitted:
        return ""
    return f"{who} reports {reported} model request(s) and this record admitted {admitted}, so some of them were made where nothing could meter them"
