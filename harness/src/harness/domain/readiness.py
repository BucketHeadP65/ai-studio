"""Whether a workflow is described well enough to keep, and whether this run could execute it.

Two independent readings of one workflow. **Described** is about the words a person meets it by:
a title, a summary, and the author's four sentences (what it is for, what a run produces, what
the person does during one, what to reach for something else for). **Executable** is about this
run's steps: whether every name the graph uses resolves here and its wiring checks out.

The rules are literal and mechanical. Lengths are counted after `strip()`, a placeholder is one of
a fixed set of five words after `placeholder_key` normalisation, and no English is read for
meaning. An error refuses a write. A warning is advice and never decides `described`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from core.ai.workflows.contracts import (
    ArtifactActionSlot,
    InputPresentation,
    ResultFieldPresentation,
    WorkflowPresentation,
    referenced_steps,
)
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from core.ai.workflows.contracts import Prepared, Workflow

PLACEHOLDERS = frozenset({"tbd", "todo", "n/a", "(not stated)", "coming soon"})
"""The whole placeholder rule. A field normalising to one of these is unwritten, not written."""

TITLE_LIMIT = 80
"""Longest a title may be, counted after stripping."""

SUMMARY_LIMIT = 280
"""Longest a summary may be, counted after stripping."""

PROSE_LIMIT = 2000
"""Longest each of the author's four sentences may be, counted after stripping."""

ReadinessField = Literal["title", "summary", "purpose", "produces", "owner_participation", "not_for", "work"]
"""Every field a finding can land on, so a form can put one beside the box it belongs to."""

_PROSE: tuple[tuple[ReadinessField, str, Callable[[WorkflowPresentation], str]], ...] = (
    ("purpose", "what this workflow is for", lambda words: words.purpose),
    ("produces", "what a finished run leaves the person holding", lambda words: words.produces),
    ("owner_participation", "what the person does during a run", lambda words: words.owner_participation),
    ("not_for", "what to reach for something else for", lambda words: words.not_for),
)
"""The four fields a reader needs: the field, what it must say, and how to read it off the words."""


def placeholder_key(value: str) -> str:
    """One string as a placeholder comparison sees it: trimmed, inner runs of space collapsed, casefolded.

    Args:
        value: The string to compare.

    Returns:
        The string in the form `PLACEHOLDERS` is written in.
    """
    return " ".join(value.split()).casefold()


class ReadinessFinding(BaseModel):
    """One thing to fix, on the field it belongs to, in words the person can act on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: ReadinessField
    severity: Literal["error", "warning"]
    message: str


class DraftReadiness(BaseModel):
    """Both readings of one workflow, plus everything either of them found.

    `described` is false whenever a finding of severity `error` is present, and only then: a
    warning is advice about a workflow that may still be saved.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    executable: bool
    described: bool
    findings: tuple[ReadinessFinding, ...]

    def errors(self) -> tuple[ReadinessFinding, ...]:
        """The findings that refuse a write.

        Returns:
            Every finding of severity `error`, in order.
        """
        return tuple(finding for finding in self.findings if finding.severity == "error")


def _line(field: ReadinessField, value: str, *, limit: int, missing: str, noun: str) -> Iterator[ReadinessFinding]:
    """The rules a one-line card field is held to (present, short enough, not a placeholder), as one finding at most."""
    text = value.strip()
    if not text:
        yield ReadinessFinding(field=field, severity="error", message=missing)
    elif len(text) > limit:
        yield ReadinessFinding(field=field, severity="error", message=f"A {noun} is {limit} characters at most. This one is {len(text)}.")
    elif placeholder_key(text) in PLACEHOLDERS:
        yield ReadinessFinding(field=field, severity="error", message=f"This {noun} is a placeholder. Write the real one.")


def _prose(field: ReadinessField, value: str, says: str) -> Iterator[ReadinessFinding]:
    """The rules each of the author's four sentences is held to. One finding at most, as above."""
    text = value.strip()
    if not text:
        yield ReadinessFinding(field=field, severity="error", message=f"Say {says}.")
    elif len(text) > PROSE_LIMIT:
        yield ReadinessFinding(
            field=field, severity="error", message=f"This is {PROSE_LIMIT} characters at most. This one is {len(text)}."
        )
    elif placeholder_key(text) in PLACEHOLDERS:
        yield ReadinessFinding(field=field, severity="error", message=f"This is a placeholder. Say {says}.")


