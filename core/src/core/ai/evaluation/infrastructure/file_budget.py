"""One evaluation cycle's budget as one file under a POSIX state root every participant shares.

The whole cycle is one document, `<state_root>/evaluation-budgets/<sha256(cycle_id)>.json`, beside
its own `<sha256(cycle_id)>.lock`. A cycle id names nothing on disk: it is hashed, so an id carrying
a slash or a dot cannot reach out of the directory it belongs to.

Every mutation takes that cycle's cooperative lock, reloads and validates the document, asks
`core.ai.evaluation.domain.budget` what the transition is, writes the whole new document through a
temporary file that is on disk before it is swapped in, and only then releases the lock. Two
processes admitting at the same moment therefore serialise, and the second sees the first's request
already in flight and is refused. An admission is on disk before the caller may make the request.

Restart changes nothing. An in-flight request stays in flight and an unresolved charge stays
unresolved, because the document is the only thing either was ever recorded in. Re-opening a cycle
or a trial is accepted only as the identical call: different limits, a different root or a different
start are a conflict, never a reset of the counters.

Supported for co-located processes on one verified POSIX device and canonical root. Every mutation
re-checks both against the document, so a process that mounted the same path elsewhere is refused
rather than trusted. No claim is made about a network filesystem.
"""

from __future__ import annotations

import asyncio

from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

from core.ai.evaluation.domain.budget import (
    BudgetAdmission,
    BudgetCycleState,
    BudgetRequestRecord,
    BudgetScope,
    BudgetSnapshot,
    BudgetTrialState,
    StateRootIdentity,
    admission_refusal,
    attempt_refusal,
    cycle_snapshot,
    trial_of,
)
from core.ai.evaluation.exceptions import (
    BudgetConflictError,
    BudgetNotOpenError,
    BudgetPersistenceError,
    BudgetPreflightError,
    BudgetRefusedError,
    BudgetUnreadableError,
)
from core.files import atomic_write, exclusive_lock
from core.fingerprints import content_fingerprint

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal
    from pathlib import Path

    from core.ai.evaluation.domain.budget import BudgetRole
    from core.ai.evaluation.domain.outcomes import EvaluationLimits

_BUDGET_DIRECTORY = "evaluation-budgets"
_DOCUMENT_SUFFIX = ".json"
_LOCK_SUFFIX = ".lock"


class _Transition[ResultT](Protocol):
    """One locked step: the state as it stands, and the state plus the answer it becomes."""

    def __call__(self, state: BudgetCycleState) -> tuple[BudgetCycleState, ResultT]: ...


