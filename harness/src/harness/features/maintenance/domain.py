"""The names the step maintenance workflow and its watches agree on, and the values its registered steps hand along its graph.

Each value is a pydantic model, so a workflow file wires a briefing to a registered step by naming the step that
produces the model (`like: prepare_plan`). Text a model wrote reaches a maintenance flag page only through `fenced`.
"""

from __future__ import annotations

import re

from typing import Literal

from core.ai.workflows.contracts import TrustLevel, WorkflowIntent, WorkflowPresentation
from core.markdown import code_block
from pydantic import BaseModel, ConfigDict, JsonValue

from harness.domain.contracts import CaseReport

MAINTENANCE_WORKFLOW = "step-maintenance"
"""The name of the workflow that checks a new reusable step version against each consumer."""

PLAN_GATE = "judge_plan"
"""The id of the step maintenance workflow's judge gate over its plan. A run that spends its last attempt lands nothing."""

CHANGED_STEPS = "changed_step_ids"
"""The step maintenance workflow's parameter: the changed step versions' ids, comma-separated."""

PAIR_CAP = 20
"""The most consumer pairs one step maintenance run takes. The rest wait for the next run."""

ADOPTION_EDITS = frozenset({"rewire", "add_parameter", "set_output"})
"""The edit operations an adoption may carry beside its replaced step. Every other one is refused at landing, so a gate,
a charge or a node a model wrote never lands in a consumer."""

NOT_YET_DECIDED = "not yet decided"
"""The reason `prepare_plan` gives every planned change, until the planner writes its own."""

TrustName = Literal["unverified", "machine-confirmed", "approved"]
"""A trust level by name."""

_PERCENT_BEFORE_PERCENT = re.compile(r"%(?=%)")


class ConsumerPair(BaseModel):
    """One consumer and the changed step version it holds an older or diverged copy of, with what the planner decides by."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str
    """The consumer's workflow version id, `wf.<name>.v<version>`."""
    workflow: str
    """The consumer's workflow name."""
    title: str
    """The consumer's card title."""
    step: str
    """The reusable step's name."""
    step_id: str
    """The changed step version's id, `st.<name>.v<version>`."""
    step_hash: str
    """The changed step version's content address."""
    state: Literal["behind", "diverged"]
    """`behind` when the copy matches an older saved version, `diverged` when it matches none."""
    held_step_id: str
    """The saved version the copy matches, or for a diverged copy the version it diverged from."""
    nodes: list[str]
    """The consumer's nodes that run the step."""
    diff: str
    """The unified diff of the step's canonical YAML from the consumer's copy to the changed version."""
    presentation: WorkflowPresentation | None
    """The consumer author's words about what it is for, when the library keeps any."""
    intent: WorkflowIntent | None
    """What the person who wanted the consumer asked for, when the library keeps it."""
    trust_level: TrustName
    """The trust level the consumer version earned."""
    trust: TrustLevel
    """The consumer version's own trust record."""
    total_trust: TrustLevel
    """The trust record over every saved version of the consumer's name."""


class ConsumerList(BaseModel):
    """The consumer pairs one step maintenance run takes, the consumers it leaves for the next run, and how many it dropped."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pairs: list[ConsumerPair]
    """At most `PAIR_CAP` behind or diverged pairs, by step name and then by consumer name."""
    left_for_next_run: list[str]
    """The consumer workflow ids past the cap, which the next run takes."""
    dropped: int
    """How many pairs were dropped because the copy is current or the consumer declined the changed version."""


class ContractCheck(BaseModel):
    """Whether one consumer still validates with its copy replaced by the changed step version, and what fails when not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str
    """The consumer's workflow version id."""
    step_id: str
    """The changed step version's id."""
    passed: bool
    issues: list[str]
    """Each validation issue as `[<kind>] <where>: <detail>`, or why the copy could not be replaced. Empty when it passed."""


class PlannedChange(BaseModel):
    """What one step maintenance run does with one consumer pair: adopt the changed version, keep the copy, or flag it.

    The workflow and the step are named by id, and the landing copies the step from the library, so a planner never
    writes a step. `check` and `cases` are the evidence `prepare_plan` copied for the pair. Answer them as they were given.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str
    """The consumer's workflow version id."""
    step_id: str
    """The changed step version's id."""
    disposition: Literal["adopt", "keep", "flag"]
    """`adopt` saves the consumer's next version with the changed step, `keep` declines it, `flag` asks the owner."""
    reason: str
    """Why, in one or two sentences the owner reads."""
    extra_ops: tuple[dict[str, JsonValue], ...] = ()
    """Edit operations applied after the step is replaced, such as a `rewire` or an `add_parameter`, for an adoption the
    contract check fails without."""
    check: ContractCheck
    """The pair's contract check."""
    cases: list[CaseReport]
    """The case reports of the changed version and of the version the consumer holds."""


