"""The registry of steps: the implementations where a workflow's logic lives.

The workflow grammar never grows logic. It grows references to steps in this registry. A
registered step is an async callable with full type annotations. Its parameter names, parameter
types and return type are read once at registration and become the contract the validator
checks every workflow's wiring against. A callable that cannot state its own types cannot be a step.
"""

from __future__ import annotations

import inspect

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, get_type_hints

from core.ai.workflows.authoring import including as _including
from core.ai.workflows.exceptions import RegistrationError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from core.ai.workflows.authoring import Problem
    from core.ai.workflows.domain.vocabulary import Workflow
    from core.ai.workflows.internal_contracts import AgentResolver, AgentRunner, CodeRunner, OrchestratorDecider, PerformStep


@dataclass(frozen=True)
class StepImplementation:
    """One step: the callable plus its introspected contract."""

    name: str
    function: Callable[..., Awaitable[object]]
    input_types: Mapping[str, type]
    required: frozenset[str]
    """Parameter names without defaults. A workflow must wire every one of them."""
    output_type: type
    replay_safe: bool = False
    """Whether running this step's body again is indistinguishable from not running it again.

    A durable engine may then run it in its own replayed code, so a finer run log inside the step,
    such as an agent's one entry per model request, becomes the retry unit.

    A step qualifies only when every effect it has is safe to replay, not only its return value.
    Pure computation and agents that log their own model requests qualify. Writing a file, running
    a command or touching a database does not. False by default. Nothing checks it: the integrator
    asserts it per step.
    """


