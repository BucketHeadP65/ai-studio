"""Admission for one evaluation cycle's model requests, and what each one actually cost.

A cycle covers a whole evaluation. A trial is one attempt at one case inside it, and every model
request any role makes belongs to exactly one trial. Admission is asked before a request reaches a
provider, settlement records what the response actually cost, and both are persisted, so the record
survives a restart and covers processes that never shared memory.

The dollar figures in `EvaluationLimits` are accrual stop thresholds, never ceilings. A request
admitted below a threshold may cross it, and the overshoot it caused is recorded rather than hidden:
nothing here can refuse a charge a provider has already earned. Only measured generative-model
response charges enter that arithmetic. An embedding or third-party charge is an
`OtherCostObservation` of its own, and an unpriced one stays unknown instead of counting as zero.

One request at a time is in flight across the whole cycle, so at most one priced call is ever in
doubt. A charge nothing could price leaves the cycle unresolved, and an unresolved cycle admits
nothing further. Neither state is cleared by restarting.

Every rule here is a pure function over a `BudgetCycleState`. An adapter owns durability and
mutual exclusion, and asks these for every answer, so two adapters cannot disagree about what a
record admits.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from pydantic import AwareDatetime, Field, model_validator

from core.ai.evaluation.domain.models import EvaluationModel, Nonempty, require_unique
from core.ai.evaluation.domain.outcomes import EvaluationLimits
from core.ai.evaluation.exceptions import BudgetNotOpenError, BudgetPreflightError

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime
    from typing import Self

type BudgetRole = Literal["designer", "worker", "reviewer", "grader"]
"""The four model roles one trial can spend on. A path outside them is not a budgeted path."""

type ChargeStatus = Literal["pending", "known", "unresolved"]
"""Where one admitted request stands: in flight, settled for a figure, or settled for no figure."""

type BudgetRefusal = Literal[
    "request_limit", "deadline", "attempt_limit", "trial_cost_stop", "cycle_cost_stop", "request_in_flight", "unresolved_charge"
]
"""Why admission was refused. Each one is a rule the record checked, never an opinion."""

type PreflightRefusal = Literal[
    "pricing_unknown", "unbudgeted_model_path", "unmetered_model_path", "unverified_state_root", "no_cooperative_lock"
]
"""Why a live trial was refused before it ran at all."""


class StateRootIdentity(EvaluationModel):
    """The canonical POSIX root and device a record verifies every participant against."""

    canonical_state_root: Nonempty
    device_id: int = Field(ge=0)


class BudgetScope(EvaluationModel):
    """Which cycle and which trial a request belongs to."""

    cycle_id: Nonempty
    trial_id: Nonempty


class BudgetAdmission(EvaluationModel):
    """One request the record let through, and the trial position it was let through at."""

    scope: BudgetScope
    request_id: Nonempty
    role: BudgetRole
    admitted_at: AwareDatetime
    trial_request_number: int = Field(ge=1)


class BudgetRequestRecord(EvaluationModel):
    """One admitted request and what it turned out to cost.

    `pending` is in flight, `known` carries the charge the response actually earned, and
    `unresolved` is a request nothing could price. An unresolved charge is not zero and is never
    rewritten into one.
    """

    admission: BudgetAdmission
    charge_status: ChargeStatus
    settled_at: AwareDatetime | None = None
    charge_usd: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def settled_states_carry_a_time(self) -> Self:
        """Refuse a settled record with no settlement time, or a known charge with no figure.

        Returns:
            The record, unchanged.

        Raises:
            ValueError: The status, the settlement time and the charge disagree.
        """
        if self.charge_status == "pending" and (self.settled_at is not None or self.charge_usd is not None):
            msg = "a pending request carries neither a settlement time nor a charge"
            raise ValueError(msg)
        if self.charge_status != "pending" and self.settled_at is None:
            msg = "a settled request records when it settled"
            raise ValueError(msg)
        if self.charge_status == "known" and self.charge_usd is None:
            msg = "a known charge carries the figure it was settled for"
            raise ValueError(msg)
        if self.charge_status == "unresolved" and self.charge_usd is not None:
            msg = "an unresolved charge carries no figure"
            raise ValueError(msg)
        return self


class BudgetTrialState(EvaluationModel):
    """One trial's own counters: when it started, when it stops, and what it has spent on."""

    trial_id: Nonempty
    started_at: AwareDatetime
    deadline_at: AwareDatetime
    attempt_ids: tuple[Nonempty, ...] = ()
    request_ids: tuple[Nonempty, ...] = ()


