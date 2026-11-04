"""Reading a library's reusable steps by id and copying them into a workflow's `inline`, for a fetch, a check and a save alike."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.exceptions import LibraryError, ReusableStepsRefusedError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from core.ai.workflows.domain.vocabulary import InlineStep, Workflow

SEARCH_STEPS_TOOL = "search_steps"
"""The tool name under which a model finds a reusable step by what it does."""

NO_REUSABLE_STEPS = "this library keeps no reusable steps. Drop `uses` and define the steps under inline"
"""The refusal for `uses` against a library that keeps no reusable steps, in the words the model reads."""


async def fetch_reusable_step(step_id: str, fetch_step: Callable[[str], Awaitable[InlineStep]]) -> InlineStep:
    """One reusable step read by its id, refused in the words the model reads when the id yields no valid step.

    Args:
        step_id: The reusable step's id.
        fetch_step: Reads one reusable step by its id. A `LibraryError`, `ValueError`, `KeyError` or `TypeError` it
            raises means that id yields no valid step. Any other error passes through.

    Returns:
        The step.

    Raises:
        ReusableStepsRefusedError: The id yields no valid step. The message names the id, why, and how to find a step.
    """
    try:
        return await fetch_step(step_id)
    except (LibraryError, ValueError, KeyError, TypeError) as exc:
        msg = (
            f"{step_id!r} did not yield a valid reusable step ({exc}). "
            f"Call {SEARCH_STEPS_TOOL} with what the step should do, and use an id it returns."
        )
        raise ReusableStepsRefusedError(msg) from exc


async def copy_reusable_steps(workflow: Workflow, uses: Sequence[str], fetch_step: Callable[[str], Awaitable[InlineStep]]) -> Workflow:
    """`workflow` with every reusable step `uses` names fetched and appended to its `inline`, in order.

    It refuses an id named twice, an id that yields no valid step, and a step whose name `inline` already carries.
    Nothing is copied when it refuses.

    Args:
        workflow: The workflow the steps are copied into.
        uses: Reusable step ids.
        fetch_step: Reads one reusable step by its id. A `LibraryError`, `ValueError`, `KeyError` or `TypeError` it
            raises means that id yields no valid step. Any other error passes through.

    Returns:
        The workflow, its `inline` followed by the copied steps.

    Raises:
        ReusableStepsRefusedError: An id is named twice, an id yields no valid step, or a copied step's name is already
            under `inline`.
    """
    carried = {step.name for step in workflow.inline}
    copied: list[InlineStep] = []
    seen: set[str] = set()
    for step_id in uses:
        if step_id in seen:
            msg = f"{step_id!r} is named more than once in `uses`. Name each reusable step once."
            raise ReusableStepsRefusedError(msg)
        seen.add(step_id)
        step = await fetch_reusable_step(step_id, fetch_step)
        if step.name in carried:
            msg = f"{step_id} is step {step.name!r}, which this workflow already carries under inline. Drop one of the two"
            raise ReusableStepsRefusedError(msg)
        carried.add(step.name)
        copied.append(step)
    return workflow.model_copy(update={"inline": (*workflow.inline, *copied)})
