"""The errors the maintenance slice raises to a caller."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.features.maintenance.domain import LandingReport


class LandingRefusedError(RuntimeError):
    """A landing that refused one or more planned changes, after landing every other one.

    `report` holds every change's outcome, the refused ones with why. The message names each refused consumer with its
    reason and each change that landed.
    """

    def __init__(self, report: LandingReport) -> None:
        """Hold the landing's report and say what it refused and what it landed.

        Args:
            report: The landing's report, holding at least one refused change.
        """
        refused = [change for change in report.changes if change.outcome == "refused"]
        landed = [change for change in report.changes if change.outcome != "refused"]
        said = [f"{change.workflow_id} with {change.step_id} was refused: {change.reason.rstrip('. ')}." for change in refused]
        message = f"step maintenance refused {len(refused)} of {len(report.changes)} planned changes. " + " ".join(said)
        if landed:
            message += " It landed " + ", ".join(f"{change.workflow_id} ({change.outcome})" for change in landed) + "."
        super().__init__(message)
        self.report = report
