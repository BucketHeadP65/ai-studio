"""What a person asked for, kept beside the workflow drafted to answer it.

An intent is the request in the requester's own words, plus what they expect back: the result a
finished run should leave them holding, one concrete example of a run that would count as a
success, what a run does with nothing to work on, what it does when it cannot finish, whether a
person starts each run or a schedule does, and whatever was assumed on their behalf.

It is a record, never a permission. Nothing here grants a capability, selects work, or enters the
workflow's identity: a workflow with an intent beside it and the same workflow without one are the
same workflow.

`intent_issues` counts what is still unwritten and nothing else. It reads no English for meaning,
so an empty answer says every field carries text, not that the text is any good.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints


class WorkflowIntent(BaseModel):
    """One automation as the person who wanted it stated it, in their words and nobody else's.

    Incomplete is a normal state: only `request` is required, and a draft may carry an intent that
    still owes every other answer. Nothing here fills a blank field in, and an assumption made on
    the requester's behalf belongs in `assumptions`, said out loud.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    """What was asked for. The one thing an intent cannot exist without."""
    expected_result: str = ""
    """What a finished run should leave the person holding."""
    success_example: str = ""
    """One concrete run that would count as having worked."""
    empty_input_behavior: str = ""
    """What a run does when there is nothing to work on."""
    failure_behavior: str = ""
    """What a run does when it cannot finish."""
    trigger: Literal["manual", "scheduled"] = "manual"
    """Whether a person starts each run, or a schedule does."""
    assumptions: tuple[str, ...] = ()
    """What was filled in without being asked, one entry each, stated rather than silently taken."""


def intent_issues(intent: WorkflowIntent) -> tuple[str, ...]:
    """Which of the four expected answers are still blank, named in the order they are asked for.

    It checks only that each answer carries text, never whether the text is right, and it never
    refuses a draft.

    Args:
        intent: The intent to read.

    Returns:
        The names of the blank fields, empty when all four carry text.
    """
    required = (
        ("expected_result", intent.expected_result),
        ("success_example", intent.success_example),
        ("empty_input_behavior", intent.empty_input_behavior),
        ("failure_behavior", intent.failure_behavior),
    )
    return tuple(name for name, value in required if not value.strip())
