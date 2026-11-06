"""Which step version a step maintenance run takes for a changed step id: the newest saved version of the step's name."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import LibraryError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from core.ai.workflows.contracts import InlineStep, StepVersion


async def newest_version(
    step_id: str,
    *,
    fetch_step: Callable[[str], Awaitable[InlineStep]],
    step_versions: Callable[[str], Awaitable[tuple[StepVersion, ...]]],
) -> tuple[StepVersion, tuple[StepVersion, ...]]:
    """The newest saved version of the step `step_id` names, whichever version the id names, beside every saved version.

    A run plans and flags the newest version, so an id naming an older one still reaches the version the run works on.

    Args:
        step_id: A changed step version's id, `st.<name>.v<version>`.
        fetch_step: Reads one reusable step by its id.
        step_versions: Reads every saved version of one step name.

    Returns:
        The newest version and every saved version of its name.

    Raises:
        LibraryError: `step_id` names no saved version of a reusable step, or a library read fails.
    """
    name = (await fetch_step(step_id)).name
    saved = await step_versions(name)
    if not any(each.step_id == step_id for each in saved):
        msg = f"{step_id} is not a saved version of the reusable step {name}"
        raise LibraryError(msg)
    return max(saved, key=lambda each: each.version), saved
