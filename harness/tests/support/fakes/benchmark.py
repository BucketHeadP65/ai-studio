"""Hand-written doubles for the post-pilot benchmark: its two arms, its grader, and its reviews.

Every model here is a `FunctionModel` double with a `BudgetedModel` mounted on it by the test, so
what these prove is the real admission path over the real file-backed record. Nothing reaches a
provider and nothing is patched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.evaluation.contracts import EvaluationError, ResourceUsage, TaskGrade
from core.ai.workflows.contracts import Outcome
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from harness.features.evals.domain.pilot import WorkflowAutomationExecution
from harness.features.evals.infrastructure.budgeted_model import BudgetedModel

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from core.ai.evaluation.contracts import EvaluationInput, OwnerReview, RunIdentity, TaskCase
    from core.ai.workflows.contracts import OutcomeStatus
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models import Model
    from pydantic_ai.models.function import AgentInfo

    from harness.features.evals.domain.benchmark import UsageSource
    from harness.features.evals.internal_contracts import BenchmarkModel

type Play = Callable[[BudgetedModel], Awaitable[int]]
"""One thing a row wants done inside a slot before the arm asks, answering how many requests it made."""


def budgeted_model_of(declared: BenchmarkModel) -> BudgetedModel:
    """The budgeted object behind a declared path, for a play that wants the record underneath it."""
    assert isinstance(declared.model, BudgetedModel), "this path was mounted on the record"
    return declared.model


def answers(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
    """The one reply every double here gives, with a plain request's worth of usage on it."""
    del messages, info
    return ModelResponse(parts=[TextPart(content="answered")], usage=RequestUsage(input_tokens=10, output_tokens=5))


class ProviderNamingModel(FunctionModel):
    """A double that names a provider, so a preflight reads it as a model that would reach one."""

    @property
    def system(self) -> str:
        """The provider name this double claims, which is what makes it a live path to a preflight."""
        return "openai"


async def ask(model: Model) -> ModelResponse:
    """One plain model request, the same call a step agent makes."""
    return await model.request([], None, ModelRequestParameters())


class ScriptedArm:
    """A benchmark arm that performs whatever the row scripted for this slot, then asks its model once.

    `plays` is one coroutine per slot, in slot order, and a slot past the end of the script just
    asks. Whatever a play raises reaches the benchmark unchanged, so a record refusing a request is
    a refused slot rather than a swallowed one. `checked` is the status of the run every slot hands
    back, the way an archived workflow run carries its own checked outcome.
    """

    def __init__(
        self,
        model: BenchmarkModel,
        *,
        name: str,
        plays: Sequence[Play] = (),
        source: UsageSource = "this_record",
        checked: OutcomeStatus = "succeeded",
    ) -> None:
        self.declared = model
        self.checked: OutcomeStatus = checked
        self.source: UsageSource = source
        self.model = model.model
        self.name = name
        self.plays = list(plays)
        self.slots: list[str] = []

    @property
    def request_paths(self) -> tuple[BenchmarkModel, ...]:
        """The one declared path this arm asks through."""
        return (self.declared,)

    @property
    def usage_source(self) -> UsageSource:
        """This record, which is what makes this arm's own count readable against it."""
        return self.source

    async def __call__(self, case: EvaluationInput, identity: RunIdentity, repetition: int) -> WorkflowAutomationExecution:
        """Play this slot's script, ask the model, and hand back what this arm produced."""
        del identity
        slot = f"{case.case_id}-{repetition}"
        self.slots.append(slot)
        asked = 0
        if self.plays:
            asked += await self.plays.pop(0)(budgeted_model_of(self.declared))
        await ask(self.model)
        asked += 1
        return WorkflowAutomationExecution(
            output=f"{self.name} answered {case.case_id}",
            usage=ResourceUsage(requests=asked),
            run_id=f"{self.name}-{slot}",
            trial_id=f"{self.name}-{slot}",
            result=Outcome(status=self.checked, summary=f"{self.name} did everything {case.case_id} asked, and did it well"),
            diagnostics=(),
        )


