"""What a live evaluation trial spends, and the model wrapper that makes every request ask first.

`BudgetedModel` wraps every designer, worker, reviewer and grader model of a measured trial. Both
request paths, the plain `request` and the streamed `request_stream`, admit before the provider is
reached and settle exactly once afterwards. Settlement is exact where a price can be resolved and
unresolved where it cannot. Every early close, cancellation, partial stream or failure also settles
unresolved. An unresolved settlement blocks every later admission until a person looks at it. The
deployment's own `SpendLimits` stays mounted beside the record and refuses a request before it
reaches this wrapper. Whichever of the two is stricter stops a run. Neither reports the other's
numbers. `count_tokens` and `compact_messages` are provider calls the record cannot meter, so both
refuse.
"""

from __future__ import annotations

import asyncio

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from uuid import uuid4

from core.ai.evaluation.contracts import BudgetRole, BudgetScope, EvaluationBudgetRecord, ResponseCharge
from core.cqrs.contracts import utcnow
from pydantic_ai.models.wrapper import WrapperModel

from harness.adapters.contracts import priced_cost
from harness.features.evals.domain.pilot import METERED_ROLE_OF_TIER
from harness.features.evals.exceptions import UnmeteredModelCallError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from datetime import datetime
    from decimal import Decimal

    from core.ai.workflows.contracts import Tier
    from pydantic_ai.messages import ModelMessage, ModelResponse
    from pydantic_ai.models import Model, ModelRequestContext, ModelRequestParameters, StreamedResponse
    from pydantic_ai.settings import ModelSettings
    from pydantic_ai.tools import AgentDepsT, RunContext
    from pydantic_ai.usage import RequestUsage


