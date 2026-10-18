"""A live pilot's value types and rules: its limits, its preflight, its trials, its seal and its handoff.

`preflight` is what a live trial is checked against before it runs: every generative model path is
budgeted, every participating process verifies the same POSIX device and canonical root, and every
configured role's model has a price the same resolver can name. The root and device are the
record's own, read off it rather than supplied beside it, so what the participants were checked
against is what the cycle opens on. Any of those failing refuses the trial rather than measuring
half of it.

`freeze_pilot` seals what ran, so a later independent evaluation reads a pilot rather than a memory
of one: the model it ran on, the hashes of its prompt, implementation, tools, conditions and grader,
the dataset it ran over, the baseline it is read against, and what it cannot show. `PilotHandoff`
carries that seal beside whatever has actually been measured under it, which is nothing at all until
the pilot runs.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Self

from core.ai.evaluation.contracts import (
    BudgetCoverageManifest,
    BudgetPreflightError,
    BudgetRole,
    EvaluationLimits,
    EvaluationModel,
    Fingerprint,
    Nonempty,
    ResourceUsage,
    ReviewedTaskOutcome,
    RunIdentity,
    TaskExecution,
    content_fingerprint,
    require_full_coverage,
    require_unique,
    total_usage,
)
from core.ai.workflows.contracts import Outcome, WorkflowIntent
from core.fingerprints import content_of
from pydantic import Field, field_validator, model_validator

if TYPE_CHECKING:
    from core.ai.artifacts import ArtifactRef
    from core.ai.evaluation.contracts import BudgetSnapshot
    from core.ai.workflows.contracts import Tier

    from harness.features.evals.domain.outcomes import WorkflowAutomationDataset

WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS = EvaluationLimits(
    cycle_cost_stop_usd=Decimal("10.00"),
    trial_cost_stop_usd=Decimal("2.00"),
    trial_deadline_seconds=180,
    trial_request_limit=16,
    trial_attempt_limit=3,
)
"""The only limits an author-visible workflow-automation pilot may opt into a live run under.

The two dollar figures are accrual stop thresholds over measured generative-model response charges,
not ceilings on the bill and not a promise about what a trial produces.
"""


METERED_ROLE_OF_TIER: dict[Tier, BudgetRole] = {"worker": "worker", "orchestrator": "reviewer", "helper": "designer"}
"""Which budget role each step-agent tier spends under.

The tiers are what a workflow names and the budget roles are what a trial accounts by. A worker does
the work, an orchestrator decides a gate and so spends as a reviewer, and a helper is what a step
reaches for while it is being put together and so spends as a designer. Exhaustive over `Tier`, so a
new tier is a new entry here rather than a role that quietly goes unaccounted.
"""


class PilotRoleModel(EvaluationModel):
    """One configured role of a live trial, as the price resolver has to be able to name it."""

    role: BudgetRole
    model_name: Nonempty
    provider_name: Nonempty


class PilotParticipant(EvaluationModel):
    """One process that will take part in a live trial, and what it can actually verify itself.

    `device_id` is `None` for a process that cannot read the device behind the state root at all,
    which is what a remote worker reports. That is refused rather than assumed to match.
    """

    name: Nonempty
    canonical_state_root: Nonempty
    device_id: int | None = None
    cooperative_locking: bool = False


class PilotPreflight(EvaluationModel):
    """Everything a live trial is refused on before it makes its first request.

    `canonical_state_root` and `device_id` are the record's own, read off it by `pilot_preflight`.
    Every participant is checked against them and the cycle is opened on them, so what was verified
    and what is measured cannot be two different places.
    """

    cycle_id: Nonempty
    limits: EvaluationLimits
    canonical_state_root: Nonempty
    device_id: int = Field(ge=0)
    coverage: BudgetCoverageManifest
    participants: tuple[PilotParticipant, ...] = Field(min_length=1)
    roles_without_price: tuple[BudgetRole, ...] = ()

    @property
    def pricing_known(self) -> bool:
        """Whether every configured role's model has a price the resolver can name."""
        return not self.roles_without_price