class BudgetCycleState(EvaluationModel):
    """The whole persisted record of one evaluation cycle.

    `shared_root` and `device_id` are the POSIX identity the cycle was opened against. A process
    that cannot see the same root on the same device is not a participant in this cycle, however
    well its path spells out.
    """

    cycle_id: Nonempty
    limits: EvaluationLimits
    shared_root: Nonempty
    device_id: int = Field(ge=0)
    started_at: AwareDatetime
    trials: tuple[BudgetTrialState, ...] = ()
    requests: tuple[BudgetRequestRecord, ...] = ()

    @model_validator(mode="after")
    def one_record_per_identity(self) -> Self:
        """Refuse a record naming one trial or one request twice, which would hide one of them.

        Returns:
            The record, unchanged.

        Raises:
            ValueError: A trial id or a request id appears twice.
        """
        require_unique((trial.trial_id for trial in self.trials), "trial ids")
        require_unique((record.admission.request_id for record in self.requests), "request ids")
        return self


class BudgetCoverageManifest(EvaluationModel):
    """Every generative-model path a trial can take, and which of them are budgeted.

    A path is a stable name for one place a model request is made from, not a filesystem path.
    """

    generative_model_paths: tuple[Nonempty, ...]
    budgeted_model_paths: tuple[Nonempty, ...]
    unmetered_model_paths: tuple[Nonempty, ...] = ()

    @model_validator(mode="after")
    def stable_unique_paths(self) -> Self:
        """Refuse a manifest naming one path twice, which would let a gap cancel itself out.

        Returns:
            The manifest, unchanged.

        Raises:
            ValueError: A path appears twice in one list.
        """
        require_unique(self.generative_model_paths, "generative model paths")
        require_unique(self.budgeted_model_paths, "budgeted model paths")
        require_unique(self.unmetered_model_paths, "unmetered model paths")
        return self


class OtherCostObservation(EvaluationModel):
    """An embedding or third-party charge, recorded beside the model total and never inside it.

    `cost_usd` is `None` when nothing could price it. That is unknown, not free, and `reason` says
    what was observed.
    """

    scope: BudgetScope
    kind: Literal["embedding", "third_party_api", "other"]
    cost_usd: Decimal | None = Field(default=None, ge=0)
    reason: Nonempty


class BudgetSnapshot(EvaluationModel):
    """What the record says right now about one trial and the cycle it belongs to.

    The spent figures are measured generative-model response charges only. Overshoot is how far
    past a stop threshold the accrued charges actually went, which is zero until one is crossed.
    """

    scope: BudgetScope
    trial_requests: int = Field(ge=0)
    trial_attempts: int = Field(ge=0)
    trial_measured_model_spent_usd: Decimal = Field(ge=0)
    cycle_measured_model_spent_usd: Decimal = Field(ge=0)
    trial_measured_model_overshoot_usd: Decimal = Field(ge=0)
    cycle_measured_model_overshoot_usd: Decimal = Field(ge=0)
    in_flight_request_id: str | None = None
    unresolved_charge: bool = False
    deadline_at: AwareDatetime


