"""Running one post-pilot benchmark: composing what it runs on, refusing what cannot be measured, and filing its report."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from core.ai.evaluation.contracts import (
    BudgetCoverageManifest,
    BudgetNotOpenError,
    BudgetPreflightError,
    BudgetRefusedError,
    BudgetScope,
    ComparisonPlan,
    EvaluationError,
    compare_outcomes,
    content_fingerprint,
    cycle_snapshot,
    has_unresolved_charge,
    measured_spend,
    overshoot,
    require_full_coverage,
)
from core.cqrs.contracts import utcnow
from engram.evals.outcomes.contracts import audit_cycle

from harness.adapters.contracts import LOCAL_HOSTS, priced_cost, split_address
from harness.features.evals.domain.benchmark import (
    BenchmarkRefusalNote,
    BenchmarkTrialRecord,
    DeclaredModelPath,
    DevelopmentReplay,
    OutcomeStateReport,
    OwnerAcceptanceRecord,
    UsageDeclaration,
    benchmark_cycle_id,
    checked_grade,
    comparability_drift,
    freeze_benchmark,
    unrecorded_requests,
)
from harness.features.evals.domain.pilot import WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS, PilotRoleModel
from harness.features.evals.exceptions import UnrecordedRequestsError
from harness.features.evals.infrastructure.budgeted_model import BudgetedModel
from harness.features.evals.infrastructure.files import (
    TrialLog,
    file_report,
    load_benchmark_custody,
    load_stamped_dataset,
    load_task_dataset,
)
from harness.features.evals.internal_contracts import BenchmarkModel
from harness.features.evals.service.pilot import pilot_preflight, roles_without_price, start_pilot_cycle

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from datetime import datetime

    from core.ai.evaluation.contracts import (
        BudgetRefusal,
        BudgetRole,
        BudgetSnapshot,
        ComparisonReport,
        EvaluationBudgetRecord,
        EvaluationInput,
        EvaluationLimits,
        ResponseCharge,
        RunIdentity,
        TaskCase,
        TaskGrade,
        Variant,
    )
    from core.ai.workflows.contracts import OutcomeStatus
    from engram.evals.outcomes.contracts import MethodologyAudit
    from pydantic_ai.models import Model

    from harness.features.evals.domain.benchmark import (
        BenchmarkFreeze,
        BenchmarkModelType,
        BenchmarkPosition,
        BenchmarkRefusal,
        BenchmarkRequest,
        OutcomeObjective,
        SealedComparison,
    )
    from harness.features.evals.domain.pilot import WorkflowAutomationExecution
    from harness.features.evals.internal_contracts import (
        BenchmarkArm,
        BenchmarkComposer,
        BenchmarkGrader,
        BenchmarkRecord,
        BenchmarkReviews,
        ComposedBenchmark,
    )

_TEST_DOUBLE_SYSTEMS = frozenset({"test", "function"})
"""The `Model.system` names pydantic-ai gives its two stand-ins, which reach no provider."""

_PLAN_OBJECTIVE: dict[OutcomeObjective, Literal["task_success", "cost"]] = {
    "task-success": "task_success",
    "measured-generative-model-cost": "cost",
}
"""How this command's objective is spelled in the comparison kernel's own vocabulary."""


_UNSTARTED_BENCHMARK_TRIAL = "benchmark-trial-not-started"
"""The trial a mounted model names before the benchmark has opened one."""


@dataclass(frozen=True)
class BenchmarkMount:
    """Where a mounted model reports: the record, the cycle, the price resolver and the clock."""

    record: EvaluationBudgetRecord
    cycle_id: str
    charge: ResponseCharge = priced_cost
    clock: Callable[[], datetime] = utcnow


def budgeted(model: Model, mount: BenchmarkMount, *, path: str, role: BudgetRole) -> BenchmarkModel:
    """Mount `model` on the record, so every request it makes is admitted before a provider is reached.

    A request made before the benchmark points the model at a real trial is refused by the record
    rather than charged to one.

    Args:
        model: The model to mount.
        mount: The record and cycle it reports to.
        path: The name of where its requests are made from.
        role: The budget role its requests are charged as.

    Returns:
        The mounted model beside its path and role.
    """
    return BenchmarkModel(
        path=path,
        role=role,
        model=BudgetedModel(
            model,
            record=mount.record,
            scope=BudgetScope(cycle_id=mount.cycle_id, trial_id=_UNSTARTED_BENCHMARK_TRIAL),
            role=role,
            charge=mount.charge,
            clock=mount.clock,
        ),
    )


def reports_to(model: Model, mount: BenchmarkMount) -> bool:
    """Whether this very object admits its requests on this record and this cycle.

    It reads the object itself and never asks the caller.

    Args:
        model: The model object requests go through.
        mount: The record and cycle.

    Returns:
        True for a `BudgetedModel` over this record and cycle.
    """
    return isinstance(model, BudgetedModel) and model.record is mount.record and model.scope.cycle_id == mount.cycle_id


def declared_paths(models: Sequence[BenchmarkModel], mount: BenchmarkMount) -> tuple[DeclaredModelPath, ...]:
    """Every declared path beside what the classifier found when it read that path's own object.

    Args:
        models: The benchmark's declared model paths.
        mount: The record and cycle.

    Returns:
        One declared path per model, in order.
    """
    return tuple(DeclaredModelPath(path=row.path, role=row.role, budgeted=reports_to(row.model, mount)) for row in models)


def discovered_coverage(models: Sequence[BenchmarkModel], mount: BenchmarkMount) -> BudgetCoverageManifest:
    """Every model path this benchmark actually holds, split by what the record can see.

    Each path is classified by the model object its requests go through, never by a declaration.
    A path somebody forgot to mount lands in `unmetered_model_paths`, and `require_full_coverage`
    refuses on it.

    Args:
        models: The benchmark's model paths.
        mount: The record and cycle.

    Returns:
        The coverage manifest.
    """
    metered = tuple(row.path for row in models if reports_to(row.model, mount))
    unmetered = tuple(row.path for row in models if not reports_to(row.model, mount))
    return BudgetCoverageManifest(
        generative_model_paths=tuple(row.path for row in models), budgeted_model_paths=metered, unmetered_model_paths=unmetered
    )


def model_type_of(models: Sequence[BenchmarkModel]) -> BenchmarkModelType:
    """Whether this benchmark reaches a provider at all, read off the models it holds.

    A stand-in with a budgeted model around it is still a stand-in. An empty sequence answers
    `live`, so a composition that lost its models is still asked for stop thresholds.

    Args:
        models: The benchmark's model paths.

    Returns:
        `test_double` when every model underneath is a pydantic-ai stand-in, otherwise `live`.
    """
    if not models:
        return "live"
    return "test_double" if all(_unwrapped(row.model).system in _TEST_DOUBLE_SYSTEMS for row in models) else "live"


def _unwrapped(model: Model) -> Model:
    """The model underneath however many `BudgetedModel` wrappers."""
    while isinstance(model, BudgetedModel):
        model = model.wrapped
    return model


def role_models_of(models: Sequence[BenchmarkModel]) -> tuple[PilotRoleModel, ...]:
    """One configured role per budget role this benchmark holds, as the price resolver has to name it.

    Args:
        models: The benchmark's model paths.

    Returns:
        One role model per budget role, taken from the first path of that role.
    """
    seen: dict[BudgetRole, PilotRoleModel] = {}
    for row in models:
        underneath = _unwrapped(row.model)
        seen.setdefault(
            row.role, PilotRoleModel(role=row.role, model_name=underneath.model_name, provider_name=underneath.system)
        )
    return tuple(seen.values())


def remote_temporal_worker(run_engine: Literal["in_process", "temporal"], address: str) -> str:
    """The reason a benchmark cannot measure this deployment's workers, or an empty string.

    A Temporal worker on another host makes model requests no record here sees, so the benchmark is
    refused.

    Args:
        run_engine: The run engine the deployment is configured for.
        address: The Temporal server address.

    Returns:
        The reason, also given when the address cannot be read, or an empty string when runs stay in
        this process or the Temporal host is local.
    """
    if run_engine != "temporal":
        return ""
    try:
        host, _ = split_address(address)
    except ValueError as error:
        return str(error)
    return "" if host in LOCAL_HOSTS else f"temporal workers serve {address}, which is another host, and nothing here can meter them"


def positions_of(composed: ComposedBenchmark) -> tuple[tuple[BenchmarkPosition, BenchmarkArm | BenchmarkGrader], ...]:
    """The three positions of one benchmark, each beside the object filling it.

    Args:
        composed: The composed benchmark.

    Returns:
        The baseline, the candidate and the grader, in that order.
    """
    return (("baseline", composed.arms.baseline), ("candidate", composed.arms.candidate), ("grader", composed.arms.grader))


def usage_declarations(composed: ComposedBenchmark) -> tuple[UsageDeclaration, ...]:
    """What each of the three positions said about itself, as the report carries it.

    Args:
        composed: The composed benchmark.

    Returns:
        One declaration per position, in position order.
    """
    return tuple(
        UsageDeclaration(made_by=name, declared_paths=tuple(row.path for row in one.request_paths), usage_source=one.usage_source)
        for name, one in positions_of(composed)
    )


def unstated_usage_sources(composed: ComposedBenchmark) -> tuple[str, ...]:
    """Which of the three positions have declared paths and a stated usage source that do not agree.

    A position that asks through this benchmark's own paths declares them and answers `this_record`.
    A position reporting figures another record measured declares nothing and answers `another_record`.
    Any other pairing is refused before any slot runs.

    Args:
        composed: The composed benchmark.

    Returns:
        One sentence per disagreement, empty when all three agree.
    """
    wrong: list[str] = []
    for name, one in positions_of(composed):
        if not one.request_paths and one.usage_source != "another_record":
            wrong.append(f"the {name} declares no model path and does not report another record's usage, so nothing here can read what it spent")
        if one.request_paths and one.usage_source == "another_record":
            wrong.append(f"the {name} declares model paths and reports another record's usage, which cannot both be true of one of them")
    return tuple(wrong)


def grader_off_the_record(composed: ComposedBenchmark) -> str:
    """Why this benchmark's grader is not on the record, or an empty string when it is.

    The grader is on the record when its own `request_paths` hold a grader-role path that is one of
    the declared objects in `composed.models`.

    Args:
        composed: The composed benchmark.

    Returns:
        The reason, or an empty string.
    """
    declared = composed.models
    if any(row.role == "grader" and any(row is one for one in declared) for row in composed.arms.grader.request_paths):
        return ""
    return "the grader does not ask through any declared grader-role path, so its requests are outside the cycle the stop thresholds bound"


def unbound_paths(composed: ComposedBenchmark) -> tuple[str, ...]:
    """Where the arms' stated model paths and the benchmark's declaration disagree, read from both sides.

    This compares two statements and never opens an arm. A path an arm states that was never
    declared is refused, and so is a declared path no arm states.
    An arm that misstates its own `request_paths` is not caught here. `unrecorded_requests` reads
    behaviour after each slot has run.

    Args:
        composed: The composed benchmark.

    Returns:
        One sentence per disagreement, empty when the two sides agree.
    """
    used = tuple(row for one in (composed.arms.baseline, composed.arms.candidate, composed.arms.grader) for row in one.request_paths)
    undeclared = tuple(f"{row.path} is requested through and was never declared" for row in used if not any(row is one for one in composed.models))
    unused = tuple(f"{row.path} is declared and no arm requests through it" for row in composed.models if not any(row is one for one in used))
    return (*undeclared, *unused)


@dataclass
class _OpenSlot:
    """The slot now being performed, carried from the runner to the grader of the same slot."""

    case_id: str
    variant: Variant
    repetition: int
    trial_id: str
    run_id: str
    admitted_after_arm: int = 0
    checked: OutcomeStatus | None = None


class MeteredSlots:
    """The pair a paired comparison drives one slot through: the runner opens it, the grader closes it.

    A slot is one case, one repetition and one arm. Its trial opens on the record before the arm runs,
    and the slot is filed after the grader is paid for, so the grader's requests are in its figures.
    After the arm and after the grader, however either ended, the requests each one reports are held
    against what the record admitted for that trial.
    Reporting more than the record admitted refuses that slot, and every later slot refuses before
    its arm runs.
    This detects and does not prevent: the refused slot's requests were already made, outside any limit.
    """

    def __init__(
        self,
        cycle_id: str,
        record: BenchmarkRecord,
        composed: ComposedBenchmark,
        trial_log: TrialLog,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        """Drive slots of `composed` on `record` under `cycle_id`, filing each one in `trial_log`.

        Args:
            cycle_id: The benchmark's open cycle.
            record: The budget record the cycle is on.
            composed: The composed benchmark whose arms and grader run.
            trial_log: Where each slot is filed.
            clock: The clock each trial's start is read from.
        """
        self.cycle_id = cycle_id
        self.record = record
        self.composed = composed
        self.trial_log = trial_log
        self.clock = clock
        self.refusals: list[BenchmarkRefusalNote] = []
        self._open: _OpenSlot | None = None

    def _variant_of(self, identity: RunIdentity) -> Variant:
        """Which arm this slot is, read off the identity the comparison handed over."""
        return "baseline" if identity == self.composed.baseline else "candidate"

    def _point_at(self, trial_id: str) -> BudgetScope:
        """Move every model this benchmark holds onto `trial_id`, and hand back that scope."""
        scope = BudgetScope(cycle_id=self.cycle_id, trial_id=trial_id)
        if self.composed.meter is not None:
            self.composed.meter.for_trial(trial_id)
        for row in self.composed.models:
            if isinstance(row.model, BudgetedModel):
                row.model.scope = scope
        return scope

    async def _snapshot(self, scope: BudgetScope) -> BudgetSnapshot | None:
        """What the record says this slot spent, or `None` when it holds no such trial."""
        state = await self.record.read(self.cycle_id)
        if state is None:
            return None
        try:
            return cycle_snapshot(state, scope)
        except BudgetNotOpenError:
            return None

    async def _file(
        self, slot: _OpenSlot, *, failure: str = "", refusal: BudgetRefusal | None = None, grade: TaskGrade | None = None
    ) -> None:
        """Write this slot down, spending and grade included, before anything reads a verdict off it."""
        snapshot = await self._snapshot(BudgetScope(cycle_id=self.cycle_id, trial_id=slot.trial_id))
        self.trial_log.file(
            BenchmarkTrialRecord(
                case_id=slot.case_id,
                variant=slot.variant,
                repetition=slot.repetition,
                trial_id=slot.trial_id,
                run_id=slot.run_id,
                requests=snapshot.trial_requests if snapshot is not None else 0,
                attempts=snapshot.trial_attempts if snapshot is not None else 0,
                measured_model_spent_usd=snapshot.trial_measured_model_spent_usd if snapshot is not None else Decimal(0),
                measured_model_overshoot_usd=snapshot.trial_measured_model_overshoot_usd if snapshot is not None else Decimal(0),
                unresolved_charge=snapshot.unresolved_charge if snapshot is not None else False,
                refusal=refusal,
                failure=failure,
                grade=grade,
            )
        )

    async def run(self, case: EvaluationInput, identity: RunIdentity, repetition: int) -> WorkflowAutomationExecution:
        """Open this slot's trial, point every model at it, and run the arm the identity names.

        Args:
            case: The case id and request.
            identity: The run identity of the arm to run.
            repetition: The repetition of the case this slot is.

        Returns:
            The arm's execution.

        Raises:
            EvaluationError: the arm failed, the record refused it, or a position reported requests
                the record did not admit. The slot is on disk with what it spent before this leaves.
        """
        variant = self._variant_of(identity)
        trial_id = f"{case.case_id}-{repetition}-{variant}"
        slot = _OpenSlot(case_id=case.case_id, variant=variant, repetition=repetition, trial_id=trial_id, run_id="")
        self._open = slot
        if self.refusals:
            # Once any position has spent where the record could not see, no later slot runs.
            self._open = None
            await self._file(slot, failure=self.refusals[0].detail)
            raise UnrecordedRequestsError(self.refusals[0].detail)
        await self.record.begin_trial(self._point_at(trial_id), at=self.clock())
        arm = self.composed.arms.baseline if variant == "baseline" else self.composed.arms.candidate
        try:
            execution = await arm(case, identity, repetition)
        except EvaluationError as error:
            self._open = None
            refusal = error.refusal if isinstance(error, BudgetRefusedError) else None
            await self._settle_arm(slot, arm, reported=error.usage.requests, failure=str(error), refusal=refusal)
            raise
        slot.run_id = execution.run_id
        slot.checked = execution.result.status if execution.result is not None else None
        await self._settle_arm(slot, arm, reported=execution.usage.requests)
        return execution

    async def _admitted_for(self, trial_id: str) -> int:
        """How many requests the record has admitted for this trial right now."""
        snapshot = await self._snapshot(BudgetScope(cycle_id=self.cycle_id, trial_id=trial_id))
        return snapshot.trial_requests if snapshot is not None else 0

    async def _settle_arm(
        self, slot: _OpenSlot, arm: BenchmarkArm, *, reported: int, failure: str = "", refusal: BudgetRefusal | None = None
    ) -> None:
        """Read what the arm spent against what the record admitted, however the arm ended."""
        slot.admitted_after_arm = await self._admitted_for(slot.trial_id)
        unrecorded = unrecorded_requests(
            who=f"the {slot.variant} arm",
            reported=reported,
            admitted=slot.admitted_after_arm,
            reads_this_record=arm.usage_source == "this_record",
        )
        if unrecorded:
            self._open = None
            await self._refuse(slot, unrecorded)
        if failure:
            await self._file(slot, failure=failure, refusal=refusal)

    async def _refuse(self, slot: _OpenSlot, detail: str) -> None:
        """File this slot, note the refusal on the whole sitting, and raise `UnrecordedRequestsError`."""
        self.refusals.append(BenchmarkRefusalNote(refusal="unrecorded_model_requests", detail=detail))
        await self._file(slot, failure=detail)
        raise UnrecordedRequestsError(detail)

    async def grade(self, case: TaskCase, output: str) -> TaskGrade:
        """Grade the slot the runner just opened, and file that slot however the grading ends.

        The grader's requests count toward this slot's trial.

        Args:
            case: The case, with the criterion the grader judges by.
            output: The arm's output text.

        Returns:
            The grade. A candidate's pass becomes a fail when its run's checked status is not
            `succeeded`.

        Raises:
            EvaluationError: the grader failed, the record refused its request, or the grader
                reported requests the record did not admit. The slot is on disk with what it spent
                and what refused it before this leaves.
        """
        slot = self._open
        self._open = None
        try:
            graded = await self.composed.arms.grader(case, output)
        except EvaluationError as error:
            if slot is not None:
                refusal = error.refusal if isinstance(error, BudgetRefusedError) else None
                await self._settle_grader(slot, reported=error.usage.requests, failure=str(error), refusal=refusal)
            raise
        if slot is None:
            return graded
        graded = checked_grade(graded, variant=slot.variant, checked=slot.checked)
        await self._settle_grader(slot, reported=graded.usage.requests, grade=graded)
        return graded

    async def _settle_grader(
        self, slot: _OpenSlot, *, reported: int, failure: str = "", refusal: BudgetRefusal | None = None, grade: TaskGrade | None = None
    ) -> None:
        """Read what the grader spent against what this trial gained while it ran, then file the slot."""
        unrecorded = unrecorded_requests(
            who="the grader",
            reported=reported,
            admitted=await self._admitted_for(slot.trial_id) - slot.admitted_after_arm,
            reads_this_record=self.composed.arms.grader.usage_source == "this_record",
        )
        if unrecorded:
            await self._refuse(slot, unrecorded)
        await self._file(slot, failure=failure, refusal=refusal, grade=grade)


@dataclass(frozen=True)
class _ReadCases:
    """The cases a benchmark read, and what identifies that exact set."""

    name: str
    version: str
    fingerprint: str


@dataclass(frozen=True)
class _Learned:
    """What a sitting had learned about what it runs on when it stopped, defaulting to nothing at all."""

    model_type: BenchmarkModelType = "unbuilt"
    custody: MethodologyAudit | None = None
    declared: tuple[DeclaredModelPath, ...] = ()
    usage_declarations: tuple[UsageDeclaration, ...] = ()


_NOTHING_LEARNED = _Learned()
"""The `_Learned` of a sitting refused before it read a case or built its models."""


@dataclass(frozen=True)
class _Sitting:
    """One sitting of one benchmark: what was asked, what was frozen, and when it started."""

    request: BenchmarkRequest
    freeze: BenchmarkFreeze
    started: datetime
    clock: Callable[[], datetime]

    def report_id(self, read: _ReadCases) -> str:
        """This sitting's own report name, over everything that makes it this report and this sitting."""
        content = f"{self.freeze.model_dump_json()}\n{self.request.objective}\n{read.fingerprint}\n{self.started.isoformat()}"
        return content_fingerprint(content)[:32]

    def blocked(self, read: _ReadCases, refusals: tuple[BenchmarkRefusalNote, ...], learned: _Learned = _NOTHING_LEARNED) -> OutcomeStateReport:
        """A report that measured nothing, naming the rule that stopped it and what it had read."""
        return OutcomeStateReport(
            report_id=self.report_id(read),
            objective=self.request.objective,
            empirical_status="blocked",
            model_kind=learned.model_type,
            started_at=self.started,
            dataset=read.name,
            dataset_version=read.version,
            dataset_fingerprint=read.fingerprint,
            freeze=self.freeze,
            custody=learned.custody,
            declared_model_paths=learned.declared,
            usage_declarations=learned.usage_declarations,
            refusals=refusals,
        )

    def filed(self, report: OutcomeStateReport) -> OutcomeStateReport:
        """File `report` under its own id and hand it back, so no caller reports what it did not write."""
        file_report(self.request.report_root, report)
        return report