def check_pilot(preflight: PilotPreflight) -> None:
    """Refuse a live trial that cannot be measured, before any model request is made.

    Args:
        preflight: What the trial is checked against.

    Raises:
        BudgetPreflightError: a generative model path is not budgeted, a participant cannot verify
            the same state root on the same device or cannot lock it, or a configured role's model
            has no resolvable price.
    """
    require_full_coverage(preflight.coverage)
    for participant in preflight.participants:
        if not participant.cooperative_locking:
            msg = f"{participant.name!r} cannot take a cooperative lock on the shared state root, so it cannot take part in a measured trial"
            raise BudgetPreflightError(msg, refusal="no_cooperative_lock")
        verified = participant.device_id == preflight.device_id and participant.canonical_state_root == preflight.canonical_state_root
        if not verified:
            msg = (
                f"{participant.name!r} verifies {participant.canonical_state_root} (device {participant.device_id}), "
                f"and the cycle runs on {preflight.canonical_state_root} (device {preflight.device_id})"
            )
            raise BudgetPreflightError(msg, refusal="unverified_state_root")
    if not preflight.pricing_known:
        msg = f"no price can be resolved for {', '.join(preflight.roles_without_price)}, so the cycle cannot record what it spends"
        raise BudgetPreflightError(msg, refusal="pricing_unknown")


class PilotTrial(EvaluationModel):
    """One case's journey through the Studio, as what it left behind.

    The trial id is the budget scope every model request of that journey was admitted under, so a
    graded outcome and the money it cost can be put side by side afterwards.
    """

    case_id: Nonempty
    repetition: int = Field(ge=0)
    run_id: Nonempty
    trial_id: Nonempty
    intent: WorkflowIntent | None = None


class WorkflowAutomationExecution(TaskExecution):
    """One pilot run as execution left it: what it produced and what it checked, nothing graded."""

    run_id: Nonempty
    trial_id: Nonempty
    result: Outcome | None = None
    intent: WorkflowIntent | None = None


class PilotAdmission(EvaluationModel):
    """The deployment's own roles and processes, as a live pilot is checked against them.

    `role_models` is every model role a measured trial makes requests through. Each one is a
    generative model path and each one is budgeted, because a measured trial mounts a meter on all
    of them. `unbudgeted_model_paths` is where else this deployment reaches a model from, and
    naming one refuses the pilot rather than measuring around it.

    Nothing here says which roles can be priced. That is asked of the price resolver itself, at the
    moment the cycle would open, rather than taken from a caller.
    """

    role_models: tuple[PilotRoleModel, ...] = Field(min_length=1)
    participants: tuple[PilotParticipant, ...] = Field(min_length=1)
    unbudgeted_model_paths: tuple[Nonempty, ...] = ()

    @model_validator(mode="after")
    def one_model_per_role(self) -> Self:
        """Refuse two models under one role, which would leave one of them unpriced and uncounted.

        Returns:
            The admission, unchanged.

        Raises:
            ValueError: two models share a role, or an unbudgeted model path is named twice.
        """
        require_unique((row.role for row in self.role_models), "configured roles")
        require_unique(self.unbudgeted_model_paths, "unbudgeted model paths")
        return self

    @property
    def coverage(self) -> BudgetCoverageManifest:
        """Every generative model path this pilot can take, and which of them the record admits.

        A role's path is named by the role and the model behind it, so two deployments running the
        same roles on different models do not read as the same coverage.
        """
        budgeted = tuple(f"{row.role}:{row.provider_name}:{row.model_name}" for row in self.role_models)
        return BudgetCoverageManifest(
            generative_model_paths=(*budgeted, *self.unbudgeted_model_paths), budgeted_model_paths=budgeted
        )


def trial_usage(snapshot: BudgetSnapshot, recorded: ResourceUsage) -> ResourceUsage:
    """What one trial spent: the record's own requests and charge, beside one run's token counts.

    Requests and cost come from the budget record, which is the thing a measured trial is measured
    by, and they cover the whole trial. Tokens come from one run's own spend record, the only place
    that counts them at all, and they cover that run alone. The trial's `token_source_run`
    diagnostic names the run the tokens are of. A cycle holding any charge nothing could price
    leaves the cost unknown rather than reporting the part that is known.

    Args:
        snapshot: The budget record's figures for the cycle and this trial.
        recorded: The usage one run's own spend record measured.

    Returns:
        The trial's usage.
    """
    return ResourceUsage(
        input_tokens=recorded.input_tokens,
        output_tokens=recorded.output_tokens,
        requests=snapshot.trial_requests,
        cost_usd=None if snapshot.unresolved_charge else float(snapshot.trial_measured_model_spent_usd),
    )


