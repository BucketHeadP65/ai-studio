"""The gate that turns a model's draft recommendation into a validated `Recommendation`.

The model never grades itself. Each proposal's evidence is composed from the structural diagnosis,
never from the case names the draft claims. The gate does no IO, reads no clock and calls no model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.evals.recommendation.domain.recommendation import (
    AddRelationOp,
    ProposalEvidence,
    Recommendation,
    RestructureSectionsOp,
    SchemaEditProposal,
    apply_op,
)
from engram.evals.shared.contracts import RECALL_PREFIX, primary_at_k_key

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from engram.evals.recommendation.domain.detectors import StructuralDiagnosis
    from engram.evals.recommendation.domain.recommendation import (
        BaselineSnapshot,
        CounterfactualResult,
        DraftProposal,
        DraftRecommendation,
        MinedLink,
        SchemaEditOp,
    )
    from engram.kb.contracts import KnowledgeBaseSchema


def gate_recommendation(  # noqa: PLR0913 (each arg is a distinct handler-supplied input, bundling them would obscure the call contract)
    draft: DraftRecommendation,
    diagnosis: StructuralDiagnosis,
    schema: KnowledgeBaseSchema,
    *,
    baseline: BaselineSnapshot,
    collection: str,
    created_at: datetime,
    min_cases_per_proposal: int,
    max_proposals: int,
) -> Recommendation:
    """Gate a draft recommendation into a validated, evidence-ranked `Recommendation`.

    Each draft proposal must survive `apply_op` against `schema`, carry a migration brief when it is a
    restructure op, and gather at least `min_cases_per_proposal` failing cases of mechanically composed
    evidence. Survivors are ranked by `recall_deficit` descending (stable on ties, preserving draft
    order), deduped so identical op payloads keep the strongest occurrence, and truncated to
    `max_proposals`. The findings tail is passed through untouched.

    Args:
        draft: The proposer's pre-gate emission.
        diagnosis: The structural diagnosis the evidence is composed from.
        schema: The current knowledge base schema each op is validated against.
        baseline: The baseline snapshot to stamp onto the recommendation.
        collection: The collection the recommendation is produced for.
        created_at: The timezone-aware timestamp to stamp onto the recommendation.
        min_cases_per_proposal: The minimum evidence case count a proposal must reach to survive.
        max_proposals: The cap on how many ranked proposals the recommendation carries.

    Returns:
        The frozen `Recommendation`, its proposals ranked strongest evidence first.
    """
    gated = [
        proposal
        for draft_proposal in draft.proposals
        if (proposal := _gate_proposal(draft_proposal, diagnosis, schema, min_cases_per_proposal=min_cases_per_proposal)) is not None
    ]
    ranked = sorted(gated, key=lambda proposal: proposal.evidence.recall_deficit, reverse=True)
    proposals = tuple(_dedupe(ranked)[:max_proposals])
    return Recommendation(collection=collection, created_at=created_at, baseline=baseline, proposals=proposals, findings=draft.findings)


def _gate_proposal(
    draft: DraftProposal, diagnosis: StructuralDiagnosis, spec: KnowledgeBaseSchema, *, min_cases_per_proposal: int
) -> SchemaEditProposal | None:
    """Validate one draft proposal, or None when its op fails `apply_op`, a restructure op has no migration brief, or too few cases back it."""
    op = draft.op
    try:
        apply_op(spec, op)
    except ValueError:
        return None
    if isinstance(op, RestructureSectionsOp) and not draft.migration:
        return None
    evidence = _compose_evidence(op, diagnosis)
    if len(evidence.case_names) < min_cases_per_proposal:
        return None
    return SchemaEditProposal(
        id=draft.id, op=op, evidence=evidence, predictions=draft.predictions, migration=draft.migration, rationale=draft.rationale
    )


def _compose_evidence(op: SchemaEditOp, diagnosis: StructuralDiagnosis) -> ProposalEvidence:
    """Compose a proposal's measured evidence from the diagnosis, dispatched on the op class."""
    if isinstance(op, RestructureSectionsOp):
        return _restructure_evidence(op, diagnosis)
    return _add_relation_evidence(op, diagnosis)