def _note(refusal: BenchmarkRefusal, detail: str) -> tuple[BenchmarkRefusalNote, ...]:
    """One refusal, as a report carries it."""
    return (BenchmarkRefusalNote(refusal=refusal, detail=detail),)


def _coverage_refusal(models: tuple[BenchmarkModel, ...], mount: BenchmarkMount) -> tuple[BenchmarkRefusalNote, ...]:
    """What the classifier finds when it reads these objects, or nothing when every path is on the record."""
    try:
        require_full_coverage(discovered_coverage(models, mount))
    except BudgetPreflightError as error:
        return _note(error.refusal, str(error))
    return ()


def _each_refusal(
    sitting: _Sitting, composed: ComposedBenchmark, mount: BenchmarkMount, model_type: BenchmarkModelType
) -> Iterator[tuple[BenchmarkRefusalNote, ...]]:
    """Every rule a composed benchmark is held to, in the order they refuse, each one empty or one note."""
    request = sitting.request
    yield (
        _note("no_declared_model_path", "the composition declares no generative model path, and every benchmark makes at least a grader request")
        if not composed.models
        else ()
    )
    unstated = unstated_usage_sources(composed)
    yield _note("unstated_usage_source", ". ".join(unstated)) if unstated else ()
    off_record = grader_off_the_record(composed)
    yield _note("no_declared_grader_path", off_record) if off_record else ()
    yield (
        _note("limits_absent", "a model that reaches a provider runs under stated stop thresholds or it does not run")
        if model_type == "live" and request.limits is None
        else ()
    )
    remote = remote_temporal_worker(request.run_engine, request.temporal_address)
    yield _note("remote_temporal_worker", remote) if remote else ()
    unbound = unbound_paths(composed)
    yield _note("undeclared_model_path", ", ".join(unbound)) if unbound else ()
    yield _coverage_refusal(composed.models, mount)
    yield (
        _note("arms_identical", "the two arms carry one identity, so nothing is being compared with anything")
        if composed.baseline == composed.candidate
        else ()
    )
    drift = comparability_drift(sitting.freeze, baseline=composed.baseline, candidate=composed.candidate)
    yield _note("identity_drift", ". ".join(drift)) if drift else ()