class PilotInputs(EvaluationModel):
    """The exact content every fingerprint of a frozen pilot is taken over.

    Each field is content, never a name for content: two pilots whose `prompt` differs by a word
    have different prompt fingerprints, and a reader can see why.
    """

    model: Nonempty
    model_version: Nonempty
    prompt: Nonempty
    implementation: Nonempty
    tools: tuple[Nonempty, ...] = ()
    configuration: Nonempty
    grader: Nonempty


class PilotBaseline(EvaluationModel):
    """What a pilot's results are read against, stated before the pilot runs.

    `observations` is how many recorded runs stand behind it. `None` is a baseline nobody measured,
    which is a description of what the work used to take and never a figure to compare against.
    """

    description: Nonempty
    observations: int | None = Field(default=None, ge=0)


class FrozenPilot(EvaluationModel):
    """One pilot implementation and its inputs, sealed so a later evaluation can reproduce it.

    `identity` carries the model it ran on and the hashes of its prompt, its implementation, its
    execution conditions and its grader. `tool_fingerprint` is over the tools its agents could hold,
    which conditions do not cover. `limitations` is what this pilot cannot show, written by whoever
    froze it, and it is never empty: a pilot claiming no limitation has not been read.

    `metered_roles` is which budget roles a meter was actually mounted on when the pilot opened, and
    it is the only thing that makes the two dollar figures mean anything. A seal naming none carries
    `unmetered_reason` instead, and its limits are a record of what was asked for rather than of
    anything that was measured.
    """

    cycle_id: Nonempty
    dataset: Nonempty
    dataset_version: Nonempty
    dataset_fingerprint: Fingerprint
    limits: EvaluationLimits
    identity: RunIdentity
    tool_fingerprint: Fingerprint
    baseline: PilotBaseline
    limitations: tuple[Nonempty, ...] = Field(min_length=1)
    metered_roles: tuple[BudgetRole, ...] = ()
    unmetered_reason: str = ""

    @model_validator(mode="after")
    def metering_is_one_answer_or_the_other(self) -> Self:
        """Refuse a seal that both meters and gives a reason not to, or that does neither.

        Returns:
            The seal, unchanged.

        Raises:
            ValueError: the seal names metered roles and a reason, or neither, or names a metered role twice.
        """
        if bool(self.metered_roles) == bool(self.unmetered_reason):
            msg = "a seal either names the roles it metered or the written reason it metered none"
            raise ValueError(msg)
        require_unique(self.metered_roles, "metered roles")
        return self

    @property
    def metered(self) -> bool:
        """Whether anything was measuring while this pilot ran."""
        return bool(self.metered_roles)


class PilotSeal(EvaluationModel):
    """What a seal carries beyond the pilot's own content: what it is read against, and what measured it.

    The same one-answer rule `FrozenPilot` holds itself to, checked here as well so a caller building
    a seal by hand meets it before anything is hashed.
    """

    baseline: PilotBaseline
    limitations: tuple[Nonempty, ...] = Field(min_length=1)
    metered_roles: tuple[BudgetRole, ...] = ()
    unmetered_reason: str = ""

    @model_validator(mode="after")
    def metering_is_one_answer_or_the_other(self) -> Self:
        """Refuse a seal that both meters and gives a reason not to, or that does neither.

        Returns:
            The seal, unchanged.

        Raises:
            ValueError: the seal names metered roles and a reason, or neither.
        """
        if bool(self.metered_roles) == bool(self.unmetered_reason):
            msg = "a seal either names the roles it metered or the written reason it metered none"
            raise ValueError(msg)
        return self


