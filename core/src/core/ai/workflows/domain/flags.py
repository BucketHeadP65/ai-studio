"""A maintenance flag: one page in the library that asks the owner to decide something step maintenance could not.

A flag lives under a stable id, so the next writer finds its own page. Every writer reads that page first and follows
`flag_to_write`, so a flag a person resolved stays resolved for the same step version.
"""

from __future__ import annotations

import difflib
import re

from typing import TYPE_CHECKING, Literal

from markdown_it import MarkdownIt
from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator

from core.ai.workflows.domain.fingerprint import canonical_step_yaml

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from core.ai.workflows.domain.consumers import ConsumerEntry, StepVersion
    from core.ai.workflows.domain.vocabulary import Workflow

FLAG_ID_PREFIX = "mf."
"""The prefix of every maintenance flag's page id."""

FlagKind = Literal["diverged_copy", "consumer_flag", "live_trial_reverted", "plan_rejected", "repository_change"]
"""What a maintenance flag asks about: a diverged copy a save recorded, a consumer step maintenance flagged, a live
trial that reverted, a plan the judge rejected three times, or a change to a repository file."""

_SHORT_KINDS: Mapping[FlagKind, str] = {
    "diverged_copy": "diverged",
    "consumer_flag": "consumer",
    "live_trial_reverted": "reverted",
    "plan_rejected": "rejected",
    "repository_change": "change",
}

AUTOMATIC_RESOLVERS = frozenset({"library", "agent:step-maintenance"})
"""Who resolves a flag on their own when its condition clears: a save, and a step maintenance run.

Their resolution is no decision, so the next flag for the same step version opens the page again."""

_BACKTICK_RUN = re.compile(r"`+")

_SECTION_BREAKS = frozenset({"h2", "h3"})
"""The headings a flag page splits its sections at."""

_DOUBLE_PERCENT_PAIR = re.compile(r"%%.*?%%", re.DOTALL)
"""What the page drops from a section, fenced text included: everything from one `%%` to the next."""

_PERCENT_BEFORE_PERCENT = re.compile(r"%(?=%)")


class MaintenanceFlag(BaseModel):
    """One maintenance flag as its page holds it: what it is about, its evidence, who raised it and its decision.

    A flag about one consumer names the consumer's workflow version id in `workflow`. A flag about a step leaves it
    empty. A field the flag does not hold is refused, and so is a `raised_at` without its offset. Evidence or a proposed
    change is refused when the page would cut it or drop part of it: a heading of level 2 or 3 outside a fenced block,
    a code fence it leaves open, or two `%%` anywhere. Both are kept without the whitespace at their edges, as the page
    keeps them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: FlagKind
    step: str
    """The reusable step's name."""
    step_id: str
    """The step version the flag is about, `st.<name>.v<version>`."""
    step_hash: str
    """The content address of that step version."""
    workflow: str = ""
    """The consumer's workflow version id, `wf.<name>.v<version>`, empty for a flag about a step."""
    title: str
    """Names the kind, the workflow and the step."""
    summary: str
    """One line saying what to decide."""
    evidence: str
    """What the writer found, written as the page's Evidence section."""
    proposed_change: str = ""
    """The drafted step files and file edits of a repository change, written as the page's Proposed change section."""
    raised_by: str
    """Who wrote the flag: `library` for a save, `agent:step-maintenance` for a run or a live trial."""
    raised_at: AwareDatetime
    status: Literal["open", "resolved"] = "open"
    resolved_by: str = ""
    """Who resolved the flag, empty while it is open."""
    decision_reason: str = ""
    """Why it was resolved, empty while it is open."""

    @field_validator("evidence", "proposed_change")
    @classmethod
    def _one_section(cls, body: str) -> str:
        # A probe heading after the text is the only section break the page may find: a heading in the text adds one,
        # and a fence the text leaves open swallows the probe.
        tokens = MarkdownIt("commonmark").parse(f"{body}\n\n## end\n")
        if sum(token.type == "heading_open" and token.tag in _SECTION_BREAKS for token in tokens) != 1:
            msg = (
                "holds a level 2 or 3 heading outside a fenced block, or a code fence it leaves open, and the flag's page "
                "would cut the text there. A heading is a line starting with ## or ###, or a line of text with a `---` or "
                "`===` line under it, so a `---` line under text, such as the line that closes a drafted step file's "
                "frontmatter, is a heading too. Put such text inside a closed fenced block, or use a heading of level 4 or deeper"
            )
            raise ValueError(msg)
        if _DOUBLE_PERCENT_PAIR.search(body):
            msg = (
                "holds two %% marks, and the flag's page drops everything from one %% to the next, fenced text included. "
                "Write a single % in place of each %%, or put a space between the two % signs"
            )
            raise ValueError(msg)
        return body.strip()


