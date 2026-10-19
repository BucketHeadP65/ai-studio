"""Performing a workflow-automation case by reading the run the pilot journey actually left behind.

A person drives the journey through the Studio, and a runner here reads back the run one case produced.
A runner receives only `EvaluationInput`, a case id and the request, and nothing it returns is graded.
Every label, criterion and expected status stays on the grading side, in `ArchivedRunOutcomeQueries`.
A case whose trial never ran, or whose run is gone from the archive, raises `EvaluationError` and fails its slot.
`start_development_pilot` opens a live pilot's persisted cycle, so spending across processes and restarts adds up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.ai.evaluation.contracts import (
    BudgetConflictError,
    BudgetNotOpenError,
    BudgetPreflightError,
    BudgetScope,
    DiagnosticObservation,
    EvaluationError,
    ResourceUsage,
    StateRootIdentity,
    TaskOutcome,
    cycle_snapshot,
)
from core.cqrs.contracts import utcnow
from pydantic_ai.messages import ModelResponse
from pydantic_ai.usage import RequestUsage

from harness.adapters.contracts import priced_cost
from harness.features.evals.domain.outcomes import evidence_support, structural_validity, task_success, workflow_design
from harness.features.evals.domain.pilot import (
    WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS,
    PilotHandoff,
    PilotPreflight,
    PilotSeal,
    UnmeteredPilot,
    WorkflowAutomationExecution,
    check_pilot,
    freeze_pilot,
    pilot_cycle_id,
    trial_usage,
)
from harness.features.evals.exceptions import UnmeterablePilotError
from harness.features.evals.infrastructure.files import load_stamped_dataset

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from core.ai.evaluation.contracts import (
        BudgetCoverageManifest,
        BudgetRole,
        BudgetSnapshot,
        EvaluationInput,
        EvaluationLimits,
        ResponseCharge,
        RunIdentity,
    )
    from core.ai.workflows.contracts import WorkflowIntent

    from harness.features.evals.domain.benchmark import UsageSource
    from harness.features.evals.domain.outcomes import WorkflowAutomationCase, WorkflowAutomationDataset
    from harness.features.evals.domain.pilot import DevelopmentPilotRequest, PilotAdmission, PilotParticipant, PilotRoleModel
    from harness.features.evals.infrastructure.budgeted_model import PilotMeter
    from harness.features.evals.internal_contracts import (
        ArchivedRuns,
        BenchmarkModel,
        PilotBudgetRecord,
        PilotCycleRecord,
        PilotRuns,
        RunSpendRecords,
    )

_PROBE_USAGE = RequestUsage(input_tokens=1_000, output_tokens=1_000)
"""A stand-in request, big enough to price and never sent anywhere, used to ask whether a role's
model has a resolvable price before the cycle opens."""


def pilot_preflight(  # noqa: PLR0913 (a preflight is taken over its record and the five facts it holds)
    record: PilotBudgetRecord,
    *,
    cycle_id: str,
    limits: EvaluationLimits,
    coverage: BudgetCoverageManifest,
    participants: tuple[PilotParticipant, ...],
    unpriced_roles: tuple[BudgetRole, ...] = (),
) -> PilotPreflight:
    """The preflight for a trial on `record`, taking the root and the device from the record itself.

    Args:
        record: The budget record the cycle opens on.
        cycle_id: The cycle to open.
        limits: The stop thresholds the cycle runs under.
        coverage: Which generative model paths are metered and which are not.
        participants: The processes that take part in the trial.
        unpriced_roles: The configured roles whose model has no price.

    Returns:
        The preflight, not yet checked.
    """
    identity = record.state_identity()
    return PilotPreflight(
        cycle_id=cycle_id,
        limits=limits,
        canonical_state_root=identity.canonical_state_root,
        device_id=identity.device_id,
        coverage=coverage,
        participants=participants,
        roles_without_price=unpriced_roles,
    )


def roles_without_price(
    models: Iterable[PilotRoleModel], *, charge: ResponseCharge = priced_cost, at: datetime | None = None
) -> tuple[BudgetRole, ...]:
    """Which configured roles the same resolver that settles a response cannot price at all.

    Each role is probed with a response that is never sent anywhere. It carries the role's own model
    and provider names and a plain request's worth of tokens.

    Args:
        models: The configured roles and their models.
        charge: The price resolver a real response is settled with.
        at: The timestamp the probes carry. The current time when `None`.

    Returns:
        The roles with no price, in the order given.
    """
    timestamp = at or utcnow()
    probes: tuple[tuple[BudgetRole, ModelResponse], ...] = tuple(
        (row.role, ModelResponse(parts=[], usage=_PROBE_USAGE, model_name=row.model_name, provider_name=row.provider_name, timestamp=timestamp))
        for row in models
    )
    return tuple(role for role, probe in probes if charge(probe) is None)


def require_bound(record: PilotBudgetRecord, preflight: PilotPreflight) -> None:
    """Refuse a preflight verified against a root or device this record does not hold itself to.

    Args:
        record: The budget record the cycle would open on.
        preflight: The preflight its participants were verified in.

    Raises:
        BudgetPreflightError: the record's own root or device differs from the one every
            participant was checked against.
    """
    identity = record.state_identity()
    if identity != StateRootIdentity(canonical_state_root=preflight.canonical_state_root, device_id=preflight.device_id):
        msg = (
            f"the participants were verified against {preflight.canonical_state_root} (device {preflight.device_id}), "
            f"and this record measures on {identity.canonical_state_root} (device {identity.device_id})"
        )
        raise BudgetPreflightError(msg, refusal="unverified_state_root")


async def start_pilot_cycle(record: PilotBudgetRecord, preflight: PilotPreflight, *, at: datetime | None = None) -> None:
    """Check the trial is measurable and open its cycle, or refuse before anything runs.

    The record's own root and device are checked against the preflight first.

    Args:
        record: The budget record the cycle opens on.
        preflight: The preflight to check.
        at: When the cycle starts. The current time when `None`.

    Raises:
        BudgetPreflightError: the record holds another root or device than the preflight, a
            generative model path is not budgeted, a participant cannot verify the state root or
            lock it, or a configured role's model has no price.
        BudgetConflictError: the cycle exists under different limits or a different start.
    """
    require_bound(record, preflight)
    check_pilot(preflight)
    await record.begin_cycle(preflight.cycle_id, preflight.limits, pricing_known=preflight.pricing_known, at=at or utcnow())


class ArchivedRunOutcomeQueries:
    """Grades an archived run against one development case, from the record's own facts.

    The spend log is read for usage alone. A run with any unpriced response has an unknown cost,
    never a cheaper one.
    """

    def __init__(self, archive: ArchivedRuns, spend: RunSpendRecords | None = None) -> None:
        """Grade runs from `archive`, with usage read from `spend`.

        Args:
            archive: The archived runs.
            spend: The spend log. `None` leaves every usage empty.
        """
        self.archive = archive
        self.spend = spend

    def outcome_for(self, run_id: str, case: WorkflowAutomationCase, *, intent: WorkflowIntent | None = None) -> TaskOutcome | None:
        """The graded outcome of `run_id` under `case`.

        Args:
            run_id: The archived run to grade.
            case: The development case, labels included.
            intent: The workflow intent the designer recorded, or `None` when there is none.

        Returns:
            The outcome, or `None` when nothing is filed under `run_id`.
        """
        record = self.archive.get(run_id)
        if record is None:
            return None
        result = record.result
        return TaskOutcome(
            run_id=run_id,
            task_id=case.case_id,
            structural_validity=structural_validity(record),
            workflow_design=workflow_design(record, intent),
            evidence_support=evidence_support(record),
            task_success=task_success(result, case.labels),
            observed_result=result.summary,
            artifact_refs=result.artifacts,
            usage=self.usage_of(run_id),
        )

    def usage_of(self, run_id: str) -> ResourceUsage:
        """What the run's own spend log recorded.

        Args:
            run_id: The run's id.

        Returns:
            The run's usage, cost unknown while any of its rows is unpriced.
        """
        return recorded_usage(self.spend, run_id)


def recorded_usage(spend: RunSpendRecords | None, run_id: str) -> ResourceUsage:
    """What one run's spend log recorded, cost left unknown while any row of it is unpriced.

    `requests` is always zero, because the spend log keeps no count of responses.

    Args:
        spend: The spend log. `None` gives empty usage.
        run_id: The run's id.

    Returns:
        The run's tokens and cost, or empty usage when the log holds nothing for it.
    """
    folded = spend.find(run_id) if spend is not None else None
    if folded is None:
        return ResourceUsage()
    totals = folded.totals
    return ResourceUsage(
        input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens,
        cost_usd=float(totals.cost_usd) if totals.unpriced == 0 else None,
    )


class WorkflowAutomationTaskRunner:
    """A task runner over archived pilot runs, holding no case labels of any kind.

    It reads a record rather than asking a model, so it makes no request of its own and declares no
    path. Whatever the journey it reads back spent was admitted under the pilot's own cycle.
    """

    def __init__(self, runs: PilotRuns, archive: ArchivedRuns, spend: RunSpendRecords | None = None) -> None:
        """Read each trial from `runs` and its run from `archive`, with usage from `spend`.

        Args:
            runs: The pilot trial index.
            archive: The archived runs.
            spend: The spend log. `None` leaves every usage empty.
        """
        self.runs = runs
        self.archive = archive
        self.spend = spend

    @property
    def request_paths(self) -> tuple[BenchmarkModel, ...]:
        """Empty. This arm reads what a journey already left behind and asks no model anything."""
        return ()

    @property
    def usage_source(self) -> UsageSource:
        """Another record. What this reports was admitted by the pilot's own cycle, not by this one."""
        return "another_record"

    async def __call__(self, case: EvaluationInput, identity: RunIdentity, repetition: int) -> WorkflowAutomationExecution:
        """The run this case's trial produced, read back whole.

        Args:
            case: The case id and request.
            identity: The arm's run identity. It is not read.
            repetition: The repetition of the case to read.

        Returns:
            The run's output, usage and checked result, with its run and trial ids.

        Raises:
            EvaluationError: no trial was recorded for this case and repetition, or the run it
                names is no longer in the archive. Either way the slot fails and is never scored.
        """
        del identity
        trial = await self.runs.trial_for(case.case_id, repetition)
        if trial is None:
            msg = f"no pilot trial is recorded for {case.case_id!r} repetition {repetition}"
            raise EvaluationError(msg)
        record = self.archive.get(trial.run_id)
        if record is None:
            msg = f"the pilot trial for {case.case_id!r} names run {trial.run_id!r}, which the archive no longer holds"
            raise EvaluationError(msg, usage=recorded_usage(self.spend, trial.run_id))
        result = record.result
        return WorkflowAutomationExecution(
            output=result.summary,
            usage=recorded_usage(self.spend, trial.run_id),
            diagnostics=(
                DiagnosticObservation(code="execution_outcome", detail=record.outcome),
                DiagnosticObservation(code="checked_outcome", detail=result.status),
            ),
            run_id=trial.run_id,
            trial_id=trial.trial_id,
            result=result,
            intent=trial.intent,
        )