class FileEvaluationBudgetRecord:
    """`EvaluationBudgetRecord` over one state root shared by co-located processes."""

    def __init__(self, state_root: Path) -> None:
        """Keep every cycle's record under one state root.

        Args:
            state_root: The root every participating process shares. Records go under its
                `evaluation-budgets` folder.
        """
        self.state_root = state_root

    async def begin_cycle(self, cycle_id: str, limits: EvaluationLimits, *, pricing_known: bool, at: datetime) -> None:
        """Open `cycle_id` under `limits`, or accept the identical call again without resetting it.

        Args:
            cycle_id: The cycle to open.
            limits: The stop thresholds the cycle runs under.
            pricing_known: Whether every configured role's model has a resolvable price.
            at: When the cycle starts.

        Raises:
            BudgetPreflightError: `pricing_known` is false, or this process cannot verify the
                cycle's own state root.
            BudgetConflictError: the cycle exists under different limits or a different start.
            BudgetPersistenceError: the record could not be read or written.
        """
        if not pricing_known:
            msg = f"cycle {cycle_id!r} cannot start: at least one configured role's model has no resolvable price"
            raise BudgetPreflightError(msg, refusal="pricing_unknown")
        await asyncio.to_thread(self._begin_cycle, cycle_id, limits, at)

    async def begin_trial(self, scope: BudgetScope, *, at: datetime) -> None:
        """Open `scope.trial_id` at `at`, deadline included, or accept the identical call again.

        Args:
            scope: The cycle and trial to open.
            at: When the trial starts.

        Raises:
            BudgetNotOpenError: the cycle was never begun.
            BudgetConflictError: the trial exists with a different start.
        """
        await asyncio.to_thread(self._begin_trial, scope, at)

    async def begin_attempt(self, scope: BudgetScope, attempt_id: str, *, at: datetime) -> BudgetSnapshot:
        """Record one attempt at the trial's work, or accept the identical attempt again.

        An attempt is counted rather than timed: the deadline every request is admitted against is
        the trial's own, so `at` names when the attempt began and is not recorded a second time.

        Args:
            scope: The trial the attempt belongs to.
            attempt_id: The attempt to record.
            at: When the attempt began. It is not stored.

        Returns:
            The trial's snapshot after the attempt is recorded.

        Raises:
            BudgetNotOpenError: the cycle or the trial was never begun.
            BudgetRefusedError: the trial has already spent its attempts.
        """
        del at
        return await asyncio.to_thread(self._begin_attempt, scope, attempt_id)

    async def admit(self, scope: BudgetScope, request_id: str, role: BudgetRole, *, at: datetime) -> BudgetAdmission:
        """Let one request through, on disk before the caller may make it.

        Args:
            scope: The trial the request belongs to.
            request_id: The request to admit.
            role: The model role making the request.
            at: When the request is admitted.

        Returns:
            The admission, to settle once the response arrives.

        Raises:
            BudgetNotOpenError: the cycle or the trial was never begun.
            BudgetRefusedError: a rule of the record refuses, named by `refusal`.
            BudgetConflictError: `request_id` was admitted under a different scope or role.
        """
        return await asyncio.to_thread(self._admit, scope, request_id, role, at)

    async def settle(self, admission: BudgetAdmission, charge_usd: Decimal | None, *, at: datetime) -> BudgetSnapshot:
        """Record what the admitted request actually cost. `None` leaves the cycle unresolved.

        Args:
            admission: The admission being settled.
            charge_usd: The measured charge, or `None` when nothing could price it.
            at: When the request settled.

        Returns:
            The trial's snapshot after settlement.

        Raises:
            BudgetNotOpenError: nothing was admitted under this request id.
            BudgetConflictError: it was already settled for a different charge.
        """
        return await asyncio.to_thread(self._settle, admission, charge_usd, at)

    def state_identity(self) -> StateRootIdentity:
        """The canonical root and POSIX device every mutation of this record is verified against.

        Reading it prepares the directory, so a caller building a preflight from it names the same
        root and the same device the first mutation will check.

        Returns:
            The root and device.

        Raises:
            BudgetPersistenceError: the directory or its device could not be read.
        """
        directory = self._prepared()
        return StateRootIdentity(canonical_state_root=_canonical_root(self.state_root), device_id=_device_of(directory))

    async def read(self, cycle_id: str) -> BudgetCycleState | None:
        """The cycle as it stands, or `None` when nothing was ever opened under `cycle_id`.

        Args:
            cycle_id: The cycle to read.

        Returns:
            The cycle state, or `None`.

        Raises:
            BudgetUnreadableError: the stored record is not a readable cycle state.
            BudgetPersistenceError: the record could not be read.
        """
        return await asyncio.to_thread(self._read, cycle_id)

    def _directory(self) -> Path:
        return self.state_root / _BUDGET_DIRECTORY

    def _document(self, cycle_id: str) -> Path:
        return self._directory() / f"{content_fingerprint(cycle_id)}{_DOCUMENT_SUFFIX}"

    def _lock(self, cycle_id: str) -> Path:
        return self._directory() / f"{content_fingerprint(cycle_id)}{_LOCK_SUFFIX}"

    def _read(self, cycle_id: str) -> BudgetCycleState | None:
        try:
            return _parsed(self._document(cycle_id).read_text(encoding="utf-8"), cycle_id)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as error:
            msg = f"the budget record for cycle {cycle_id!r} could not be read: {error}"
            raise BudgetPersistenceError(msg) from error

    def _begin_cycle(self, cycle_id: str, limits: EvaluationLimits, at: datetime) -> None:
        directory = self._prepared()
        with exclusive_lock(self._lock(cycle_id)):
            current = self._read(cycle_id)
            if current is None:
                opened = BudgetCycleState(
                    cycle_id=cycle_id,
                    limits=limits,
                    shared_root=_canonical_root(self.state_root),
                    device_id=_device_of(directory),
                    started_at=at,
                )
            else:
                self._require_same_root(current, directory)
                if current.limits != limits or current.started_at != at:
                    msg = f"cycle {cycle_id!r} is already open under different limits or a different start, and is never reset"
                    raise BudgetConflictError(msg)
                opened = current
            self._write(cycle_id, opened)

    def _begin_trial(self, scope: BudgetScope, at: datetime) -> None:
        def transition(state: BudgetCycleState) -> tuple[BudgetCycleState, None]:
            trial = trial_of(state, scope.trial_id)
            if trial is not None:
                if trial.started_at != at:
                    msg = f"trial {scope.trial_id!r} is already open from {trial.started_at.isoformat()}, and is never restarted"
                    raise BudgetConflictError(msg)
                return state, None
            opened = BudgetTrialState(
                trial_id=scope.trial_id, started_at=at, deadline_at=at + timedelta(seconds=state.limits.trial_deadline_seconds)
            )
            return state.model_copy(update={"trials": (*state.trials, opened)}), None

        self._mutate(scope.cycle_id, transition)

    def _begin_attempt(self, scope: BudgetScope, attempt_id: str) -> BudgetSnapshot:
        def transition(state: BudgetCycleState) -> tuple[BudgetCycleState, BudgetSnapshot]:
            refusal = attempt_refusal(state, scope, attempt_id)
            if refusal is not None:
                msg = f"trial {scope.trial_id!r} has already spent its {state.limits.trial_attempt_limit} attempts"
                raise BudgetRefusedError(msg, refusal=refusal)
            trial = _trial(state, scope)
            if attempt_id in trial.attempt_ids:
                return state, cycle_snapshot(state, scope)
            recorded = _with_trial(state, trial.model_copy(update={"attempt_ids": (*trial.attempt_ids, attempt_id)}))
            return recorded, cycle_snapshot(recorded, scope)

        return self._mutate(scope.cycle_id, transition)

    def _admit(self, scope: BudgetScope, request_id: str, role: BudgetRole, at: datetime) -> BudgetAdmission:
        def transition(state: BudgetCycleState) -> tuple[BudgetCycleState, BudgetAdmission]:
            trial = _trial(state, scope)
            existing = next((record for record in state.requests if record.admission.request_id == request_id), None)
            if existing is not None:
                if existing.admission.scope != scope or existing.admission.role != role:
                    msg = f"request {request_id!r} was already admitted for another scope or role"
                    raise BudgetConflictError(msg)
                return state, existing.admission
            refusal = admission_refusal(state, scope, at=at)
            if refusal is not None:
                msg = f"trial {scope.trial_id!r} admits no further request: {refusal}"
                raise BudgetRefusedError(msg, refusal=refusal)
            admission = BudgetAdmission(
                scope=scope, request_id=request_id, role=role, admitted_at=at, trial_request_number=len(trial.request_ids) + 1
            )
            admitted = _with_trial(state, trial.model_copy(update={"request_ids": (*trial.request_ids, request_id)}))
            recorded = admitted.model_copy(
                update={"requests": (*admitted.requests, BudgetRequestRecord(admission=admission, charge_status="pending"))}
            )
            return recorded, admission

        return self._mutate(scope.cycle_id, transition)

    def _settle(self, admission: BudgetAdmission, charge_usd: Decimal | None, at: datetime) -> BudgetSnapshot:
        def transition(state: BudgetCycleState) -> tuple[BudgetCycleState, BudgetSnapshot]:
            existing = next((record for record in state.requests if record.admission.request_id == admission.request_id), None)
            if existing is None:
                msg = f"request {admission.request_id!r} was never admitted, so there is nothing to settle"
                raise BudgetNotOpenError(msg)
            if existing.charge_status != "pending":
                if existing.charge_usd != charge_usd:
                    msg = f"request {admission.request_id!r} already settled for {_charge_text(existing.charge_usd)}"
                    raise BudgetConflictError(msg)
                return state, cycle_snapshot(state, admission.scope)
            status = "known" if charge_usd is not None else "unresolved"
            settled = existing.model_copy(update={"charge_status": status, "settled_at": at, "charge_usd": charge_usd})
            rows = tuple(settled if record.admission.request_id == admission.request_id else record for record in state.requests)
            recorded = state.model_copy(update={"requests": rows})
            return recorded, cycle_snapshot(recorded, admission.scope)

        return self._mutate(admission.scope.cycle_id, transition)

    def _mutate[ResultT](self, cycle_id: str, transition: _Transition[ResultT]) -> ResultT:
        directory = self._prepared()
        with exclusive_lock(self._lock(cycle_id)):
            current = self._read(cycle_id)
            if current is None:
                msg = f"cycle {cycle_id!r} was never begun"
                raise BudgetNotOpenError(msg)
            self._require_same_root(current, directory)
            state, result = transition(current)
            self._write(cycle_id, state)
            return result

    def _prepared(self) -> Path:
        directory = self._directory()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            msg = f"the budget directory at {directory} could not be opened: {error}"
            raise BudgetPersistenceError(msg) from error
        return directory

    def _write(self, cycle_id: str, state: BudgetCycleState) -> None:
        try:
            atomic_write(self._document(cycle_id), state.model_dump_json(indent=2))
        except OSError as error:
            msg = f"the budget record for cycle {cycle_id!r} could not be written: {error}"
            raise BudgetPersistenceError(msg) from error

    def _require_same_root(self, state: BudgetCycleState, directory: Path) -> None:
        """Refuse a process that cannot see this cycle's own root on this cycle's own device."""
        root = _canonical_root(self.state_root)
        device = _device_of(directory)
        if state.shared_root != root or state.device_id != device:
            msg = (
                f"cycle {state.cycle_id!r} was opened on {state.shared_root} (device {state.device_id}), "
                f"and this process verifies {root} (device {device})"
            )
            raise BudgetPreflightError(msg, refusal="unverified_state_root")