class StepRegistry:
    """What this run can execute: the steps somebody registered, and the terms for building a step a workflow declares.

    A workflow names steps, the validator checks its wiring against their contracts, and the graph
    builder calls them. Extending a registry with a workflow's own steps always reads the authoring
    terms (`perform`, `grants` and the rest) off the registry it extends, so no caller can pass
    steps that disagree with their terms. `perform`, `grants` and `capability_grants` are the
    host's terms and bound what a workflow can reach. A saved workflow may invent a type and a
    step, but never a database, a credential, a tool, a capability or an approver.
    """

    def __init__(
        self,
        *,
        perform: PerformStep | None = None,
        grants: Sequence[str] = (),
        run_code: CodeRunner | None = None,
        run_agent: AgentRunner | None = None,
        resolve_agent: AgentResolver | None = None,
        capability_grants: Sequence[str] = (),
        decide_gate: OrchestratorDecider | None = None,
        judge_tools: frozenset[str] | None = None,
    ) -> None:
        """One registry: this run's steps, under this run's authoring terms.

        Args:
            perform: What performing a briefing means here. `None` refuses, with a reason, a
                workflow that declares steps. A workflow naming only registered steps is unaffected.
            grants: Tool names a declared step may ask for. Empty means it reasons from its
                inputs alone.
            run_code: What running an inline program means here (which sandbox). `None` refuses,
                with a reason, a workflow that carries one.
            run_agent: What running an inline agent step means here (the host's `Agent.from_spec`).
                `None` refuses, with a reason, a workflow that carries one.
            resolve_agent: What an `AgentStep.agent` string reference means here. `None` refuses,
                with a reason, an agent step naming one. An inline agent document is unaffected
                either way.
            capability_grants: pydantic-ai capability names an agent step may declare. Empty means
                none.
            decide_gate: This run's standing orchestrator, which answers an `OrchestratorGate`.
                `None` refuses, with a reason, a workflow declaring one. Such a gate is never
                passed without an answer.
            judge_tools: Tool names a judge's inline step may hold. `None` leaves a judge's tools
                unchecked.
        """
        self._works: dict[str, StepImplementation] = {}
        self.perform = perform
        self.grants: tuple[str, ...] = tuple(grants)
        self.run_code = run_code
        self.run_agent = run_agent
        self.resolve_agent = resolve_agent
        self.capability_grants: tuple[str, ...] = tuple(capability_grants)
        self.decide_gate = decide_gate
        self.judge_tools = judge_tools

    @property
    def performs_briefings(self) -> bool:
        """Whether this run can build a step a workflow declares, not only name a registered one."""
        return self.perform is not None

    @property
    def runs_programs(self) -> bool:
        """Whether this run has a sandbox, and so can build an inline program."""
        return self.run_code is not None

    @property
    def runs_agents(self) -> bool:
        """Whether this run has an agent runner, and so can build an inline agent step."""
        return self.run_agent is not None

    @property
    def decides_gates(self) -> bool:
        """Whether this run has an orchestrator, and so can answer a gate that names no person."""
        return self.decide_gate is not None

    def including(self, workflow: Workflow) -> tuple[StepRegistry, tuple[Problem, ...]]:
        """A registry holding this one's steps plus the inline steps of `workflow`, and the problems found.

        This registry is never changed, and nothing is raised, so the validator can report every
        problem at once. A step that fails to build is absent from the returned registry, so the
        wiring checks also report the nodes naming it as naming an unknown step.

        Args:
            workflow: The workflow whose inline steps to add.

        Returns:
            The extended registry, and one problem per inline step that did not build.
        """
        return _including(self, workflow)

    def register(self, fn: Callable[..., object], *, name: str | None = None, replay_safe: bool = False) -> StepImplementation:
        """Register one step, reading its contract from its annotations.

        `fn` is typed as any callable. A sync function is refused here, with a message, not later
        at execution.

        Args:
            fn: An async callable with every parameter and the return type annotated.
            name: Registry name. Defaults to ``fn.__name__``.
            replay_safe: The integrator's assertion that running this body again is unobservable,
                as `StepImplementation.replay_safe` describes. Off unless stated.

        Returns:
            The introspected implementation.

        Raises:
            RegistrationError: If ``fn`` is not async, lacks annotations, annotates anything as
                `Any`, or the name is taken.
        """
        work_name = name if name is not None else getattr(fn, "__name__", repr(fn))
        if work_name in self._works:
            msg = f"work {work_name!r} is already registered"
            raise RegistrationError(msg)
        if not inspect.iscoroutinefunction(fn):
            msg = f"work {work_name!r} must be an async function"
            raise RegistrationError(msg)

        hints = get_type_hints(fn)
        signature = inspect.signature(fn)
        missing = [p for p in signature.parameters if p not in hints]
        if missing:
            msg = f"work {work_name!r} has unannotated parameters: {', '.join(missing)}"
            raise RegistrationError(msg)
        if "return" not in hints:
            msg = f"work {work_name!r} has no return annotation"
            raise RegistrationError(msg)
        vague = sorted(name for name, hint in hints.items() if hint is Any)
        if vague:  # Any states nothing, and a step must state its types
            msg = f"work {work_name!r} has Any annotations: {', '.join(vague)}"
            raise RegistrationError(msg)

        required = frozenset(p_name for p_name, p in signature.parameters.items() if p.default is inspect.Parameter.empty)
        implementation = StepImplementation(
            name=work_name,
            function=fn,
            input_types={p: hints[p] for p in signature.parameters},
            required=required,
            output_type=hints["return"],
            replay_safe=replay_safe,
        )
        self._works[work_name] = implementation
        return implementation

    def get(self, name: str) -> StepImplementation | None:
        """The step registered under `name`, or None when nothing is.

        Args:
            name: The step's registry name.

        Returns:
            The step, or None.
        """
        return self._works.get(name)

    def copy(self) -> StepRegistry:
        """A registry with the same steps and the same terms, safe to extend without changing this one.

        A workflow's inline steps (`Workflow.inline`) go into a copy made for that workflow, so they
        never widen the steps a *different* workflow is validated against.

        Returns:
            The copy.
        """
        clone = StepRegistry(
            perform=self.perform,
            grants=self.grants,
            run_code=self.run_code,
            run_agent=self.run_agent,
            resolve_agent=self.resolve_agent,
            capability_grants=self.capability_grants,
            decide_gate=self.decide_gate,
            judge_tools=self.judge_tools,
        )
        clone._works = dict(self._works)
        return clone

    @property
    def names(self) -> tuple[str, ...]:
        """The registered step names, sorted: what this run can execute."""
        return tuple(sorted(self._works))

    def __contains__(self, name: str) -> bool:
        return name in self._works