async def _open_development_pilot(
    record: PilotCycleRecord,
    *,
    cycle_id: str,
    admission: PilotAdmission,
    charge: ResponseCharge = priced_cost,
    at: datetime | None = None,
) -> PilotPreflight:
    """Refuse a live pilot that cannot be measured, and open its cycle or continue the one already open."""
    preflight = pilot_preflight(
        record,
        cycle_id=cycle_id,
        limits=WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS,
        coverage=admission.coverage,
        participants=admission.participants,
        unpriced_roles=roles_without_price(admission.role_models, charge=charge, at=at),
    )
    standing = await record.read(cycle_id)
    if standing is None:
        try:
            await start_pilot_cycle(record, preflight, at=at)
        except BudgetConflictError:
            # Another process opened this same cycle between the read and the write, with its own start
            # time. It is this pilot's own cycle, so it is continued.
            standing = await record.read(cycle_id)
            if standing is None:
                raise
        else:
            return preflight
    require_bound(record, preflight)
    check_pilot(preflight)
    if standing.limits != preflight.limits:
        msg = f"cycle {cycle_id!r} is already open under other limits, and what it has spent cannot be read under these"
        raise BudgetConflictError(msg)
    return preflight


def _spend_diagnostics(snapshot: BudgetSnapshot, run_id: str) -> tuple[DiagnosticObservation, ...]:
    """What the record says about this trial's own spending, as observations nothing grades."""
    observed = [
        DiagnosticObservation(code="trial_requests", detail=str(snapshot.trial_requests)),
        DiagnosticObservation(code="trial_attempts", detail=str(snapshot.trial_attempts)),
        DiagnosticObservation(code="token_source_run", detail=run_id),
    ]
    if snapshot.unresolved_charge:
        observed.append(DiagnosticObservation(code="unresolved_charge", detail="a charge nothing could price is on this cycle"))
    if snapshot.trial_measured_model_overshoot_usd > 0:
        observed.append(
            DiagnosticObservation(code="trial_cost_overshoot_usd", detail=str(snapshot.trial_measured_model_overshoot_usd))
        )
    if snapshot.cycle_measured_model_overshoot_usd > 0:
        observed.append(
            DiagnosticObservation(code="cycle_cost_overshoot_usd", detail=str(snapshot.cycle_measured_model_overshoot_usd))
        )
    return tuple(observed)


