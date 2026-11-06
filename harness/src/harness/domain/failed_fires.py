"""What became of the changed steps an on-step-change fire that ended failed was fired with."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

NotPutBack = Literal["other_failure", "put_back_once", "schedule_paused"]
"""Why a failed fire's changed steps were not put back: the run did not stop with a provider error, a failed fire put
them back once already, or the schedule is paused."""


class FailedFireSteps(BaseModel):
    """The changed steps of one failed on-step-change fire: those put back on its schedule and those a person runs by hand."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    put_back: tuple[str, ...]
    """The step ids back among the schedule's changed steps, which it passes on when it fires again after its quiet period."""
    by_hand: tuple[str, ...]
    """The step ids no fire passes on again, which a run of the workflow by hand with them as `changed_step_ids` plans."""
    why: NotPutBack | None
    """Why `by_hand` was not put back. None when `by_hand` is empty."""