def freeze_pilot(dataset: WorkflowAutomationDataset, inputs: PilotInputs, *, cycle_id: str, seal: PilotSeal) -> FrozenPilot:
    """Seal this pilot's implementation and inputs, hashing each part over its own actual content.

    The conditions hash covers the execution conditions and the development limits together, so a
    pilot reproduced under different stop thresholds is a different pilot and says so. What was
    measuring is recorded beside them rather than hashed, so two sittings of one pilot are the same
    pilot whether or not both were metered.

    Args:
        dataset: The dataset the pilot runs over.
        inputs: The exact content to hash.
        cycle_id: The cycle the pilot spends under.
        seal: What the pilot is read against, and what measured it.

    Returns:
        The frozen pilot.
    """
    limits = WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS
    return FrozenPilot(
        metered_roles=seal.metered_roles,
        unmetered_reason=seal.unmetered_reason,
        cycle_id=cycle_id,
        dataset=dataset.name,
        dataset_version=dataset.version,
        dataset_fingerprint=content_fingerprint(dataset.model_dump_json()),
        limits=limits,
        identity=RunIdentity(
            model=inputs.model,
            model_version=inputs.model_version,
            prompt_fingerprint=content_fingerprint(inputs.prompt),
            implementation_fingerprint=content_fingerprint(inputs.implementation),
            conditions_fingerprint=content_fingerprint(f"{inputs.configuration}\n{limits.model_dump_json()}"),
            grader_fingerprint=content_fingerprint(inputs.grader),
        ),
        tool_fingerprint=content_fingerprint(content_of(inputs.tools)),
        baseline=seal.baseline,
        limitations=seal.limitations,
    )


def pilot_cycle_id(dataset: WorkflowAutomationDataset, inputs: PilotInputs) -> str:
    """The one cycle this exact pilot spends under, derived from the pilot itself.

    A pilot is its cases and its inputs, and nothing else. The same pilot opened again is the same
    cycle, so the cycle stop threshold bounds the pilot rather than one sitting of it. Change a
    case, the prompt, the implementation, the tools or the configuration, and it is a different
    pilot with a cycle and a threshold of its own.

    Args:
        dataset: The dataset the pilot runs over.
        inputs: The pilot's inputs.

    Returns:
        The cycle id: `wa-pilot-` then 32 hex characters.
    """
    return f"wa-pilot-{content_fingerprint(f'{dataset.model_dump_json()}\n{inputs.model_dump_json()}')[:32]}"


class DevelopmentPilotRequest(EvaluationModel):
    """Everything one live pilot needs before it opens: what to run, what it runs as, what it is read against.

    The two paths are the caller's. Nothing here knows where a template keeps its evaluation data.
    """

    dataset_path: Path
    stamp_path: Path
    inputs: PilotInputs
    admission: PilotAdmission
    baseline: PilotBaseline
    limitations: tuple[Nonempty, ...] = Field(min_length=1)


class UnmeteredPilot(EvaluationModel):
    """A pilot the caller has chosen to run with no meter mounted, and the reason they wrote down.

    Nothing about such a run is measured. Its cycle opens and records its thresholds, no request is
    ever admitted against them, and both dollar figures are inert rather than approximate. Running
    unmetered is always asked for in writing. The reason lands in a permanent seal, stripped, and a
    blank one is refused.
    """

    reason: Nonempty

    @field_validator("reason")
    @classmethod
    def written_rather_than_blank(cls, value: str) -> str:
        """The reason as written, stripped, and refused when nothing was written.

        Args:
            value: The reason as the caller gave it.

        Returns:
            The reason, stripped.

        Raises:
            ValueError: the reason is empty or is only whitespace.
        """
        written = value.strip()
        if not written:
            msg = "an unmetered pilot records a reason somebody wrote, and whitespace is not one"
            raise ValueError(msg)
        return written


class PilotHandoff(EvaluationModel):
    """A frozen pilot beside whatever it has actually measured, for an independent evaluation to read.

    `outcomes` is empty until the pilot has run. An empty handoff is a pilot nobody has measured,
    which is what it says, and never a measured result of zero.
    """

    frozen: FrozenPilot
    outcomes: tuple[ReviewedTaskOutcome, ...] = ()

    @property
    def measured(self) -> bool:
        """Whether anything has actually been measured under this frozen pilot."""
        return bool(self.outcomes)

    @property
    def output_refs(self) -> tuple[ArtifactRef, ...]:
        """Every value the measured runs actually fixed, in the order they were graded."""
        return tuple(ref for row in self.outcomes for ref in row.outcome.artifact_refs)

    @property
    def correction_seconds(self) -> float | None:
        """How long people spent correcting the measured runs, or `None` while any row is unknown.

        A row that recorded no correction time is unknown, never zero. A total that folded one in
        would read as work nobody had to do.
        """
        if not self.outcomes:
            return None
        times = tuple(row.outcome.correction_seconds for row in self.outcomes)
        return sum(time for time in times if time is not None) if all(time is not None for time in times) else None

    @property
    def usage(self) -> ResourceUsage:
        """What every measured row is known to have spent, cost unknown while any row's is."""
        return total_usage(row.outcome.usage for row in self.outcomes)