def _preflight_refusals(
    sitting: _Sitting, composed: ComposedBenchmark, mount: BenchmarkMount, model_type: BenchmarkModelType
) -> tuple[BenchmarkRefusalNote, ...]:
    """The first rule that refuses this composed benchmark, or nothing when none of them does."""
    return next((found for found in _each_refusal(sitting, composed, mount, model_type) if found), ())


async def _owner_acceptance(
    comparison: ComparisonReport[WorkflowAutomationExecution], reviews: BenchmarkReviews | None
) -> tuple[OwnerAcceptanceRecord, ...]:
    """What owners decided about each run this comparison read, `unknown` when unreviewed, else the last review filed."""
    records: list[OwnerAcceptanceRecord] = []
    seen: set[str] = set()
    for trial in comparison.trials:
        if trial.execution is None or trial.execution.run_id in seen:
            continue
        seen.add(trial.execution.run_id)
        filed = await reviews.for_run(trial.execution.run_id) if reviews is not None else ()
        records.append(
            OwnerAcceptanceRecord(
                case_id=trial.case_id,
                run_id=trial.execution.run_id,
                acceptance=filed[-1].outcome if filed else "unknown",
                reviews=len(filed),
            )
        )
    return tuple(records)


async def _sealed_benchmark(
    sitting: _Sitting, cases: SealedComparison, record: BenchmarkRecord, compose: BenchmarkComposer, charge: ResponseCharge
) -> OutcomeStateReport:
    """Audit the custodian's cases, refuse anything unmeasurable, and otherwise run the comparison."""
    request = sitting.request
    missing = next((path for path in (cases.dataset_path, cases.custody_path) if not path.is_file()), None)
    if missing is not None:
        read = _ReadCases(name="sealed-cases-absent", version="none", fingerprint=content_fingerprint(str(missing)))
        detail = f"no sealed cases are filed at {missing}, so there is nothing an independent custodian has sealed to measure"
        return sitting.filed(sitting.blocked(read, _note("custody_absent", detail)))

    dataset = load_task_dataset(cases.dataset_path)
    custody = load_benchmark_custody(cases.custody_path)
    read = _ReadCases(name=dataset.name, version=dataset.version, fingerprint=dataset.fingerprint)
    audit = audit_cycle(
        cases=dataset.cases,
        seals=custody.seals,
        exposures=custody.exposures,
        implementation_fingerprint=sitting.freeze.workflow_fingerprint,
        implementation_frozen_at=custody.implementation_frozen_at,
        implementation_authors=custody.implementation_authors,
        freshness=custody.freshness,
        premises=custody.premises,
    )
    if not audit.independent_holdout_verified or not audit.verified_case_ids:
        detail = ". ".join(audit.reasons) or "the cycle declares no held-out claim at all"
        return sitting.filed(sitting.blocked(read, _note("custody_unverified", detail), _Learned(custody=audit)))

    cycle_id = benchmark_cycle_id(sitting.freeze, read.fingerprint)
    composed = await compose(cycle_id)
    mount = BenchmarkMount(record=record, cycle_id=cycle_id, charge=charge, clock=sitting.clock)
    model_type = model_type_of(composed.models)
    learned = _Learned(model_type=model_type, custody=audit, declared=declared_paths(composed.models, mount), usage_declarations=usage_declarations(composed))
    refusals = _preflight_refusals(sitting, composed, mount, model_type)
    if refusals:
        return sitting.filed(sitting.blocked(read, refusals, learned))
    try:
        plan = ComparisonPlan(
            dataset=dataset,
            evaluation_case_ids=audit.verified_case_ids,
            repetitions=request.repetitions,
            objective=_PLAN_OBJECTIVE[request.objective],
        )
    except ValueError as error:
        return sitting.filed(sitting.blocked(read, _note("custody_unverified", str(error)), learned))

    limits = request.limits if request.limits is not None else WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS
    preflight = pilot_preflight(
        record,
        cycle_id=cycle_id,
        limits=limits,
        coverage=discovered_coverage(composed.models, mount),
        participants=request.participants,
        unpriced_roles=roles_without_price(role_models_of(composed.models), charge=charge, at=sitting.started),
    )
    try:
        await start_pilot_cycle(record, preflight, at=sitting.started)
    except BudgetPreflightError as error:
        return sitting.filed(sitting.blocked(read, _note(error.refusal, str(error)), learned))
    running = _RunningBenchmark(composed=composed, record=record, mount=mount, audit=audit, plan=plan, limits=limits, model_type=model_type)
    return sitting.filed(await _measured(sitting, read, running))


