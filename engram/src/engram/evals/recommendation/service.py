"""The recommendation's use cases: the structural diagnosis, its case names, and the split simulation.

`structural_diagnosis` reads what the three detectors need from the knowledge base and folds their signals into one diagnosis.
`remap_diagnosis` names every case of a diagnosis by its dataset case name.
`resolve_baseline_policy` and `resolve_rerun_policy` choose the policy a baseline runs under and a verification reruns under.
`case_snapshots` records each case of a run as the baseline snapshot keeps it.
`simulate_restructures` measures a restructure proposal and indexes nothing. It splits each over-budget gold section
into parts, filling each part with whole paragraphs up to the section token budget, and ranks the whole body and the
best part for the failing case's query among the knowledge base's real sections.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from core.markdown import split_section_body

from engram.evals.recommendation.domain.detectors import (
    PairReachability,
    StructuralDiagnosis,
    detect_missing_edges,
    detect_over_expansion,
    detect_section_splits,
)
from engram.evals.recommendation.domain.recommendation import (
    CandidateEdge,
    CaseSnapshot,
    CounterfactualResult,
    Recommendation,
    RestructureSectionsOp,
    SimulatedSplit,
    SimulationResult,
)
from engram.evals.shared.contracts import Archetype
from engram.evals.tuning.contracts import baseline_policy, load_policy
from engram.indexing.contracts import token_count
from engram.kb.contracts import ACTIVE_POLICY_PATH

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from engram.config import RecommendationSettings
    from engram.evals.commands import RecommendationKnowledgeBaseGateway
    from engram.evals.rca.contracts import Diagnosis, FailingCase, ReportLike
    from engram.evals.recommendation.domain.detectors import EvidenceGrade, MissingEdgeSignal
    from engram.evals.recommendation.domain.recommendation import MinedLink, OversizedSection, SchemaEditProposal
    from engram.evals.recommendation.internal_contracts import SimulationGateway
    from engram.evals.tuning.contracts import RetrievalPolicy

_MIN_PARTS = 2
"""A split is measurable only when it actually divides the body, one part is the unsplit whole."""


type PolicySource = Literal["active-policy", "explicit", "default"]
"""Where a baseline's policy came from: the knowledge base's active policy, a policy file the caller named, or the shipped baseline."""


@dataclass(frozen=True)
class _SimulationRun:
    """The per-run inputs one recommendation's simulation threads through its helpers."""

    diagnosis: StructuralDiagnosis
    gateway: SimulationGateway
    question_by_name: Mapping[str, str]
    token_budget: int
    collection: str


async def simulate_restructures(  # noqa: PLR0913 (each arg is a distinct handler-supplied input, bundling them would obscure the call contract)
    recommendation: Recommendation,
    diagnosis: StructuralDiagnosis,
    gateway: SimulationGateway,
    *,
    question_by_name: Mapping[str, str],
    token_budget: int,
    collection: str,
) -> Recommendation:
    """Add the simulated split's measured rank change to each restructure proposal of the recommendation.

    A case is measured only against the over-budget sections of its own gold pages, ranked for its original
    question. Other proposals, and proposals where nothing was measurable, come back unchanged.

    Args:
        recommendation: The gated recommendation to enrich.
        diagnosis: The structural diagnosis the recommendation was gated from, its cases named by dataset case name.
        gateway: The knowledge base reads the simulation runs on.
        question_by_name: The original question of each dataset case name.
        token_budget: The section token budget, both the offender filter and the per-part budget.
        collection: The collection whose sections the whole body and the parts are ranked among.

    Returns:
        A new recommendation whose restructure proposals carry a `SimulationResult` when measurable.
    """
    run = _SimulationRun(
        diagnosis=diagnosis, gateway=gateway, question_by_name=question_by_name, token_budget=token_budget, collection=collection
    )
    proposals = [await _simulate_proposal(proposal, run) for proposal in recommendation.proposals]
    return recommendation.model_copy(update={"proposals": tuple(proposals)})