class LivePilotTaskRunner:
    """A task runner over a live pilot: the journey's own run, and what its trial actually spent.

    It holds no case labels of any kind.
    A trial the cycle never opened, and a run the archive no longer holds, are failed slots rather
    than scored ones. Either way the usage the record does know goes out with the failure.
    """

    def __init__(
        self, cycle_id: str, record: PilotCycleRecord, runs: PilotRuns, archive: ArchivedRuns, spend: RunSpendRecords | None = None
    ) -> None:
        """Read each trial's run from `archive` and its spending from `record` under `cycle_id`.

        Args:
            cycle_id: The pilot's open cycle.
            record: The budget record the cycle is persisted in.
            runs: The pilot trial index.
            archive: The archived runs.
            spend: The spend log the run's tokens come from. `None` leaves them empty.
        """
        self.cycle_id = cycle_id
        self.record = record
        self.archived = WorkflowAutomationTaskRunner(runs, archive, spend)

    @property
    def request_paths(self) -> tuple[BenchmarkModel, ...]:
        """Empty. This reads a record and a cycle, and asks no model anything."""
        return ()

    @property
    def usage_source(self) -> UsageSource:
        """Another record. What this reports was admitted by the pilot's own cycle, not by this one."""
        return "another_record"

    async def __call__(self, case: EvaluationInput, identity: RunIdentity, repetition: int) -> WorkflowAutomationExecution:
        """The run this case's trial produced, with the trial's own measured spending beside it.

        Args:
            case: The case id and request.
            identity: The arm's run identity. It is not read.
            repetition: The repetition of the case to read.

        Returns:
            The run's output and checked result, with the trial's requests and cost from the record
            and the run's own tokens.

        Raises:
            EvaluationError: the trial was never recorded, its run is no longer in the archive, or
                the cycle holds no such trial to read a spend from.
        """
        execution = await self.archived(case, identity, repetition)
        scope = BudgetScope(cycle_id=self.cycle_id, trial_id=execution.trial_id)
        state = await self.record.read(self.cycle_id)
        if state is None:
            msg = f"cycle {self.cycle_id!r} was never opened, so nothing measured the trial for {case.case_id!r}"
            raise EvaluationError(msg, usage=execution.usage)
        try:
            snapshot = cycle_snapshot(state, scope)
        except BudgetNotOpenError as error:
            raise EvaluationError(str(error), usage=execution.usage) from error
        return execution.model_copy(
            update={
                "usage": trial_usage(snapshot, execution.usage),
                "diagnostics": (*execution.diagnostics, *_spend_diagnostics(snapshot, execution.run_id)),
            }
        )