def require_aware(at: datetime, label: str) -> datetime:
    """The timestamp `at`, refused when it carries no timezone.

    Two processes comparing a deadline have to mean the same instant by it, and a naive timestamp
    means whatever the process that wrote it was set to.

    Args:
        at: The timestamp to check.
        label: What the timestamp is, named in the refusal.

    Returns:
        `at`, unchanged.

    Raises:
        ValueError: `at` carries no timezone.
    """
    if at.tzinfo is None or at.utcoffset() is None:
        msg = f"{label} must carry a timezone"
        raise ValueError(msg)
    return at


def trial_of(state: BudgetCycleState, trial_id: str) -> BudgetTrialState | None:
    """The trial `trial_id` names in this cycle, or `None` when the cycle holds no such trial.

    Args:
        state: The cycle to look in.
        trial_id: The trial to find.

    Returns:
        The trial, or `None`.
    """
    return next((trial for trial in state.trials if trial.trial_id == trial_id), None)


def requests_of(state: BudgetCycleState, trial_id: str) -> tuple[BudgetRequestRecord, ...]:
    """Every request admitted for one trial, in the order the record admitted them.

    Args:
        state: The cycle to look in.
        trial_id: The trial whose requests are wanted.

    Returns:
        The trial's request records.
    """
    return tuple(record for record in state.requests if record.admission.scope.trial_id == trial_id)


def measured_spend(records: Iterable[BudgetRequestRecord]) -> Decimal:
    """What these requests are known to have cost. An unresolved charge adds nothing to a total.

    Args:
        records: The requests to add up.

    Returns:
        The sum of the known charges.
    """
    return sum((record.charge_usd for record in records if record.charge_usd is not None), Decimal(0))


def overshoot(spent: Decimal, stop: Decimal) -> Decimal:
    """How far past `stop` the accrued charges went, which is zero until one crosses it.

    Args:
        spent: What was accrued.
        stop: The stop threshold.

    Returns:
        The overshoot, never negative.
    """
    return max(spent - stop, Decimal(0))


def in_flight_request_id(state: BudgetCycleState) -> str | None:
    """The one request this cycle is waiting on a charge for, or `None` when it waits on none.

    Args:
        state: The cycle to look in.

    Returns:
        The pending request's id, or `None`.
    """
    return next((record.admission.request_id for record in state.requests if record.charge_status == "pending"), None)


def has_unresolved_charge(state: BudgetCycleState) -> bool:
    """Whether any request in this cycle settled for a charge nothing could name.

    Args:
        state: The cycle to look in.

    Returns:
        True when any request settled unresolved.
    """
    return any(record.charge_status == "unresolved" for record in state.requests)


def cycle_snapshot(state: BudgetCycleState, scope: BudgetScope) -> BudgetSnapshot:
    """What the record says about `scope` right now.

    Args:
        state: The cycle as it stands.
        scope: The trial to report on.

    Returns:
        The snapshot of the trial and its cycle.

    Raises:
        BudgetNotOpenError: the cycle holds no such trial.
    """
    trial = _require_trial(state, scope)
    trial_spent = measured_spend(requests_of(state, trial.trial_id))
    cycle_spent = measured_spend(state.requests)
    return BudgetSnapshot(
        scope=scope,
        trial_requests=len(trial.request_ids),
        trial_attempts=len(trial.attempt_ids),
        trial_measured_model_spent_usd=trial_spent,
        cycle_measured_model_spent_usd=cycle_spent,
        trial_measured_model_overshoot_usd=overshoot(trial_spent, state.limits.trial_cost_stop_usd),
        cycle_measured_model_overshoot_usd=overshoot(cycle_spent, state.limits.cycle_cost_stop_usd),
        in_flight_request_id=in_flight_request_id(state),
        unresolved_charge=has_unresolved_charge(state),
        deadline_at=trial.deadline_at,
    )