async def _simulate_proposal(proposal: SchemaEditProposal, run: _SimulationRun) -> SchemaEditProposal:
    """Simulate the split for one proposal, returning it enriched or untouched."""
    op = proposal.op
    if not isinstance(op, RestructureSectionsOp):
        return proposal
    signals = [signal for signal in run.diagnosis.section_splits if signal.page_type == op.page_type]
    if not signals:
        return proposal
    case_names = tuple(dict.fromkeys(name for signal in signals for name in signal.case_names))
    offender_pages = tuple(dict.fromkeys(stat.page_id for signal in signals for stat in signal.offenders))
    sections = await run.gateway.oversized_section_bodies(offender_pages, token_budget=run.token_budget, collection=run.collection)
    result = await _measure(sections, case_names, run)
    if result is None:
        return proposal
    evidence = proposal.evidence.model_copy(update={"simulation": result})
    return proposal.model_copy(update={"evidence": evidence})


async def _measure(sections: Sequence[OversizedSection], case_names: tuple[str, ...], run: _SimulationRun) -> SimulationResult | None:
    """Split and rank every (case, gold over-budget section) pair, or None when nothing was measurable."""
    gold_by_name = {case.question: set(case.gold_ids) for case in run.diagnosis.base.worst_cases}
    splits: list[SimulatedSplit] = []
    unsplittable: set[tuple[str, str]] = set()
    for name in case_names:
        query = run.question_by_name.get(name)
        gold_ids = gold_by_name.get(name, set())
        if query is None:
            continue
        for section in sections:
            if section.page_id not in gold_ids:
                continue
            parts = split_section_body(section.body, token_budget=run.token_budget, count_tokens=token_count)
            if len(parts) < _MIN_PARTS:
                unsplittable.add((section.page_id, section.canon))
                continue
            ranks = await run.gateway.snippet_ranks(query, (section.body, *parts), collection=run.collection)
            splits.append(
                SimulatedSplit(
                    case_name=name,
                    page_id=section.page_id,
                    canon=section.canon,
                    n_parts=len(parts),
                    rank_before=ranks[0],
                    rank_after=min(ranks[1:]),
                )
            )
    if not splits and not unsplittable:
        return None
    return SimulationResult(
        n_measured=len(splits),
        n_unsplittable=len(unsplittable),
        mean_rank_before=_mean([split.rank_before for split in splits]),
        mean_rank_after=_mean([split.rank_after for split in splits]),
        splits=tuple(splits),
    )


def _mean(values: list[int]) -> float:
    """The arithmetic mean, 0.0 for an empty list (nothing measured)."""
    return sum(values) / len(values) if values else 0.0


async def structural_diagnosis(  # noqa: PLR0913 (the diagnosis base, report, gateway, grade, collection, and knobs are distinct inputs)
    base: Diagnosis,
    report: ReportLike,
    gateway: RecommendationKnowledgeBaseGateway,
    *,
    evidence_grade: EvidenceGrade,
    collection: str,
    settings: RecommendationSettings,
) -> StructuralDiagnosis:
    """Read what the three detectors need from the knowledge base and fold their signals into one diagnosis.

    A case counts as failing when its recall is below 1. Only failing trace and survey cases feed the
    missing-edge signal, so the diagnosis carries at most one.

    Args:
        base: The diagnosis of the evaluation run.
        report: The evaluation run's report, which the over-expansion detector reads.
        gateway: The knowledge base reads the detectors need.
        evidence_grade: Whether the run used a supplied policy or the raw baseline.
        collection: The collection the gateway reads.
        settings: The section token budget and the hop horizon the detectors use.

    Returns:
        The structural diagnosis of the run, its cases still named by their questions.
    """
    failing = [case for case in base.worst_cases if case.recall < 1.0]
    gold_pages = list(dict.fromkeys(gid for case in failing for gid in case.gold_ids))
    over_budget = await gateway.sections_over_budget(gold_pages, token_budget=settings.section_token_budget, collection=collection)
    page_types = await gateway.page_types(gold_pages, collection=collection)
    section_splits = detect_section_splits(base, over_budget, page_types)
    missing = await _missing_edges(base, failing, gateway, collection=collection, settings=settings)
    over_expansion = detect_over_expansion(report)
    return StructuralDiagnosis(
        base=base,
        evidence_grade=evidence_grade,
        section_splits=section_splits,
        missing_edges=(missing,) if missing is not None else (),
        over_expansion=over_expansion,
    )