@dataclass(frozen=True)
class PilotCollaborators:
    """Where one pilot's runs are found: the trial index, the archive, and the spend log."""

    runs: PilotRuns
    archive: ArchivedRuns
    spend: RunSpendRecords | None = None


type PilotMetering = PilotMeter | UnmeteredPilot
"""How a pilot is metered, or the written reason it is not. There is no third answer and no default."""


def require_metered(metering: PilotMetering, record: PilotCycleRecord, cycle_id: str) -> tuple[BudgetRole, ...]:
    """The roles this pilot is metered on, or a refusal naming what is missing.

    A `PilotMeter` passes when it meters this cycle, was built over this very record, and has
    mounted at least one model, every one on that same record and cycle.
    The last check only shows that something asked the meter once. You must build the journey's
    environment through the meter. Nothing here can check that.
    An `UnmeteredPilot` passes and meters nothing.

    Args:
        metering: The pilot's meter, or the written reason it has none.
        record: The budget record the pilot opens on.
        cycle_id: The pilot's cycle.

    Returns:
        The metered roles, empty for an `UnmeteredPilot`.

    Raises:
        UnmeterablePilotError: the meter belongs to another cycle or another record, nothing has
            ever asked it for a model, or a model it mounted reports to another record or cycle.
    """
    if isinstance(metering, UnmeteredPilot):
        return ()
    if metering.record is not record:
        msg = (
            "the meter was built over a different budget record than this pilot opens on, "
            "so its requests would be counted somewhere this pilot never reads"
        )
        raise UnmeterablePilotError(msg)
    if metering.cycle_id != cycle_id:
        msg = f"the meter measures cycle {metering.cycle_id!r} and this pilot is cycle {cycle_id!r}"
        raise UnmeterablePilotError(msg)
    if not metering.mounted:
        msg = (
            "nothing has asked this meter for a model, so no environment was built through it. Compose the "
            "environment the pilot's journey runs in with harness_session(..., wrap_model=meter) before opening the pilot"
        )
        raise UnmeterablePilotError(msg)
    for budgeted in metering.mounted:
        if budgeted.record is not record or budgeted.scope.cycle_id != cycle_id:
            msg = f"the meter mounted a {budgeted.role} model that does not report to this pilot's own cycle"
            raise UnmeterablePilotError(msg)
    return metering.metered_roles


