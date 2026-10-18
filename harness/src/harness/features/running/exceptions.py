"""The errors the running slice raises when a run cannot start or a gate cannot be decided."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.ai.workflows.contracts import WiringIssue


class AutomodeUnavailableError(Exception):
    """Why an automode launch was refused, in words the person can act on."""


class RunEngineUnavailableError(Exception):
    """Why a launch was refused: the configured engine answered nothing when this process composed itself."""


class OrchestratorUnavailableError(RuntimeError):
    """The session an orchestrator gate needed stayed busy past this run's own gate timeout."""


class WorkflowNotRunnableError(Exception):
    """A workflow does not validate against this process's environment.

    The message is `this app cannot run it. ` followed by every problem, joined by a period and a space.
    `issues` holds the problems the validator reported.
    """

    def __init__(self, workflow_id: str, *, issues: Sequence[WiringIssue] = (), problems: Sequence[str] = ()) -> None:
        """Name the workflow and every problem found in it.

        Args:
            workflow_id: The workflow that was checked, or `draft` for a draft.
            issues: The problems the validator reported. Each is named as `where: detail`.
            problems: Problems already written out, such as a draft's verdict. They follow `issues`.
        """
        detail = ". ".join([*(f"{issue.where}: {issue.detail}" for issue in issues), *problems])
        super().__init__(f"this app cannot run it. {detail}")
        self.workflow_id = workflow_id
        self.issues = tuple(issues)


class RunStateRefusedError(Exception):
    """An act on a run was refused because of the state the run or the engine is in, in words the person can act on."""


class RunGraphRefusedError(Exception):
    """A resume was refused because no graph could be found to resume with, in words the person can act on."""