class MaintenancePlan(BaseModel):
    """Every planned change of one step maintenance run, and the consumers it leaves for the next run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    changes: list[PlannedChange]
    left_for_next_run: list[str]
    """The consumer workflow ids past the pair cap, as `list_consumers` named them."""


class LandedChange(BaseModel):
    """What the landing did with one planned change."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str
    """The consumer's workflow version id the plan named."""
    step_id: str
    """The changed step version's id."""
    disposition: Literal["adopt", "keep", "flag"]
    outcome: Literal["adopted", "kept", "flagged", "refused"]
    """`refused` when the library or the contract check refused the change, which then landed nothing."""
    reason: str
    """The planner's reason, or for a refused change why it was refused."""
    saved_id: str
    """The id of the consumer version an adoption saved, empty for every other outcome."""
    notes: list[str]
    """What did not happen beside the outcome, such as a flag page that was not written."""


class LandingReport(BaseModel):
    """What one landing did with every planned change, and the consumers it leaves for the next run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    changes: list[LandedChange]
    left_for_next_run: list[str]


class ConsumerSide(BaseModel):
    """One consumer on one side of a repository change: what the landing did with it and what the consumer is for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str
    """The consumer's workflow version id the plan named."""
    outcome: Literal["adopted", "kept", "flagged"]
    reason: str
    """The planner's reason."""
    nodes: list[str]
    """The consumer's nodes that run the step."""
    diff: str
    """The unified diff of the step's canonical YAML from the consumer's copy to the changed version."""
    presentation: WorkflowPresentation | None
    intent: WorkflowIntent | None


class DraftedFile(BaseModel):
    """One file of a repository change, as its whole new text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    """The file's path from the repository root."""
    text: str
    """The file's whole new text."""


class RepositoryChange(BaseModel):
    """A drafted change to a reusable step's files, for a step some consumers adopted and others kept or flagged.

    `adopted` and `kept` are the evidence `select_disagreements` filled. Answer them as they were given. The drafted
    fields are empty until a drafter writes a new version of the step with a parameter the consumers set differently,
    or a split into two named steps.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: str
    """The reusable step's name."""
    step_id: str
    """The changed step version's id."""
    adopted: list[ConsumerSide]
    """The consumers that adopted the changed version."""
    kept: list[ConsumerSide]
    """The consumers that kept their copy or were flagged."""
    proposal: str = ""
    """What the drafted change does and why every consumer's goal holds under it."""
    step_files: tuple[DraftedFile, ...] = ()
    """The new or changed step files."""
    file_edits: tuple[DraftedFile, ...] = ()
    """The changed workflow files of the consumers the repository holds."""


class RepositoryVerdict(BaseModel):
    """A judge's verdict on one drafted repository change, named by its step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: str
    """The reusable step the drafted change is about."""
    decision: Literal["keep", "drop", "undecided"]
    """`keep` hands the draft over, `drop` and `undecided` leave it out."""
    reason: str
    """Why, in one or two sentences."""


class DroppedDraft(BaseModel):
    """A repository change that was not handed over, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: str
    reason: str


class HandOver(BaseModel):
    """What the hand-over wrote, the flag page of each kept draft, the drafts it dropped and what failed, beside the report.

    It is the step maintenance run's answer, so it carries the owner's report of the landing too.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    report: str
    """The owner's report of the landing, as `report_changes` answered it."""
    flags: list[str]
    """The id of each `repository_change` flag page written, `mf.change.<step>`."""
    dropped: list[DroppedDraft]
    problems: list[str]
    """Each flag page that was not written and a notice that did not go out, with why. The landing stands either way."""


def changed_ids(changed_step_ids: str) -> tuple[str, ...]:
    """The step ids `changed_step_ids` names, comma-separated, each once, in order.

    Args:
        changed_step_ids: The ids, comma-separated, with any spaces around each.

    Returns:
        The ids, blanks left out.
    """
    return tuple(dict.fromkeys(part.strip() for part in changed_step_ids.split(",") if part.strip()))


def fenced(text: str) -> str:
    """`text` as a fenced block a maintenance flag page keeps whole, for text a model wrote or a diff.

    The fence is longer than any run of backticks in the text, so no heading or fence inside it counts. Each `%%` is
    written `% %`, because the page drops everything between two `%%`, fenced text included.

    Args:
        text: The text to quote.

    Returns:
        The fenced block.
    """
    return code_block(_PERCENT_BEFORE_PERCENT.sub("% ", text), info="text")


def one_line(text: str) -> str:
    """`text` on one line a maintenance flag page keeps whole, for a short name a model wrote, such as a file path.

    Each run of whitespace becomes one space, backticks are dropped, and each `%%` is written `% %`.

    Args:
        text: The text to put on one line.

    Returns:
        The line.
    """
    return _PERCENT_BEFORE_PERCENT.sub("% ", " ".join(text.replace("`", "").split()))


def change_line(change: LandedChange) -> str:
    """One landed change as the owner reads it: the consumer, the step version, what happened, the reason and any note.

    An adoption names the new version now on live trial.

    Args:
        change: The landed change.

    Returns:
        The line.
    """
    match change.outcome:
        case "adopted":
            said = f"{change.saved_id} adopts {change.step_id} on live trial in place of {change.workflow_id}. {change.reason}"
        case "kept":
            said = f"{change.workflow_id} keeps its copy instead of {change.step_id}. {change.reason}"
        case "flagged":
            said = f"{change.workflow_id} waits for your decision on {change.step_id}. {change.reason}"
        case "refused":
            said = f"{change.workflow_id} was refused {change.step_id}: {change.reason}"
    notes = f" ({', '.join(change.notes)})" if change.notes else ""
    return f"{said}{notes}"