def card_readiness(
    workflow: Workflow, prepared: Prepared, *, title: str, summary: str, presentation: WorkflowPresentation | None
) -> DraftReadiness:
    """Read one workflow's card and its steps, and report both answers with everything found.

    `presentation` absent is read as one with every field empty, so a workflow nobody has
    described yet reports the four sentences it still owes rather than passing silently.

    Args:
        workflow: The workflow being described.
        prepared: The workflow already built against this run's steps.
        title: The card's title.
        summary: The card's one line.
        presentation: The author's own words, or None when nothing has written any.

    Returns:
        Both readings, with every finding.
    """
    words = presentation if presentation is not None else WorkflowPresentation()
    findings = [
        *_line("title", title, limit=TITLE_LIMIT, missing="A title says what this workflow is called. Write one.", noun="title"),
        *_line("summary", summary, limit=SUMMARY_LIMIT, missing="A summary is the one line a reader chooses by. Write one.", noun="summary"),
    ]
    for field, says, read in _PROSE:
        findings.extend(_prose(field, read(words), says))
    if summary.strip() and placeholder_key(summary) == placeholder_key(workflow.description):
        findings.append(
            ReadinessFinding(
                field="summary",
                severity="warning",
                message="This summary repeats the description in the workflow. A reader learns nothing from reading both.",
            )
        )
    missing = [name for name in referenced_steps(workflow) if name not in prepared.registry]
    if missing:
        findings.append(
            ReadinessFinding(
                field="work",
                severity="warning",
                message=f"Nothing here can build {', '.join(missing)}, so this run cannot execute it.",
            )
        )
    return DraftReadiness(
        executable=prepared.report.ok and not missing,
        described=not any(finding.severity == "error" for finding in findings),
        findings=tuple(findings),
    )


class WorkflowPresentationPatch(BaseModel):
    """A change to some of the author's words, as a tool call or a browser sends one.

    Every field is optional and **absent means unchanged**: a turn that writes the purpose must
    not blank the other three. `None` is how absence is spelled on the wire, so an empty string
    stays a real value the readiness gate can refuse.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    purpose: str | None = None
    produces: str | None = None
    owner_participation: str | None = None
    not_for: str | None = None
    inputs: tuple[InputPresentation, ...] | None = None
    result_fields: tuple[ResultFieldPresentation, ...] | None = None
    actions: tuple[ArtifactActionSlot, ...] | None = None

    def prose_fields(self) -> frozenset[ReadinessField]:
        """Which of the four sentences this patch carries.

        Returns:
            The fields of the sentences it carries, empty when it carries none of them.
        """
        named: set[ReadinessField] = set()
        if self.purpose is not None:
            named.add("purpose")
        if self.produces is not None:
            named.add("produces")
        if self.owner_participation is not None:
            named.add("owner_participation")
        if self.not_for is not None:
            named.add("not_for")
        return frozenset(named)

    def changes_anything(self) -> bool:
        """Whether this patch carries a single field at all.

        Returns:
            True when at least one field is not `None`.
        """
        return any(
            value is not None
            for value in (self.purpose, self.produces, self.owner_participation, self.not_for, self.inputs, self.result_fields, self.actions)
        )


def effective_presentation(existing: WorkflowPresentation | None, patch: WorkflowPresentationPatch) -> WorkflowPresentation:
    """What the author's words become once `patch` is applied to `existing`.

    Starts from `existing`, or from a presentation with every field empty when there is none, and
    replaces only the fields the patch carries. The result is validated like any presentation.

    Args:
        existing: The author's words today, or `None` when there are none.
        patch: The fields to replace.

    Returns:
        The presentation with the patch applied.

    Raises:
        ValidationError: the result labels one parameter twice, names one action twice, or records
            a review of nothing.
    """
    base = existing if existing is not None else WorkflowPresentation()
    return WorkflowPresentation(
        purpose=base.purpose if patch.purpose is None else patch.purpose,
        produces=base.produces if patch.produces is None else patch.produces,
        owner_participation=base.owner_participation if patch.owner_participation is None else patch.owner_participation,
        not_for=base.not_for if patch.not_for is None else patch.not_for,
        inputs=base.inputs if patch.inputs is None else patch.inputs,
        result_fields=base.result_fields if patch.result_fields is None else patch.result_fields,
        actions=base.actions if patch.actions is None else patch.actions,
    )


def refusal_text(readiness: DraftReadiness) -> str:
    """Every error in one answer, one line each, named by the field it lands on.

    Args:
        readiness: The readings whose errors refuse a write.

    Returns:
        A header line, then one indented line per error.
    """
    return "\n".join(
        ["This workflow is not described yet.", *(f"  {finding.field}: {finding.message}" for finding in readiness.errors())]
    )