def _trial(state: BudgetCycleState, scope: BudgetScope) -> BudgetTrialState:
    trial = trial_of(state, scope.trial_id)
    if trial is None:
        msg = f"trial {scope.trial_id!r} was never begun in cycle {scope.cycle_id!r}"
        raise BudgetNotOpenError(msg)
    return trial


def _with_trial(state: BudgetCycleState, trial: BudgetTrialState) -> BudgetCycleState:
    return state.model_copy(update={"trials": tuple(trial if row.trial_id == trial.trial_id else row for row in state.trials)})


def _canonical_root(state_root: Path) -> str:
    return str(state_root.resolve())


def _device_of(directory: Path) -> int:
    try:
        return directory.stat().st_dev
    except OSError as error:
        msg = f"the device holding {directory} could not be read: {error}"
        raise BudgetPersistenceError(msg) from error


def _charge_text(charge_usd: Decimal | None) -> str:
    return "an unresolved charge" if charge_usd is None else f"USD {charge_usd}"


def _parsed(content: str, cycle_id: str) -> BudgetCycleState:
    """The cycle `content` holds, refusing anything that is no longer a readable budget record."""
    try:
        return BudgetCycleState.model_validate_json(content)
    except ValueError as error:
        msg = f"the budget record for cycle {cycle_id!r} is not a readable cycle state"
        raise BudgetUnreadableError(msg) from error
