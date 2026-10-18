"""The errors the workflow engine raises, in one module every other module of the engine may import."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.ai.workflows.authoring import Problem
    from core.ai.workflows.domain.run_events import RetryFeedback
    from core.ai.workflows.domain.validator import ValidationReport


class RegistrationError(ValueError):
    """The callable cannot serve as a step: not async, missing annotations, or a duplicate name."""


class RepeatedFailureError(RuntimeError):
    """A map aborted because its last few items failed with the same error class in a row.

    It ends the run whatever the map's `on_error` says.
    """

    def __init__(self, node_id: str, failure: str, count: int) -> None:
        """Name the map, the failure and how many items it took.

        Args:
            node_id: The map node's id.
            failure: The qualified name of the error class the items failed with.
            count: How many consecutive items failed with it.
        """
        self.node_id = node_id
        self.failure = failure
        self.count = count
        super().__init__(
            f"map {node_id!r} aborted: {count} consecutive items failed with {failure}, this is a broken system, not bad items"
        )


class UnrecordedTurnError(RuntimeError):
    """A `FromNode.turn` wire named a map turn whose collected list was never recorded.

    A validated workflow never raises it. It names the map and the turn where a bare `KeyError` would not.
    """

    def __init__(self, node_id: str, turn: str) -> None:
        """Name the map and the turn the wire asked for.

        Args:
            node_id: The map node's id.
            turn: The step name of the turn.
        """
        self.node_id = node_id
        self.turn = turn
        super().__init__(f"map {node_id!r} turn {turn!r}: no value was recorded for it")


class WorkflowInvalidError(ValueError):
    """The workflow failed validation, and the report says exactly where."""

    def __init__(self, report: ValidationReport) -> None:
        """Carry the report and list its issues in the message.

        Args:
            report: The validation report, with at least one issue.
        """
        self.report = report
        lines = [f"  [{i.kind}] {i.where}: {i.detail}" for i in report.issues]
        super().__init__("spec failed validation:\n" + "\n".join(lines))


class RetryExhaustedError(RuntimeError):
    """A gate refused its last permitted reach: the repair is owed and the run has no reach left.

    Carries the whole rejection, reviewed value included, so an ending can say what was asked for
    and never got done.
    """

    def __init__(self, gate_id: str, target_node: str, attempts: int, feedback: RetryFeedback) -> None:
        """Carry the gate, the node it would rewind to, the reach count and the rejection.

        Args:
            gate_id: The gate's node id.
            target_node: The node the rejection would have rerun, the gate's `rejected_to`.
            attempts: The reach that spent the ceiling, which is the gate's `max_attempts`.
            feedback: The rejection, reviewed value and requested repair included.
        """
        self.gate_id = gate_id
        self.target_node = target_node
        self.attempts = attempts
        self.feedback = feedback
        super().__init__(f"gate {gate_id!r} refused {target_node!r} on reach {attempts} of {attempts}: {feedback.requested_repair}")


class TaskShapeError(ValueError):
    """A defined step could not be built. Carries one problem per reason, never just the first."""

    def __init__(self, problems: Sequence[Problem]) -> None:
        """Carry the problems and join them into the message.

        Args:
            problems: Every reason the step could not be built, each with where it sits.
        """
        self.problems = tuple(problems)
        super().__init__(". ".join(f"{where}: {detail}" for where, detail in self.problems))


class WiringRefusedError(ValueError):
    """A `WorkflowBuilder` call would have produced a workflow with broken wiring.

    Carries one problem per reason, never just the first.
    """

    def __init__(self, problems: Sequence[Problem]) -> None:
        """Carry the problems and join them into the message.

        Args:
            problems: Every wiring problem, each with where it sits.
        """
        self.problems = tuple(problems)
        super().__init__(". ".join(f"{where}: {detail}" for where, detail in self.problems))


class LibraryError(RuntimeError):
    """The library cannot serve what was asked of it, and the caller should not guess why.

    A caller catches this one error for every library backend.
    """


class WorkflowUnknownError(LibraryError, KeyError):
    """The library holds no workflow under that id.

    It is both a `LibraryError` and a `KeyError`, so a caller may catch either. Its text is the
    message alone, without the quotes a `KeyError` adds.
    """

    def __str__(self) -> str:
        """The message, unquoted."""
        return str(self.args[0]) if self.args else ""


class SaveRefusedError(RuntimeError):
    """The workflow was not saved, and the report says exactly why."""

    def __init__(self, report: ValidationReport, missing_parameters: tuple[str, ...]) -> None:
        """Carry the report and the missing parameters, and list both in the message.

        Args:
            report: The validation report, which may have no issue.
            missing_parameters: The required parameters nothing supplies.
        """
        self.report = report
        self.missing_parameters = missing_parameters
        lines = [f"  [{i.kind}] {i.where}: {i.detail}" for i in report.issues]
        if missing_parameters:
            lines.append(f"  parameters nothing supplies: {', '.join(missing_parameters)}")
        super().__init__("refused to save an unexecutable workflow:\n" + "\n".join(lines))


class PresentationRefusedError(RuntimeError):
    """The presentation names something the workflow does not declare, so nothing was written."""


class AdaptationRefusedError(ValueError):
    """The edit was not applied, and the message says which op and why."""


class DocumentError(ValueError):
    """The text is not a readable workflow file or step file.

    Either file fails when its frontmatter is missing, unclosed, not YAML or not a mapping, or when nothing
    sits under the requested key. A workflow file also fails on coerced keys. A step file also fails when
    `title`, `summary` or `version` is missing or of the wrong type.
    """


class ParameterSchemaError(ValueError):
    """Why a parameter's declared schema cannot admit anything: it is not a schema, or it reaches out."""


class ArgumentRefusedError(ValueError):
    """One supplied argument the workflow's own parameter schema will not admit."""

    def __init__(self, parameter: str, path: str, detail: str) -> None:
        """Name the parameter, where in its value the refusal sits and what the check said.

        Args:
            parameter: The declared parameter the value was supplied for.
            path: The JSON path inside the value, from the value's own root.
            detail: The check's own words.
        """
        self.parameter = parameter
        """Which declared parameter the value was supplied for."""
        self.path = path
        """Where inside that value the refusal sits, as a JSON path from the value's own root."""
        self.detail = detail
        """What the check said, in the validator's own words."""
        super().__init__(f"{parameter} {path}: {detail}")