async def _missing_edges(
    base: Diagnosis, failing: Sequence[FailingCase], gateway: RecommendationKnowledgeBaseGateway, *, collection: str, settings: RecommendationSettings
) -> MissingEdgeSignal | None:
    """Measure gold-pair reachability for the failing trace and survey cases, then mine links and measure the counterfactual."""
    graph_cases = [case for case in failing if case.archetype in (Archetype.TRACE, Archetype.SURVEY)]
    pairs = _gold_pairs(graph_cases)
    if not pairs:
        return None
    flags = await gateway.reachable_pairs(pairs, max_hops=settings.max_hops, collection=collection)
    reachability = tuple(PairReachability(src_id=src, dst_id=dst, reachable=flag) for (src, dst), flag in zip(pairs, flags, strict=True))
    mined = await gateway.unlifted_links(collection=collection)
    unreachable = tuple((entry.src_id, entry.dst_id) for entry in reachability if not entry.reachable)
    counterfactual = (
        await _counterfactual(gateway, unreachable, mined, collection=collection, max_hops=settings.max_hops) if unreachable else None
    )
    return detect_missing_edges(base, reachability, mined, counterfactual)


async def _counterfactual(
    gateway: RecommendationKnowledgeBaseGateway, unreachable: tuple[tuple[str, str], ...], mined: Sequence[MinedLink], *, collection: str, max_hops: int
) -> CounterfactualResult | None:
    """Add the candidate edges joining the unreachable pairs and measure how many pairs they connect."""
    candidates = _candidate_edges(unreachable, mined)
    if not candidates:
        return None
    flags = await gateway.reachable_pairs(list(unreachable), max_hops=max_hops, collection=collection, extra_edges=candidates)
    return CounterfactualResult(
        n_pairs=len(unreachable),
        reachable_before=0,
        reachable_after=sum(1 for flag in flags if flag),
        max_hops=max_hops,
        candidate_edges=candidates,
    )


def _gold_pairs(cases: Sequence[FailingCase]) -> list[tuple[str, str]]:
    """Every ordered gold pair (source != destination) across the cases, deduped in first-seen order."""
    pairs: dict[tuple[str, str], None] = {}
    for case in cases:
        for src in case.gold_ids:
            for dst in case.gold_ids:
                if src != dst:
                    pairs[(src, dst)] = None
    return list(pairs)


def _candidate_edges(unreachable: tuple[tuple[str, str], ...], mined: Sequence[MinedLink]) -> tuple[CandidateEdge, ...]:
    """The candidate edges whose resolved endpoints join an unreachable pair (unordered), deduped."""
    unordered = {frozenset(pair) for pair in unreachable}
    edges: dict[tuple[str, str, str], CandidateEdge] = {}
    for link in mined:
        if link.dst_page_id is None:
            continue
        if frozenset((link.src_page_id, link.dst_page_id)) in unordered:
            edges[(link.src_page_id, link.dst_page_id, link.rel)] = CandidateEdge(
                src_id=link.src_page_id, dst_id=link.dst_page_id, rel=link.rel
            )
    return tuple(edges.values())