@dataclass(frozen=True)
class OpenedPilot:
    """A live pilot with its one cycle open: what was sealed, what was checked, and the runner over it.

    `handoff` starts with no outcomes at all. A caller fills it in as cases are graded, and an empty
    one is a pilot nobody ran rather than a measured zero.
    """

    handoff: PilotHandoff
    preflight: PilotPreflight
    dataset: WorkflowAutomationDataset
    runner: LivePilotTaskRunner

    @property
    def cycle_id(self) -> str:
        """The one cycle every request of this pilot is admitted under."""
        return self.handoff.frozen.cycle_id


async def start_development_pilot(
    record: PilotCycleRecord,
    request: DevelopmentPilotRequest,
    collaborators: PilotCollaborators,
    metering: PilotMetering,
    *,
    charge: ResponseCharge = priced_cost,
) -> OpenedPilot:
    """Open one live development pilot, and hand back the sealed pilot and the runner over it.

    This is the only way to open a pilot. It loads the dataset only if its stamp covers exactly that
    version, requires the pilot to be metered, seals it, prices every configured role through
    `charge`, and opens the one cycle `pilot_cycle_id` names under `WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS`.
    Opening the same pilot again continues its cycle, and every check runs again.
    To meter a pilot, name its cycle with `pilot_cycle_id`, build a `PilotMeter` over it, compose the
    journey's environment through `harness_session(..., wrap_model=meter)`, and only then open it here.
    A journey driven through an environment composed without the meter is unmetered, and nothing
    here can see that.

    Args:
        record: The budget record the pilot's cycle opens on.
        request: The dataset and stamp paths, the inputs, the baseline, the limitations and the
            admission the pilot is checked against.
        collaborators: Where the pilot's runs are found.
        metering: A `PilotMeter` bound to this pilot's cycle and record, or an `UnmeteredPilot`
            carrying the caller's written reason, which the seal records. It has no default.
        charge: The price resolver every configured role is probed with.

    Returns:
        The opened pilot: its handoff, its preflight, its dataset and its runner.

    Raises:
        ValueError: the stamp names another dataset version, another set of cases, or other kinds of
            work than the cases declare.
        UnmeterablePilotError: the meter belongs to another cycle or another record, it has
            mounted nothing, or a model it mounted reports to another record or cycle.
        BudgetPreflightError: a generative model path is not budgeted, a participant cannot verify
            the record's own state root on its device or cannot lock it, or a configured role's
            model has no price the resolver can name.
        BudgetConflictError: the pilot's cycle is already open under other limits.
    """
    dataset = load_stamped_dataset(request.dataset_path, request.stamp_path)
    cycle_id = pilot_cycle_id(dataset, request.inputs)
    metered_roles = require_metered(metering, record, cycle_id)
    seal = PilotSeal(
        baseline=request.baseline,
        limitations=request.limitations,
        metered_roles=metered_roles,
        unmetered_reason=metering.reason if isinstance(metering, UnmeteredPilot) else "",
    )
    frozen = freeze_pilot(dataset, request.inputs, cycle_id=cycle_id, seal=seal)
    preflight = await _open_development_pilot(record, cycle_id=cycle_id, admission=request.admission, charge=charge)
    runner = LivePilotTaskRunner(cycle_id, record, collaborators.runs, collaborators.archive, collaborators.spend)
    return OpenedPilot(handoff=PilotHandoff(frozen=frozen), preflight=preflight, dataset=dataset, runner=runner)