def admission_refusal(state: BudgetCycleState, scope: BudgetScope, *, at: datetime) -> BudgetRefusal | None:
    """Which rule refuses one more request for `scope` at `at`, or `None` when none does.

    Args:
        state: The cycle as it stands.
        scope: The trial asking for one more request.
        at: When the request would be admitted.

    Returns:
        The first refusing rule, or `None`.

    Raises:
        BudgetNotOpenError: the cycle holds no such trial.
        ValueError: `at` carries no timezone, so it names no instant two processes share.
    """
    trial = _require_trial(state, scope)
    require_aware(at, "an admission time")
    snapshot = cycle_snapshot(state, scope)
    rules: tuple[tuple[bool, BudgetRefusal], ...] = (
        (snapshot.trial_requests >= state.limits.trial_request_limit, "request_limit"),
        (at >= trial.deadline_at, "deadline"),
        (snapshot.trial_measured_model_spent_usd >= state.limits.trial_cost_stop_usd, "trial_cost_stop"),
        (snapshot.cycle_measured_model_spent_usd >= state.limits.cycle_cost_stop_usd, "cycle_cost_stop"),
        (snapshot.in_flight_request_id is not None, "request_in_flight"),
        (snapshot.unresolved_charge, "unresolved_charge"),
    )
    return next((refusal for refused, refusal in rules if refused), None)


def attempt_refusal(state: BudgetCycleState, scope: BudgetScope, attempt_id: str) -> BudgetRefusal | None:
    """Whether `attempt_id` is one attempt too many for this trial. A repeat is never too many.

    Args:
        state: The cycle as it stands.
        scope: The trial the attempt belongs to.
        attempt_id: The attempt to check.

    Returns:
        `"attempt_limit"` when the attempt is one too many, otherwise `None`.

    Raises:
        BudgetNotOpenError: the cycle holds no such trial.
    """
    trial = _require_trial(state, scope)
    if attempt_id in trial.attempt_ids:
        return None
    return "attempt_limit" if len(trial.attempt_ids) >= state.limits.trial_attempt_limit else None


def require_full_coverage(manifest: BudgetCoverageManifest) -> None:
    """Refuse a manifest that leaves any generative-model path outside the budgeted set.

    Args:
        manifest: The trial's coverage manifest.

    Raises:
        BudgetPreflightError: a generative path is unbudgeted, a budgeted path is not a declared
            generative path, or the manifest names an unmetered path at all.
    """
    if manifest.unmetered_model_paths:
        msg = f"unmetered model paths cannot take part in a measured trial: {', '.join(manifest.unmetered_model_paths)}"
        raise BudgetPreflightError(msg, refusal="unmetered_model_path")
    generative = set(manifest.generative_model_paths)
    budgeted = set(manifest.budgeted_model_paths)
    if generative != budgeted:
        unbudgeted = ", ".join(sorted(generative - budgeted)) or "none"
        unclaimed = ", ".join(sorted(budgeted - generative)) or "none"
        msg = f"every generative model path must be budgeted: unbudgeted {unbudgeted}, budgeted but undeclared {unclaimed}"
        raise BudgetPreflightError(msg, refusal="unbudgeted_model_path")


def other_cost_total(observations: Iterable[OtherCostObservation]) -> Decimal | None:
    """What the non-model observations add up to, or `None` when any one of them is unknown.

    Args:
        observations: The non-model charges to add up.

    Returns:
        The total, or `None`.
    """
    rows = tuple(observations)
    known = tuple(row.cost_usd for row in rows if row.cost_usd is not None)
    return sum(known, Decimal(0)) if len(known) == len(rows) else None


def _require_trial(state: BudgetCycleState, scope: BudgetScope) -> BudgetTrialState:
    if state.cycle_id != scope.cycle_id:
        msg = f"cycle {state.cycle_id!r} holds nothing for cycle {scope.cycle_id!r}"
        raise BudgetNotOpenError(msg)
    trial = trial_of(state, scope.trial_id)
    if trial is None:
        msg = f"trial {scope.trial_id!r} was never begun in cycle {scope.cycle_id!r}"
        raise BudgetNotOpenError(msg)
    return trial
