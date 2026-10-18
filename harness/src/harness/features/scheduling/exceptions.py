"""The errors the scheduler raises when it refuses a schedule."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


class ScheduleNotFoundError(KeyError):
    """No schedule has the given id."""


class WorkflowUnavailableError(Exception):
    """The library cannot serve the workflow a schedule names.

    `reason` holds the library's own words.
    """

    def __init__(self, workflow_id: str, *, reason: str) -> None:
        """Name the workflow and why the library could not serve it.

        Args:
            workflow_id: The workflow the schedule names.
            reason: The library's own words.
        """
        super().__init__(reason)
        self.workflow_id = workflow_id
        self.reason = reason


class WorkflowParametersMissingError(Exception):
    """The arguments leave a required parameter of the schedule's workflow without a value.

    `missing` names each such parameter. The message lists them.
    """

    def __init__(self, workflow_id: str, *, missing: Sequence[str]) -> None:
        """Name the workflow and the parameters still without a value.

        Args:
            workflow_id: The workflow the schedule names.
            missing: Every required parameter the arguments leave unfilled.
        """
        super().__init__(f"fill in: {', '.join(missing)}")
        self.workflow_id = workflow_id
        self.missing = tuple(missing)


class ScheduleSettingInvalidError(ValueError):
    """A cadence, gate policy, blackout or gate mode is not one of its own shapes.

    The message says what the setting should look like.
    """
