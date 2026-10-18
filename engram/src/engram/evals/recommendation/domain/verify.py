"""Judge a recommendation after its edits were applied and the knowledge base was indexed again.

`verify_recommendation` splits the baseline cases into retained (the gold pages still exist and the rerun measured
them) and dropped. It compares each retained case's recall with the baseline snapshot, judges every prediction
against its retained targets, and attributes knowledge base drift through the index manifest. A change outside the
migration briefs is an unattributed drift warning.

It does no IO. You must pass the rerun recalls, the surviving gold pages, the current knowledge base stamp and the
manifest's changed paths. `render_verify_markdown` renders the report.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from collections.abc import Set as AbstractSet
    from datetime import datetime

    from engram.evals.recommendation.domain.recommendation import KnowledgeBaseStamp, Prediction, Recommendation

_EPSILON = 1e-9
_MAX_LISTED_PATHS = 5


class CaseDelta(BaseModel):
    """One retained case's recall movement between the baseline snapshot and the re-run.

    Attributes:
        case_name: The dataset case name.
        baseline_recall: The case's recall in the recommendation's baseline snapshot.
        current_recall: The case's recall in the verify re-run.
        delta: Current minus baseline.
        targeted: Whether any proposal's prediction names this case as a target.
    """

    model_config = ConfigDict(frozen=True)

    case_name: str
    baseline_recall: float
    current_recall: float
    delta: float
    targeted: bool


class PredictionVerdict(BaseModel):
    """One prediction judged mechanically against its retained target cases.

    Attributes:
        proposal_id: The proposal the prediction belongs to.
        description: The prediction's own statement.
        metric: The metric the prediction was stated in.
        direction: The claimed direction, an increase or no regression.
        target_cases: The prediction's declared target cases.
        retained_targets: The targets that survived the retained partition and were measured.
        holds: True when the claim held, False when it was falsified, None when unmeasurable.
        detail: A short human-readable account of the measured movement.
    """

    model_config = ConfigDict(frozen=True)

    proposal_id: str
    description: str
    metric: str
    direction: Literal["increase", "no_regression"]
    target_cases: tuple[str, ...]
    retained_targets: tuple[str, ...]
    holds: bool | None
    detail: str


class DriftReport(BaseModel):
    """The knowledge_base-drift attribution between the recommendation's stamp and the current knowledge_base.

    Attributes:
        knowledge_base_changed: Whether any part of the knowledge base stamp differs.
        spec_changed: Whether the spec fingerprint differs (an `apply-spec` plus reindex shows here).
        content_changed: Whether the manifest digest differs (content was migrated or edited).
        changed_paths: The manifest paths indexed after the recommendation was created.
        unattributed_paths: The changed paths not covered by any proposal's migration brief.
        warning: The loud drift warning, or None when the change is fully attributed (or absent).
    """

    model_config = ConfigDict(frozen=True)

    knowledge_base_changed: bool
    spec_changed: bool
    content_changed: bool
    changed_paths: tuple[str, ...]
    unattributed_paths: tuple[str, ...]
    warning: str | None


class VerifyOutcome(BaseModel):
    """The full mechanized verification result the markdown report and the CLI summary render.

    Attributes:
        retained: The per-case deltas for baseline cases the re-run could measure.
        dropped: The baseline case names that could not be measured, gold gone or case missing.
        verdicts: One judged verdict per prediction across every proposal.
        targeted_mean_delta: The mean recall delta over the retained targeted cases (0.0 when none).
        untargeted_mean_delta: The mean recall delta over the retained untargeted cases (0.0 when none).
        drift: The knowledge_base-drift attribution.
    """

    model_config = ConfigDict(frozen=True)

    retained: tuple[CaseDelta, ...]
    dropped: tuple[str, ...]
    verdicts: tuple[PredictionVerdict, ...]
    targeted_mean_delta: float
    untargeted_mean_delta: float
    drift: DriftReport


def verify_recommendation(
    recommendation: Recommendation,
    *,
    current_recalls: Mapping[str, float],
    existing_pages: AbstractSet[str],
    current_stamp: KnowledgeBaseStamp,
    changed_paths: Sequence[str],
) -> VerifyOutcome:
    """Diff the re-run against the recommendation's baseline and judge every prediction, purely.

    Args:
        recommendation: The recommendation under verification.
        current_recalls: The re-run's primary recall per dataset case name.
        existing_pages: The baseline gold pages that still exist in the current knowledge_base.
        current_stamp: The current knowledge base stamp for the recommendation's collection.
        changed_paths: The manifest paths indexed after the recommendation was created. After a full
            rebuild every row's timestamp moves, so this may over-report, the drift warning wording
            accounts for the attribution being an approximation in that case.

    Returns:
        The `VerifyOutcome` with the retained/dropped partition, verdicts, deltas, and drift.
    """
    targeted_names = {
        target for proposal in recommendation.proposals for prediction in proposal.predictions for target in prediction.target_cases
    }
    retained: list[CaseDelta] = []
    dropped: list[str] = []
    for snapshot in recommendation.baseline.cases:
        measurable = snapshot.case_name in current_recalls and all(gold in existing_pages for gold in snapshot.gold_ids)
        if not measurable:
            dropped.append(snapshot.case_name)
            continue
        current = current_recalls[snapshot.case_name]
        retained.append(
            CaseDelta(
                case_name=snapshot.case_name,
                baseline_recall=snapshot.recall,
                current_recall=current,
                delta=current - snapshot.recall,
                targeted=snapshot.case_name in targeted_names,
            )
        )

    delta_by_name = {case.case_name: case.delta for case in retained}
    verdicts = tuple(
        _judge(proposal_id=proposal.id, prediction=prediction, delta_by_name=delta_by_name)
        for proposal in recommendation.proposals
        for prediction in proposal.predictions
    )
    return VerifyOutcome(
        retained=tuple(retained),
        dropped=tuple(dropped),
        verdicts=verdicts,
        targeted_mean_delta=_mean(case.delta for case in retained if case.targeted),
        untargeted_mean_delta=_mean(case.delta for case in retained if not case.targeted),
        drift=_attribute_drift(recommendation, current_stamp=current_stamp, changed_paths=changed_paths),
    )


def _judge(*, proposal_id: str, prediction: Prediction, delta_by_name: Mapping[str, float]) -> PredictionVerdict:
    """Judge one prediction against the retained targets: increase needs a positive mean, no-regression no drop."""
    retained_targets = tuple(target for target in prediction.target_cases if target in delta_by_name)
    if not retained_targets:
        return PredictionVerdict(
            proposal_id=proposal_id,
            description=prediction.description,
            metric=prediction.metric,
            direction=prediction.direction,
            target_cases=prediction.target_cases,
            retained_targets=(),
            holds=None,
            detail="no retained target cases to measure",
        )
    deltas = [delta_by_name[target] for target in retained_targets]
    mean = _mean(iter(deltas))
    holds = mean > _EPSILON if prediction.direction == "increase" else all(delta >= -_EPSILON for delta in deltas)
    detail = f"mean delta {mean:+.3f} over {len(retained_targets)} retained target case(s)"
    return PredictionVerdict(
        proposal_id=proposal_id,
        description=prediction.description,
        metric=prediction.metric,
        direction=prediction.direction,
        target_cases=prediction.target_cases,
        retained_targets=retained_targets,
        holds=holds,
        detail=detail,
    )


def _attribute_drift(recommendation: Recommendation, *, current_stamp: KnowledgeBaseStamp, changed_paths: Sequence[str]) -> DriftReport:
    """Attribute the knowledge base change: spec versus content, and briefed versus unattributed paths."""
    baseline = recommendation.baseline.knowledge_base
    spec_changed = current_stamp.spec_hash != baseline.spec_hash
    content_changed = current_stamp.manifest_digest != baseline.manifest_digest
    briefed = {order.note_path for proposal in recommendation.proposals for order in proposal.migration}
    unattributed = tuple(sorted(set(changed_paths) - briefed)) if content_changed else ()
    warning: str | None = None
    if content_changed and unattributed:
        listed = ", ".join(unattributed[:_MAX_LISTED_PATHS])
        more = f" (+{len(unattributed) - _MAX_LISTED_PATHS} more)" if len(unattributed) > _MAX_LISTED_PATHS else ""
        warning = f"unattributed knowledge base drift: {len(unattributed)} changed path(s) outside the migration briefs: {listed}{more}"
    elif content_changed and not changed_paths:
        warning = (
            "knowledge_base content changed but no manifest rows are newer than the baseline "
            "(a full rebuild resets every timestamp), attribution unavailable"
        )
    return DriftReport(
        knowledge_base_changed=current_stamp != baseline,
        spec_changed=spec_changed,
        content_changed=content_changed,
        changed_paths=tuple(changed_paths),
        unattributed_paths=unattributed,
        warning=warning,
    )


def _mean(values: Iterable[float]) -> float:
    """The arithmetic mean of a float iterable, 0.0 when empty (the report's neutral element)."""
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def render_verify_markdown(
    recommendation: Recommendation, outcome: VerifyOutcome, *, verified_at: datetime, policy_note: str | None = None
) -> str:
    """Render the verification outcome as a deterministic markdown report, the `.verify.md` sibling.

    Args:
        recommendation: The recommendation that was verified.
        outcome: The mechanized verification result.
        verified_at: The wall-clock timestamp of the verify run (the handler's injected clock).
        policy_note: An optional caveat about the policy the re-run used, rendered verbatim.

    Returns:
        The markdown text, ASCII-only and stable for identical inputs.
    """
    lines = [
        f"# Recommendation verification: {recommendation.collection}",
        "",
        f"**Baseline created:** {recommendation.created_at.isoformat()}",
        f"**Verified:** {verified_at.isoformat()}",
        f"**Dataset:** {recommendation.baseline.dataset}",
        f"**Evidence grade at baseline:** {recommendation.baseline.evidence_grade}",
    ]
    if policy_note:
        lines += ["", f"> {policy_note}"]
    lines += ["", "## Knowledge base drift", ""]
    drift = outcome.drift
    lines.append(f"- knowledge base changed: {'yes' if drift.knowledge_base_changed else 'no'}")
    lines.append(f"- spec fingerprint changed: {'yes' if drift.spec_changed else 'no'}")
    lines.append(f"- content (manifest digest) changed: {'yes' if drift.content_changed else 'no'}")
    if drift.changed_paths:
        lines.append(f"- changed paths since baseline: {', '.join(drift.changed_paths)}")
    if drift.warning:
        lines += ["", f"**WARNING:** {drift.warning}"]
    lines += ["", "## Case partition", "", f"- retained: {len(outcome.retained)}", f"- dropped: {len(outcome.dropped)}"]
    if outcome.dropped:
        lines.append(f"- dropped cases: {', '.join(outcome.dropped)}")
    lines += ["", "## Prediction verdicts", ""]
    if not outcome.verdicts:
        lines.append("(the recommendation carries no predictions)")
    for verdict in outcome.verdicts:
        status = "UNMEASURABLE" if verdict.holds is None else ("HOLDS" if verdict.holds else "FAILED")
        lines.append(
            f"- **{status}** [{verdict.proposal_id}] {verdict.description} ({verdict.metric}, {verdict.direction}): {verdict.detail}"
        )
    lines += [
        "",
        "## Deltas",
        "",
        f"- targeted mean delta: {outcome.targeted_mean_delta:+.3f}",
        f"- untargeted mean delta: {outcome.untargeted_mean_delta:+.3f}",
        "",
        "| case | baseline | current | delta | targeted |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {case.case_name} | {case.baseline_recall:.3f} | {case.current_recall:.3f} | {case.delta:+.3f} | {'yes' if case.targeted else 'no'} |"
        for case in outcome.retained
    ]
    return "\n".join(lines) + "\n"
