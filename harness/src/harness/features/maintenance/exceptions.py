"""The errors the maintenance slice raises to a caller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from harness.features.maintenance.domain import change_line

if TYPE_CHECKING:
    from harness.features.maintenance.domain import LandingReport


class LandingRefusedError(RuntimeError):
    """A landing that refused one or more planned changes, after landing every other one.

    `report` holds every change's outcome, the refused ones with why. The message says every change in one line each: the
    new version of each adoption now on live trial, the step, each flagged or refused pair's reason and any note.
    """

    def __init__(self, report: LandingReport) -> None:
        """Hold the landing's report and say what it refused and what it landed.

        Args:
            report: The landing's report, holding at least one refused change.
        """
        refused = sum(change.outcome == "refused" for change in report.changes)
        rest = " and landed the rest" if refused < len(report.changes) else ""
        lines = "\n".join(change_line(change) for change in report.changes)
        super().__init__(f"step maintenance refused {refused} of {len(report.changes)} planned changes{rest}:\n{lines}")
        self.report = report