class BudgetedModel(WrapperModel):
    """A model whose every request is admitted by the record first and settled exactly once after.

    One instance covers one role of one trial. The wrapped model is reached only once admission is
    on disk, so a refusal costs nothing and a crash between the two leaves a request in flight
    rather than a charge nobody recorded.
    """

    record: EvaluationBudgetRecord
    scope: BudgetScope
    role: BudgetRole
    charge: ResponseCharge
    clock: Callable[[], datetime]

    def __init__(
        self,
        wrapped: Model,
        *,
        record: EvaluationBudgetRecord,
        scope: BudgetScope,
        role: BudgetRole,
        charge: ResponseCharge,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        """Wrap `wrapped` so every request it makes is admitted and settled on `record`.

        Args:
            wrapped: The model the requests reach.
            record: The budget record that admits and settles each request.
            scope: The cycle and trial the requests are charged to.
            role: The budget role the requests spend under.
            charge: What prices a complete response.
            clock: The clock admissions and settlements are stamped with.
        """
        super().__init__(wrapped)
        self.record = record
        self.scope = scope
        self.role = role
        self.charge = charge
        self.clock = clock

    async def request(
        self, messages: list[ModelMessage], model_settings: ModelSettings | None, model_request_parameters: ModelRequestParameters
    ) -> ModelResponse:
        """Admit, make the request, and settle what the response actually cost.

        Both settlements are shielded from cancellation, so a charge this already knows lands
        rather than leaving the cycle blocked on a request nobody will ever settle. An exception
        settles unresolved.

        Args:
            messages: The messages to send.
            model_settings: The settings for this request.
            model_request_parameters: The tools and output settings for this request.

        Returns:
            The provider's response.

        Raises:
            BudgetNotOpenError: the cycle or the trial was never begun.
            BudgetRefusedError: a rule of the record refuses the request.
        """
        admission = await self.record.admit(self.scope, uuid4().hex, self.role, at=self.clock())
        try:
            response = await super().request(messages, model_settings, model_request_parameters)
        except BaseException:
            await asyncio.shield(self.record.settle(admission, None, at=self.clock()))
            raise
        await asyncio.shield(self.record.settle(admission, self.charge(response), at=self.clock()))
        return response

    async def count_tokens(
        self, messages: list[ModelMessage], model_settings: ModelSettings | None, model_request_parameters: ModelRequestParameters
    ) -> RequestUsage:
        """Refuse. A token count is a provider round trip this record neither admits nor settles.

        Args:
            messages: The messages that would be counted.
            model_settings: The settings for the count.
            model_request_parameters: The tools and output settings for the count.

        Returns:
            Nothing. It always raises.

        Raises:
            UnmeteredModelCallError: always, under a budgeted scope.
        """
        del messages, model_settings, model_request_parameters
        msg = f"counting tokens for the {self.role} model is a provider call the budget record cannot meter"
        raise UnmeteredModelCallError(msg)

    async def compact_messages(self, request_context: ModelRequestContext, *, instructions: str | None = None) -> ModelResponse:
        """Refuse. Compaction is a billed generative request on the providers that implement it.

        Args:
            request_context: The request that would be compacted.
            instructions: The compaction instructions.

        Returns:
            Nothing. It always raises.

        Raises:
            UnmeteredModelCallError: always, under a budgeted scope.
        """
        del request_context, instructions
        msg = f"compacting the {self.role} model's messages is a generative request the budget record cannot meter"
        raise UnmeteredModelCallError(msg)

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[AgentDepsT] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        """Admit before the provider context is entered, and settle the stream exactly once.

        A stream that ran to the end is priced from the complete response it built. Every other
        ending (an early close, a cancellation, an interrupted or incomplete state, an iteration
        that raised, a provider that failed before answering) settles unresolved. Settlement runs
        shielded from cancellation, and a cancellation that stops it from being confirmed leaves the
        request in flight, where it blocks the next admission instead of vanishing.

        Args:
            messages: The messages to send.
            model_settings: The settings for this request.
            model_request_parameters: The tools and output settings for this request.
            run_context: The agent run's context, or `None` outside a run.

        Yields:
            The provider's streamed response.

        Raises:
            BudgetNotOpenError: the cycle or the trial was never begun.
            BudgetRefusedError: a rule of the record refuses the request.
        """
        admission = await self.record.admit(self.scope, uuid4().hex, self.role, at=self.clock())
        settled = False

        async def settle_once(charge_usd: Decimal | None) -> None:
            nonlocal settled
            if settled:
                return
            settled = True
            await asyncio.shield(self.record.settle(admission, charge_usd, at=self.clock()))

        try:
            async with self.wrapped.request_stream(messages, model_settings, model_request_parameters, run_context) as stream:
                try:
                    yield stream
                except BaseException:
                    await settle_once(None)
                    raise
                else:
                    response = stream.get()
                    charge = self.charge(response) if response.state == "complete" else None
                    await settle_once(charge)
        except BaseException:
            await settle_once(None)
            raise
        finally:
            await settle_once(None)


class PilotMeter:
    """The `StepModelWrapper` a measured pilot installs, so every step agent's model asks the record first.

    Handed to the environment builder as `wrap_model`, it returns a `BudgetedModel` for every tier the
    environment asks about and remembers each one in `mounted`. A non-empty `mounted` is evidence that
    the hook ran, never evidence about the environment a journey uses. Call `for_trial` between trials
    and never during one: a request in flight settles against whichever trial the scope names when it
    settles.
    """

    def __init__(
        self,
        record: EvaluationBudgetRecord,
        cycle_id: str,
        *,
        charge: ResponseCharge = priced_cost,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        """Meter every model mounted through this hook on `record`, under `cycle_id`.

        Until `for_trial` is called, the record refuses every request a mounted model makes.

        Args:
            record: The budget record every mounted model admits and settles on.
            cycle_id: The cycle the pilot spends under.
            charge: What prices a complete response.
            clock: The clock admissions and settlements are stamped with.
        """
        self.record = record
        self.cycle_id = cycle_id
        self.charge = charge
        self.clock = clock
        self.trial_id = _UNSTARTED_TRIAL
        self.mounted: list[BudgetedModel] = []

    def __call__(self, model: Model, *, role: Tier) -> Model:
        """The budgeted model a step agent of `role` runs on, and a record of having mounted it.

        Args:
            model: The step agent's own model.
            role: The step agent's tier, which decides the budget role it spends under.

        Returns:
            `model` wrapped in a `BudgetedModel` for the current trial.
        """
        budgeted = BudgetedModel(
            model,
            record=self.record,
            scope=BudgetScope(cycle_id=self.cycle_id, trial_id=self.trial_id),
            role=METERED_ROLE_OF_TIER[role],
            charge=self.charge,
            clock=self.clock,
        )
        self.mounted.append(budgeted)
        return budgeted

    def for_trial(self, trial_id: str) -> None:
        """Point every model this meter mounted at `trial_id`, so their requests are that trial's.

        Args:
            trial_id: The trial now being performed.
        """
        self.trial_id = trial_id
        scope = BudgetScope(cycle_id=self.cycle_id, trial_id=trial_id)
        for budgeted in self.mounted:
            budgeted.scope = scope

    @property
    def metered_roles(self) -> tuple[BudgetRole, ...]:
        """Which budget roles this meter has actually mounted a model for, in mount order."""
        return tuple(budgeted.role for budgeted in self.mounted)


_UNSTARTED_TRIAL = "trial-not-started"
"""The trial a meter names before any trial has begun, never opened, so the record refuses its requests."""