@dataclass(frozen=True)
class _RunningBenchmark:
    """A benchmark whose cycle is open: what it runs, what it runs on, and what bounds it."""

    composed: ComposedBenchmark
    record: BenchmarkRecord
    mount: BenchmarkMount
    audit: MethodologyAudit
    plan: ComparisonPlan
    limits: EvaluationLimits
    model_type: BenchmarkModelType


async def _measured(sitting: _Sitting, read: _ReadCases, running: _RunningBenchmark) -> OutcomeStateReport:
    """Perform every planned slot, filing each one before any verdict is folded over it, then file the verdict beside them."""
    record = running.record
    report_id = sitting.report_id(read)
    trial_log = TrialLog(sitting.request.report_root / f"{report_id}.trials")
    slots = MeteredSlots(running.mount.cycle_id, record, running.composed, trial_log, clock=running.mount.clock)
    comparison = await compare_outcomes(
        running.plan, slots.run, slots.grade, baseline=running.composed.baseline, candidate=running.composed.candidate
    )
    state = await record.read(running.mount.cycle_id)
    graded = tuple(sorted({trial.case_id for trial in comparison.trials if trial.grade is not None}))
    ungraded = tuple(sorted(set(running.plan.evaluation_case_ids) - set(graded)))
    unresolved = state is not None and has_unresolved_charge(state)
    spent = measured_spend(state.requests) if state is not None else Decimal(0)
    return OutcomeStateReport(
        report_id=report_id,
        objective=sitting.request.objective,
        empirical_status="inconclusive" if ungraded or unresolved or slots.refusals else "measured",
        model_kind=running.model_type,
        started_at=sitting.started,
        dataset=read.name,
        dataset_version=read.version,
        dataset_fingerprint=read.fingerprint,
        freeze=sitting.freeze,
        custody=running.audit,
        declared_model_paths=declared_paths(running.composed.models, running.mount),
        usage_declarations=usage_declarations(running.composed),
        refusals=tuple(dict.fromkeys(slots.refusals)),
        trials=trial_log.filed(),
        owner_acceptance=await _owner_acceptance(comparison, running.composed.reviews),
        graded_case_ids=graded,
        ungraded_case_ids=ungraded,
        cycle_measured_model_spent_usd=None if unresolved else spent,
        cycle_measured_model_overshoot_usd=overshoot(spent, running.limits.cycle_cost_stop_usd),
        unresolved_charge=unresolved,
        verdict=comparison.verdict,
        verdict_reasons=comparison.reasons,
        baseline_success_rate=comparison.baseline_success_rate,
        candidate_success_rate=comparison.candidate_success_rate,
        regressions=comparison.regressions,
        baseline_usage=comparison.baseline_usage,
        candidate_usage=comparison.candidate_usage,
    )


