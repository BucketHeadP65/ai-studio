"""The live trial: the next real runs of a consumer version step maintenance adopted, each read once when it ends.

A run is ignored when it says nothing about the adopted step, which a run that failed anywhere but at the step never
does, failed when the step did not produce while everything it reads did, and worse when it did less well than the
version the adoption replaced. Any other run counts. The rule reads
only what the run and its grades recorded, and asks no model.
"""

from __future__ import annotations

import math

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.ai.workflows.domain.validator import wires_of
from core.ai.workflows.domain.vocabulary import FromNode

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from core.ai.workflows.domain.evidence import Grade, RunEvidence
    from core.ai.workflows.domain.outcome import OutcomeStatus
    from core.ai.workflows.domain.vocabulary import Workflow

STRUCTURAL_MARGIN = 0.1
"""How far below the baseline's mean a structural grade may fall before the run is worse."""

_RANKS: Mapping[OutcomeStatus, int] = {"succeeded": 3, "noop": 3, "partial": 2, "unknown": 1, "failed": 0}
"""How checked statuses rank: succeeded and noop above partial, partial above unknown, unknown above failed."""

_GATE_KINDS = frozenset({"gate", "judge", "orchestrator-gate"})


class LiveTrialMarker(BaseModel):
    """What a consumer version on live trial carries, under the frontmatter key `live_trial`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    previous: str
    """The consumer version the adoption replaced, `wf.<name>.v<version>`, whose runs are the baseline."""
    step: str
    """The reusable step's name."""
    step_hash: str
    """The content address of the step version adopted."""
    runs: int = Field(default=3, ge=1)
    """How many runs that count close the live trial."""


class LiveTrialVerdict(BaseModel):
    """How one run of a version on live trial reads, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["ignored", "failed", "worse", "counts"]
    """`failed` and `worse` revert the version. `counts` brings the live trial closer to its end. `ignored` does neither."""
    reason: str
    """One or two sentences saying what in the run decided it."""


def live_trial_verdict(  # noqa: PLR0913 (each argument is one separate record the rule reads, from either version)
    workflow: Workflow,
    touched: Collection[str],
    run: RunEvidence,
    *,
    run_grades: Sequence[Grade],
    baseline: Sequence[RunEvidence],
    baseline_grades: Sequence[Grade],
    previous: Workflow,
) -> LiveTrialVerdict:
    """Read one ended run of a version on live trial as ignored, failed, worse or counting.

    A run a person stopped is ignored. A run whose outcome or checked status is failed is failed when a touched node
    did not produce and every node it wires from did. Any other failed run is ignored, baseline or not: a node a touched
    node wires from produced nothing, or every touched node produced and the run failed elsewhere. Against a baseline a
    run that did not fail is worse when its checked status ranks below every one of the baseline's, when a gate both
    versions hold was rejected more times than in any baseline run, or when one of its structural grades is more than
    `STRUCTURAL_MARGIN` below the baseline's mean for the same measure. An empty baseline makes only failed possible.
    Any other run counts.

    Args:
        workflow: The version on live trial.
        touched: The node ids its adaptation touched, from its lineage.
        run: The run that ended.
        run_grades: The grades about that run.
        baseline: The newest three completed runs of the previous version, or fewer when it has fewer.
        baseline_grades: The grades about those runs.
        previous: The previous version, which says which gates both versions hold.

    Returns:
        The verdict with its reason.
    """
    if run.outcome == "stopped":
        return LiveTrialVerdict(kind="ignored", reason="A person stopped the run, so it says nothing about the step.")
    if run.outcome == "failed" or run.result.status == "failed":
        return _failed_at_a_touched_node(workflow, touched, run)
    if not baseline:
        return LiveTrialVerdict(
            kind="counts", reason="The run did not fail at the adopted step, and the previous version has no completed run to compare it with."
        )
    return (
        _ranked_below(run, baseline)
        or _rejected_more(workflow, previous, run, baseline)
        or _graded_lower(run_grades, baseline_grades)
        or LiveTrialVerdict(kind="counts", reason=f"The run did as well as the previous version's last {len(baseline)} completed runs.")
    )


def _failed_at_a_touched_node(workflow: Workflow, touched: Collection[str], run: RunEvidence) -> LiveTrialVerdict:
    """Failed when a touched node alone did not produce, otherwise ignored."""
    upstream_of = {node.id: [wire.node for _, wire in wires_of(node) if isinstance(wire, FromNode)] for node in workflow.nodes}
    silent = [node_id for node_id in upstream_of if node_id in touched and node_id not in run.produced]
    for node_id in silent:
        if all(source in run.produced for source in upstream_of[node_id]):
            return LiveTrialVerdict(
                kind="failed", reason=f"The run failed and {node_id}, which runs the adopted step, produced nothing while every node it reads produced."
            )
    if not silent:
        return LiveTrialVerdict(
            kind="ignored", reason="The run failed outside the nodes that run the adopted step, so it says nothing about the step."
        )
    node_id = silent[0]
    idle = ", ".join(source for source in upstream_of[node_id] if source not in run.produced)
    return LiveTrialVerdict(
        kind="ignored", reason=f"The run failed before {node_id} could run: {idle} produced nothing, so the run says nothing about the adopted step."
    )


def _ranked_below(run: RunEvidence, baseline: Sequence[RunEvidence]) -> LiveTrialVerdict | None:
    """Worse when the run's checked status ranks below every baseline run's."""
    if _RANKS[run.result.status] >= min(_RANKS[earlier.result.status] for earlier in baseline):
        return None
    theirs = ", ".join(earlier.result.status for earlier in baseline)
    return LiveTrialVerdict(
        kind="worse",
        reason=f"The run's checked status is {run.result.status}, below every one of the previous version's last completed runs ({theirs}).",
    )


def _rejected_more(workflow: Workflow, previous: Workflow, run: RunEvidence, baseline: Sequence[RunEvidence]) -> LiveTrialVerdict | None:
    """Worse when a gate both versions hold was rejected more times than in any baseline run."""
    held_before = {node.id for node in previous.nodes if node.kind in _GATE_KINDS}
    for node in workflow.nodes:
        if node.kind not in _GATE_KINDS or node.id not in held_before:
            continue
        rejected = run.gates_rejected.get(node.id, 0)
        most = max(earlier.gates_rejected.get(node.id, 0) for earlier in baseline)
        if rejected > most:
            return LiveTrialVerdict(
                kind="worse",
                reason=f"Gate {node.id} was rejected {rejected} times, more than in any of the previous version's last completed runs (at most {most}).",
            )
    return None


def _graded_lower(run_grades: Sequence[Grade], baseline_grades: Sequence[Grade]) -> LiveTrialVerdict | None:
    """Worse when a structural grade of the run is more than the margin below the baseline's mean for its measure."""
    for grade in run_grades:
        if grade.judge_kind != "structural":
            continue
        theirs = [earlier.score for earlier in baseline_grades if earlier.judge_kind == "structural" and earlier.measure == grade.measure]
        if not theirs:
            continue
        mean = sum(theirs) / len(theirs)
        drop = mean - grade.score
        if drop > STRUCTURAL_MARGIN and not math.isclose(drop, STRUCTURAL_MARGIN):
            return LiveTrialVerdict(
                kind="worse",
                reason=(
                    f"Its structural grade for {grade.measure} is {grade.score:.2f}, more than {STRUCTURAL_MARGIN} below "
                    f"the previous version's mean of {mean:.2f}."
                ),
            )
    return None