def flag_id(kind: FlagKind, step: str, workflow_name: str = "") -> str:
    """The stable page id of a flag: `mf.<short kind>.<workflow name>.<step>`, or `mf.<short kind>.<step>` for a step.

    The short kinds are `diverged`, `consumer`, `reverted`, `rejected` and `change`.

    Args:
        kind: The flag's kind.
        step: The reusable step's name.
        workflow_name: The consumer's workflow name, empty for a flag about a step.

    Returns:
        The page id.
    """
    middle = f"{workflow_name}." if workflow_name else ""
    return f"{FLAG_ID_PREFIX}{_SHORT_KINDS[kind]}.{middle}{step}"


def flag_to_write(stored: MaintenanceFlag | None, new: MaintenanceFlag) -> MaintenanceFlag | None:
    """The flag a writer writes over the page under its id, or None to leave that page alone.

    No page gives the new flag open. An open page takes the new flag, unless it already holds the same flag apart from
    `raised_at`. A page a person resolved, whose `step_hash` matches the new flag's, is left alone, because the person
    already decided that step version. A resolved page whose `step_hash` differs opens again with the new flag, and so
    does a page one of `AUTOMATIC_RESOLVERS` resolved, whatever its `step_hash`. A writer never writes a resolution of its
    own: the flag it hands is written open.

    Args:
        stored: The flag the page holds now, or None when there is no page.
        new: The flag the writer raises.

    Returns:
        The flag to write, open, or None.
    """
    opened = new.model_copy(update={"status": "open", "resolved_by": "", "decision_reason": ""})
    if stored is None:
        return opened
    if stored.status == "resolved":
        decided = stored.resolved_by not in AUTOMATIC_RESOLVERS and stored.step_hash == opened.step_hash
        return None if decided else opened
    return None if stored.model_copy(update={"raised_at": opened.raised_at}) == opened else opened


def diverged_copy_flag(
    workflow: Workflow, workflow_id: str, entry: ConsumerEntry, version: StepVersion, *, raised_at: datetime
) -> MaintenanceFlag:
    """The `diverged_copy` flag a save raises for one diverged copy: about the version it diverged from, with the diff.

    Its evidence names that version and how it was found, then the unified diff of the canonical YAML of that version
    and of the copy, in a fenced block. The diff writes each `%%` as `% %`, and says so, because the page drops the text
    between two `%%`.

    Args:
        workflow: The saved workflow, which holds the copy.
        workflow_id: The saved workflow version's id.
        entry: The consumer record's entry for the diverged copy.
        version: The saved step version the copy diverged from.
        raised_at: When the save raised the flag.

    Returns:
        The flag, raised by `library`.

    Raises:
        ValueError: If the workflow holds no inline step named as the entry's step.
    """
    copy = next((step for step in workflow.inline if step.name == entry.step), None)
    if copy is None:
        msg = f"{workflow_id} holds no copy of {entry.step}, so it has no diverged copy to flag"
        raise ValueError(msg)
    basis = (
        "the version the earlier record of this workflow named" if entry.diverged_basis == "parent" else "the saved version closest to it by text"
    )
    diff = "\n".join(
        difflib.unified_diff(
            canonical_step_yaml(version.step).splitlines(),
            canonical_step_yaml(copy).splitlines(),
            fromfile=version.step_id,
            tofile=workflow_id,
            lineterm="",
        )
    )
    split = _PERCENT_BEFORE_PERCENT.sub("% ", diff)
    note = "" if split == diff else " The diff writes each double percent sign as % %, because the flag's page drops the text between two of them."
    diff = split
    fence = "`" * max([3, *(len(run) + 1 for run in _BACKTICK_RUN.findall(diff))])
    return MaintenanceFlag(
        kind="diverged_copy",
        step=entry.step,
        step_id=version.step_id,
        step_hash=version.step_hash,
        workflow=workflow_id,
        title=f"Diverged copy of {entry.step} in {workflow.name}",
        summary=(
            f"Decide whether the copy of {entry.step} in {workflow_id} stays as it is, becomes a new version of {entry.step}, "
            f"or goes back to a saved version."
        ),
        evidence=(
            f"{workflow_id} holds a copy of the reusable step {entry.step} that matches no saved version. It diverged from "
            f"{version.step_id}, {basis}.{note}\n\n{fence}diff\n{diff}\n{fence}"
        ),
        raised_by="library",
        raised_at=raised_at,
    )