class ScriptedGrader:
    """A grader that makes its own model request first, then answers what the row said it finds.

    The request is the point: a grader spends on the same trial as the arm it is grading, and a
    record that did not count it would report a trial that cost less than it did.
    """

    def __init__(
        self,
        model: BenchmarkModel,
        *,
        verdicts: Mapping[str, bool] | None = None,
        source: UsageSource = "this_record",
        trips_on_slot: int | None = None,
        elsewhere: Model | None = None,
    ) -> None:
        self.declared = model
        self.source: UsageSource = source
        self.model = model.model
        self.verdicts = dict(verdicts or {})
        self.trips_on_slot = trips_on_slot
        self.elsewhere = elsewhere
        self.graded: list[str] = []

    @property
    def request_paths(self) -> tuple[BenchmarkModel, ...]:
        """The one declared path this grader asks through."""
        return (self.declared,)

    @property
    def usage_source(self) -> UsageSource:
        """This record, which is what makes this grader's own count readable against it."""
        return self.source

    async def __call__(self, case: TaskCase, output: str) -> TaskGrade:
        """Ask the model, then grade this case the way the row scripted it."""
        del output
        if self.trips_on_slot == len(self.graded) and self.elsewhere is not None:
            await ask(self.elsewhere)
            self.graded.append(case.case_id)
            return TaskGrade(success=True, reason=f"scripted verdict for {case.case_id}", usage=ResourceUsage(requests=1))
        await ask(self.model)
        self.graded.append(case.case_id)
        success = self.verdicts.get(case.case_id, True)
        return TaskGrade(success=success, reason=f"scripted verdict for {case.case_id}", usage=ResourceUsage(requests=1))


class FakeOwnerReviews:
    """What owners filed against each run, keyed by run id. A run with no entry has no review."""

    def __init__(self, filed: Mapping[str, tuple[OwnerReview, ...]] | None = None) -> None:
        self.filed = dict(filed or {})
        self.asked: list[str] = []

    async def for_run(self, run_id: str) -> tuple[OwnerReview, ...]:
        """Every review committed against `run_id`, in the order they were appended."""
        self.asked.append(run_id)
        return self.filed.get(run_id, ())


class RogueArm:
    """An arm that names one path and asks through another, which no statement about itself can catch.

    Its usage is honest, because a real runner's request count comes from the library that made the
    requests rather than from the runner's opinion of them. That honesty is the one thing readable
    from outside, and holding it against what the record admitted is what catches this.
    """

    def __init__(
        self, declared: tuple[BenchmarkModel, ...], *, asks: Model, name: str, requests: int = 2, source: UsageSource = "this_record", raises: str = ""
    ) -> None:
        self.declared = declared
        self.source: UsageSource = source
        self.raises = raises
        self.asks = asks
        self.name = name
        self.requests = requests
        self.slots: list[str] = []

    @property
    def request_paths(self) -> tuple[BenchmarkModel, ...]:
        """Whatever this arm says it asks through, which is not where it asks."""
        return self.declared

    @property
    def usage_source(self) -> UsageSource:
        """Whatever this arm says about where its figures come from."""
        return self.source

    async def __call__(self, case: EvaluationInput, identity: RunIdentity, repetition: int) -> WorkflowAutomationExecution:
        """Ask the model nobody mounted, and report the requests that really happened."""
        del identity
        slot = f"{case.case_id}-{repetition}"
        self.slots.append(slot)
        for _ in range(self.requests):
            await ask(self.asks)
        if self.raises:
            raise EvaluationError(self.raises, usage=ResourceUsage(requests=self.requests))
        return WorkflowAutomationExecution(
            output=f"{self.name} answered {case.case_id}",
            usage=ResourceUsage(requests=self.requests),
            run_id=f"{self.name}-{slot}",
            trial_id=f"{self.name}-{slot}",
            diagnostics=(),
        )


class RogueGrader:
    """A grader that asks through a model the benchmark never mounted, naming whatever it likes.

    `declared` empty is the case that needs nobody to lie: a custodian's own grader truthfully says
    it asks through none of the benchmark's declared paths, and builds its own model.
    """

    def __init__(
        self, declared: tuple[BenchmarkModel, ...], *, asks: Model, requests: int = 1, source: UsageSource = "this_record", raises: str = ""
    ) -> None:
        self.declared = declared
        self.source: UsageSource = source
        self.raises = raises
        self.asks = asks
        self.requests = requests
        self.graded: list[str] = []

    @property
    def request_paths(self) -> tuple[BenchmarkModel, ...]:
        """Whatever this grader says it asks through, which is not where it asks."""
        return self.declared

    @property
    def usage_source(self) -> UsageSource:
        """Whatever this grader says about where its figures come from."""
        return self.source

    async def __call__(self, case: TaskCase, output: str) -> TaskGrade:
        """Ask the model nobody mounted, and report the requests that really happened."""
        del output
        for _ in range(self.requests):
            await ask(self.asks)
        if self.raises:
            raise EvaluationError(self.raises, usage=ResourceUsage(requests=self.requests))
        self.graded.append(case.case_id)
        return TaskGrade(success=True, reason=f"rogue verdict for {case.case_id}", usage=ResourceUsage(requests=self.requests))
