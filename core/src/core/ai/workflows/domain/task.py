"""Typed work items: a job as a contract, separate from whoever executes it.

A step can be a plain function or an agentic task (instructions, its own tools, an output
contract), and both register the same way. Schemas are real JSON Schema derived from the
annotations, so a step is done when its output validates, not when the model stops.
"""

from __future__ import annotations

import inspect

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from pydantic.json_schema import JsonSchemaValue

    from core.ai.workflows.registry import StepImplementation


@dataclass(frozen=True)
class TaskContract:
    """One step's contract, as schemas rather than prose.

    Named inputs with their shapes, and the shape of what comes back.
    """

    name: str
    description: str
    inputs: Mapping[str, JsonSchemaValue]
    output: JsonSchemaValue
    required: tuple[str, ...]
    tools: tuple[str, ...] = ()
    """Tool names, if agentic. Empty for a plain function step."""

    @property
    def is_agentic(self) -> bool:
        """Whether the step carries tools, which makes it agentic."""
        return bool(self.tools)


def contract_of(implementation: StepImplementation, *, tools: Sequence[str] = ()) -> TaskContract:
    """Derive a step's contract from its introspected types.

    Schemas come from the same annotations the validator checks, so a contract cannot drift
    from the thing it describes.

    Args:
        implementation: The registered step.
        tools: The tool names an agentic step may call. Empty for a plain function step.

    Returns:
        The contract, described by the first paragraph of the step function's docstring.
    """
    inputs = {name: TypeAdapter(annotation).json_schema() for name, annotation in implementation.input_types.items()}
    return TaskContract(
        name=implementation.name,
        description=(implementation.function.__doc__ or "").strip().split("\n\n")[0],
        inputs=inputs,
        output=TypeAdapter(implementation.output_type).json_schema(),
        required=tuple(sorted(implementation.required)),
        tools=tuple(tools),
    )


@dataclass
class AgentTask:
    """An agentic step: instructions, its own tools, a typed output.

    The caller supplies `runner`. This package imports no agent framework.
    """

    name: str
    instructions: str
    runner: Callable[..., Awaitable[object]]
    tool_names: tuple[str, ...] = field(default_factory=tuple)
    """Tools the task may call, recorded so a contract can advertise them."""

    def __post_init__(self) -> None:
        if not self.instructions.strip():
            msg = f"agent task {self.name!r} has no instructions. An unbriefed step is not a contract"
            raise ValueError(msg)

    def step(self, *, inputs: Mapping[str, type], output: type) -> Callable[..., Awaitable[object]]:
        """This task as a registrable async callable, annotated so the registry can read it.

        The callable carries a real `__signature__` and `__annotations__`, so `register` treats it
        exactly like a hand-written function.

        Args:
            inputs: Each keyword input's name and type.
            output: The type the task returns.

        Returns:
            An async callable that awaits `runner` with the keyword arguments it is given.
        """
        types = dict(inputs)

        async def run(**kwargs: object) -> object:
            return await self.runner(**kwargs)

        run.__name__ = self.name
        run.__qualname__ = self.name
        run.__doc__ = self.instructions.strip().split("\n\n")[0]
        run.__annotations__ = {**types, "return": output}
        run.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
            [inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation) for name, annotation in types.items()],
            return_annotation=output,
        )
        return run