async def run_outcome_benchmark(
    request: BenchmarkRequest,
    record: BenchmarkRecord,
    compose: BenchmarkComposer,
    *,
    charge: ResponseCharge = priced_cost,
    clock: Callable[[], datetime] = utcnow,
) -> OutcomeStateReport:
    """Run one governed outcome benchmark, or write down exactly why none ran.

    Every refusal happens before a cycle is opened, so a benchmark that cannot be measured costs
    nothing. `compose` is called only after the sealed cases pass their custody audit.
    A `DevelopmentReplay` always writes a blocked report, because its dataset is author-visible.
    Metering is read off the models `compose` returns. A path whose model does not admit on this
    record and this cycle refuses the benchmark.

    Args:
        request: The benchmark as the person starting it names it.
        record: The budget record the benchmark's cycle opens on.
        compose: Builds the metered models, arms and grader for the cycle.
        charge: The price resolver each response is settled with.
        clock: The clock the sitting's start is read from.

    Returns:
        The report, already filed under its own id in `request.report_root`.

    Raises:
        ValueError: a development replay's stamp does not cover its dataset.
        FileExistsError: a report is already filed under this id.
        BudgetConflictError: this benchmark's cycle is already open under other limits.
    """
    freeze = freeze_benchmark(request.inputs, root=record.state_identity(), limits=request.limits)
    sitting = _Sitting(request=request, freeze=freeze, started=clock(), clock=clock)
    if isinstance(request.cases, DevelopmentReplay):
        loaded = load_stamped_dataset(request.cases.dataset_path, request.cases.stamp_path)
        read = _ReadCases(name=loaded.name, version=loaded.version, fingerprint=content_fingerprint(loaded.model_dump_json()))
        detail = f"{read.name} {read.version} declares itself author-visible, so no result over it is independent generalization evidence"
        return sitting.filed(sitting.blocked(read, _note("development_dataset_only", detail)))
    return await _sealed_benchmark(sitting, request.cases, record, compose, charge)