def _restructure_evidence(op: RestructureSectionsOp, diagnosis: StructuralDiagnosis) -> ProposalEvidence:
    """Match the section-split signals of the op's page type and format their over-budget offenders."""
    signals = tuple(signal for signal in diagnosis.section_splits if signal.page_type == op.page_type)
    case_names = _dedupe_names(name for signal in signals for name in signal.case_names)
    oversized = tuple(f"{stat.page_id}#{stat.canon} ({stat.tokens})" for signal in signals for stat in signal.offenders)
    return ProposalEvidence(
        case_names=case_names,
        recall_deficit=_recall_deficit(diagnosis, case_names),
        counterfactual=None,
        mined_links=(),
        oversized_sections=oversized,
    )


def _add_relation_evidence(op: AddRelationOp, diagnosis: StructuralDiagnosis) -> ProposalEvidence:
    """Match every missing-edge signal, keep the mined links that fit the relation and carry the first counterfactual."""
    signals = diagnosis.missing_edges
    case_names = _dedupe_names(name for signal in signals for name in signal.case_names)
    mined = tuple(link for signal in signals for link in signal.mined_candidates if _link_matches_relation(link, op))
    counterfactual = _first_counterfactual(signal.counterfactual for signal in signals)
    return ProposalEvidence(
        case_names=case_names,
        recall_deficit=_recall_deficit(diagnosis, case_names),
        counterfactual=counterfactual,
        mined_links=mined,
        oversized_sections=(),
    )


def _link_matches_relation(link: MinedLink, op: AddRelationOp) -> bool:
    """Whether a mined link's page types fit the relation's domain and range, an unknown destination always fitting."""
    src_ok = link.src_type in {op.on_page_type, op.relation.domain}
    dst_ok = link.dst_type is None or link.dst_type == op.relation.range
    return src_ok and dst_ok


def _first_counterfactual(counterfactuals: Iterable[CounterfactualResult | None]) -> CounterfactualResult | None:
    """The first measured counterfactual among the matched signals, or None when none was measured."""
    return next((counterfactual for counterfactual in counterfactuals if counterfactual is not None), None)


def _recall_deficit(diagnosis: StructuralDiagnosis, case_names: tuple[str, ...]) -> float:
    """The largest shortfall of an evidence case's bucket recall below the overall recall, 0.0 when none resolves."""
    base = diagnosis.base
    recall_key = primary_at_k_key(base.overall, prefix=RECALL_PREFIX)
    overall_recall = base.overall.get(recall_key) if recall_key is not None else None
    if recall_key is None or overall_recall is None:
        return 0.0
    bucket_by_slice = {(bucket.archetype, bucket.difficulty): bucket for bucket in base.buckets}
    slice_by_case = {case.question: (case.archetype, case.difficulty) for case in base.worst_cases}
    deficits: list[float] = []
    for name in case_names:
        slice_key = slice_by_case.get(name)
        bucket = bucket_by_slice.get(slice_key) if slice_key is not None else None
        bucket_recall = bucket.metrics.get(recall_key) if bucket is not None else None
        if bucket_recall is not None:
            deficits.append(max(0.0, overall_recall - bucket_recall))
    return max(deficits) if deficits else 0.0


def _dedupe(proposals: list[SchemaEditProposal]) -> list[SchemaEditProposal]:
    """Keep the first occurrence of each op payload, so a ranked list keeps the strongest duplicate."""
    seen: set[SchemaEditOp] = set()
    deduped: list[SchemaEditProposal] = []
    for proposal in proposals:
        if proposal.op in seen:
            continue
        seen.add(proposal.op)
        deduped.append(proposal)
    return deduped


def _dedupe_names(names: Iterable[str]) -> tuple[str, ...]:
    """The case names in first-seen order with duplicates removed."""
    return tuple(dict.fromkeys(names))
