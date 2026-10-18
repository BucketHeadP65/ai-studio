"""Budget records that report what was asked of them, or die in the middle of answering.

`CountingLedger` is the real file-backed record with a counter around it, so a test can say a
request was settled exactly once rather than settled with the same figure twice. `LostLedger` is
the same record with a settlement that never lands, which is what a process killed between
admitting and settling leaves behind. `GatedLedger` holds a settlement open until the test lets it
through, which is how a test lands a cancellation in the middle of one. `BlindFirstReadLedger` is
the real record with one blind spot, which is how a test lands two callers opening one cycle at once.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal

    from core.ai.evaluation.contracts import (
        BudgetCycleState,
        BudgetRole,
        BudgetScope,
        BudgetSnapshot,
        EvaluationBudgetRecord,
        EvaluationLimits,
        FileEvaluationBudgetRecord,
        StateRootIdentity,
    )
    from core.ai.evaluation.domain.budget import BudgetAdmission


class SettlementLostError(RuntimeError):
    """The settlement never reached the record, the way a killed process never reaches it."""


class CountingLedger:
    """The record underneath, plus the admissions and settlements it was actually asked for."""

    def __init__(self, ledger: EvaluationBudgetRecord) -> None:
        self.ledger = ledger
        self.admitted: list[str] = []
        self.settlements: list[tuple[str, Decimal | None]] = []

    async def begin_cycle(self, cycle_id: str, limits: EvaluationLimits, *, pricing_known: bool, at: datetime) -> None:
        """Open the cycle on the record underneath."""
        await self.ledger.begin_cycle(cycle_id, limits, pricing_known=pricing_known, at=at)

    async def begin_trial(self, scope: BudgetScope, *, at: datetime) -> None:
        """Open the trial on the record underneath."""
        await self.ledger.begin_trial(scope, at=at)

    async def begin_attempt(self, scope: BudgetScope, attempt_id: str, *, at: datetime) -> BudgetSnapshot:
        """Record the attempt on the record underneath."""
        return await self.ledger.begin_attempt(scope, attempt_id, at=at)

    async def admit(self, scope: BudgetScope, request_id: str, role: BudgetRole, *, at: datetime) -> BudgetAdmission:
        """Admit through the record underneath, remembering the request id it let through."""
        admission = await self.ledger.admit(scope, request_id, role, at=at)
        self.admitted.append(admission.request_id)
        return admission

    async def settle(self, admission: BudgetAdmission, charge_usd: Decimal | None, *, at: datetime) -> BudgetSnapshot:
        """Settle through the record underneath, remembering the figure it was settled for."""
        self.settlements.append((admission.request_id, charge_usd))
        return await self.ledger.settle(admission, charge_usd, at=at)


class LostLedger(CountingLedger):
    """A record that admits and then loses every settlement, leaving the request in flight."""

    async def settle(self, admission: BudgetAdmission, charge_usd: Decimal | None, *, at: datetime) -> BudgetSnapshot:
        """Fail before the settlement is recorded anywhere."""
        del at
        self.settlements.append((admission.request_id, charge_usd))
        msg = f"the process settling {admission.request_id!r} did not get that far"
        raise SettlementLostError(msg)


class GatedLedger(CountingLedger):
    """A record whose settlement waits for the test before it lands.

    `settling` is set the moment a settlement is entered, `release` is what the test opens to let
    it finish, and `settled` is set once the record underneath has actually recorded it.
    """

    def __init__(self, ledger: EvaluationBudgetRecord) -> None:
        super().__init__(ledger)
        self.settling = asyncio.Event()
        self.release = asyncio.Event()
        self.settled = asyncio.Event()

    async def settle(self, admission: BudgetAdmission, charge_usd: Decimal | None, *, at: datetime) -> BudgetSnapshot:
        """Announce the settlement, wait to be let through, then record it."""
        self.settling.set()
        await self.release.wait()
        snapshot = await super().settle(admission, charge_usd, at=at)
        self.settled.set()
        return snapshot


class BlindFirstReadLedger:
    """The real file-backed record, whose first read answers `None` however the record stands.

    That is the window a race opens in: two processes both look, both see nothing, and both try to
    open the same cycle. The second one's write meets the first one's on disk. Everything else here
    is the record underneath, so what the second caller meets is the real lock and the real message.
    """

    def __init__(self, ledger: FileEvaluationBudgetRecord) -> None:
        self.ledger = ledger
        self.blind = True

    def state_identity(self) -> StateRootIdentity:
        """The root and device the record underneath holds itself to."""
        return self.ledger.state_identity()

    async def begin_cycle(self, cycle_id: str, limits: EvaluationLimits, *, pricing_known: bool, at: datetime) -> None:
        """Open the cycle on the record underneath, meeting whatever another caller already wrote."""
        await self.ledger.begin_cycle(cycle_id, limits, pricing_known=pricing_known, at=at)

    async def begin_trial(self, scope: BudgetScope, *, at: datetime) -> None:
        """Open the trial on the record underneath."""
        await self.ledger.begin_trial(scope, at=at)

    async def read(self, cycle_id: str) -> BudgetCycleState | None:
        """Nothing, the first time. The record's own answer every time after."""
        if self.blind:
            self.blind = False
            return None
        return await self.ledger.read(cycle_id)