def remap_diagnosis(diagnosis: StructuralDiagnosis, name_by_question: Mapping[str, str]) -> StructuralDiagnosis:
    """Rebuild the diagnosis with every case named by its dataset case name in place of its question.

    The worst cases and all three signal groups are renamed. A question with no entry in
    `name_by_question` keeps its question as its name.

    Args:
        diagnosis: The structural diagnosis whose cases are named by their questions.
        name_by_question: The dataset case name of each question.

    Returns:
        A copy of the diagnosis whose cases are named by their dataset case names.
    """
    base = diagnosis.base.model_copy(
        update={
            "worst_cases": tuple(
                case.model_copy(update={"question": name_by_question.get(case.question, case.question)})
                for case in diagnosis.base.worst_cases
            )
        }
    )
    section_splits = tuple(
        signal.model_copy(update={"case_names": _remap_names(signal.case_names, name_by_question)}) for signal in diagnosis.section_splits
    )
    missing_edges = tuple(
        signal.model_copy(update={"case_names": _remap_names(signal.case_names, name_by_question)}) for signal in diagnosis.missing_edges
    )
    over = diagnosis.over_expansion
    over_expansion = over.model_copy(update={"case_names": _remap_names(over.case_names, name_by_question)}) if over is not None else None
    return diagnosis.model_copy(
        update={"base": base, "section_splits": section_splits, "missing_edges": missing_edges, "over_expansion": over_expansion}
    )


def _remap_names(names: tuple[str, ...], name_by_question: Mapping[str, str]) -> tuple[str, ...]:
    """Remap each case identifier through the question-to-dataset-name map (identity when absent)."""
    return tuple(name_by_question.get(name, name) for name in names)


def resolve_baseline_policy(explicit: str | None, knowledge_base_path: Path) -> tuple[RetrievalPolicy, PolicySource, EvidenceGrade]:
    """The baseline policy with its source and evidence grade: explicit YAML, else the active policy, else the shipped one.

    Args:
        explicit: The path of a policy YAML the caller named, or `None` or empty for none.
        knowledge_base_path: The knowledge base root, where the active policy lives.

    Returns:
        The policy, its source, and the evidence grade of a baseline run under it. A policy someone tuned grades
        `residual-of-tuned`, and the shipped baseline grades `raw-baseline`.
    """
    if explicit:
        return load_policy(Path(explicit)), "explicit", "residual-of-tuned"
    active = knowledge_base_path.joinpath(*ACTIVE_POLICY_PATH)
    if active.exists():
        return load_policy(active), "active-policy", "residual-of-tuned"
    return baseline_policy(), "default", "raw-baseline"


def resolve_rerun_policy(source: PolicySource, knowledge_base_path: Path) -> tuple[RetrievalPolicy, str | None]:
    """The rerun policy for the baseline's recorded source, with a note for the report when it cannot be reproduced.

    Args:
        source: The source the baseline recorded.
        knowledge_base_path: The knowledge base root, where the active policy lives.

    Returns:
        The policy to rerun under, and a note for the report, or `None` when the rerun reproduces the baseline.
    """
    active = knowledge_base_path.joinpath(*ACTIVE_POLICY_PATH)
    if source == "default":
        return baseline_policy(), None
    if active.exists():
        note = (
            "the baseline ran under an explicit policy YAML. Re-ran under the knowledge base's active policy" if source == "explicit" else None
        )
        return load_policy(active), note
    return (
        baseline_policy(),
        f"the baseline ran under the {source} policy, which is no longer available. Re-ran under the shipped baseline",
    )


def case_snapshots(report: ReportLike, name_by_question: Mapping[str, str], recall_key: str | None) -> tuple[CaseSnapshot, ...]:
    """One `CaseSnapshot` per report case, named by its dataset case name.

    Args:
        report: The evaluation report whose cases are recorded.
        name_by_question: The dataset case name of each question.
        recall_key: The score name holding recall, or `None` when the report has no recall score, which records 0.0.

    Returns:
        One snapshot for each case, in report order.
    """
    snapshots: list[CaseSnapshot] = []
    for case in report.cases:
        gold = case.expected_output.cited_source_ids if case.expected_output is not None else ()
        result = case.scores.get(recall_key) if recall_key is not None else None
        snapshots.append(
            CaseSnapshot(
                case_name=name_by_question.get(case.inputs.question, case.inputs.question),
                recall=float(result.value) if result is not None else 0.0,
                gold_ids=tuple(gold),
                cited_ids=tuple(case.output.cited_source_ids),
            )
        )
    return tuple(snapshots)
