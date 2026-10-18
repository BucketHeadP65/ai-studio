"""Rendering of a recommendation as a MADR-shaped draft ADR for a person to review.

The draft has a title with an always-`proposed` status, a Context of the measured evidence, a Decision
with each schema edit in rank order, Consequences with the predictions and the migration burden, and a
findings tail for what the typed edits cannot express. Case names are cited verbatim, so each claim
joins back to its dataset case.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.evals.recommendation.domain.recommendation import RestructureSectionsOp

if TYPE_CHECKING:
    from engram.evals.recommendation.domain.recommendation import (
        MinedLink,
        Recommendation,
        SchemaEditOp,
        SchemaEditProposal,
        StructuralFinding,
    )


def render_draft_adr(recommendation: Recommendation) -> str:
    """Render a recommendation as a MADR-shaped draft ADR.

    The same recommendation always renders the same document, in its own proposal and finding order.

    Args:
        recommendation: The recommendation to render.

    Returns:
        The draft ADR as a markdown string.
    """
    lines: list[str] = []
    lines += _render_title(recommendation)
    lines += _render_context(recommendation)
    lines += _render_decision(recommendation)
    lines += _render_consequences(recommendation)
    lines += _render_findings(recommendation)
    return "\n".join(lines) + "\n"


def _render_title(recommendation: Recommendation) -> list[str]:
    """Render the title, the proposed-draft status, and the generation stamp."""
    return [
        f"# Draft ADR: Structural recommendation for `{recommendation.collection}`",
        "",
        "**Status:** proposed (a draft for human review, never auto-applied).",
        "",
        f"Generated {recommendation.created_at.isoformat()} for collection `{recommendation.collection}`.",
        "",
    ]


def _render_context(recommendation: Recommendation) -> list[str]:
    """Render the measured evidence: the baseline, the knowledge base stamp, and per-proposal evidence."""
    baseline = recommendation.baseline
    knowledge_base = baseline.knowledge_base
    lines = [
        "## Context",
        "",
        f"Baseline dataset `{baseline.dataset}`, evidence grade `{baseline.evidence_grade}`, policy source `{baseline.policy_source}`.",
        "",
        (
            f"Knowledge base stamp: collection `{knowledge_base.collection}`, spec hash `{knowledge_base.spec_hash}`, "
            f"manifest digest `{knowledge_base.manifest_digest}`, {knowledge_base.n_pages} pages."
        ),
        "",
    ]
    if baseline.overall:
        metrics = ", ".join(f"{name} {value:.3f}" for name, value in sorted(baseline.overall.items()))
        lines += [f"Baseline metrics: {metrics}.", ""]
    if not recommendation.proposals:
        lines += ["No spec proposals were gated in, so there is no measured proposal evidence.", ""]
        return lines
    for index, proposal in enumerate(recommendation.proposals, start=1):
        lines += _render_proposal_evidence(index, proposal)
    return lines


def _render_proposal_evidence(index: int, proposal: SchemaEditProposal) -> list[str]:
    """Render one proposal's measured evidence as a bullet list under a numbered heading."""
    evidence = proposal.evidence
    lines = [
        f"### Evidence {index}. `{proposal.id}`",
        "",
        f"- Failing cases: {_join_cases(evidence.case_names)}",
        f"- Recall deficit: {evidence.recall_deficit:.2f}",
    ]
    counterfactual = evidence.counterfactual
    if counterfactual is not None:
        lines.append(
            f"- Counterfactual reachability: {counterfactual.reachable_before} -> {counterfactual.reachable_after} "
            f"of {counterfactual.n_pairs} gold pairs within {counterfactual.max_hops} hops"
        )
    if evidence.mined_links:
        clusters = ", ".join(f"{cluster} ({count})" for cluster, count in _cluster_mined_links(evidence.mined_links))
        lines.append(f"- Mined-link clusters: {clusters}")
    if evidence.oversized_sections:
        lines.append(f"- Oversized sections: {', '.join(evidence.oversized_sections)}")
    simulation = evidence.simulation
    if simulation is not None:
        lines.append(
            f"- Shadow split: mean would-be rank {simulation.mean_rank_before:.1f} -> {simulation.mean_rank_after:.1f} "
            f"over {simulation.n_measured} case-section pair(s) ({simulation.n_unsplittable} unsplittable)"
        )
    lines.append("")
    return lines


def _render_decision(recommendation: Recommendation) -> list[str]:
    """Render the proposed schema edits, one subsection per proposal in rank order, with rationale."""
    lines = ["## Decision", ""]
    if not recommendation.proposals:
        lines += ["No spec proposals were gated in, so no spec edit is recommended.", ""]
        return lines
    for index, proposal in enumerate(recommendation.proposals, start=1):
        lines += [f"### {index}. `{proposal.id}`", "", _render_op(proposal.op), "", proposal.rationale, ""]
    return lines


def _render_op(op: SchemaEditOp) -> str:
    """Render one typed schema edit as a sentence a person reads."""
    if isinstance(op, RestructureSectionsOp):
        sections = ", ".join(op.canonical_sections) if op.canonical_sections else "(empty)"
        return f"Restructure page type `{op.page_type}` canonical sections to: {sections}."
    relation = op.relation
    return (
        f"Add relation `{relation.slug}` (domain `{relation.domain}` -> range `{relation.range}`). "
        f"Place edge field `{op.edge_field.name}` (type `{op.edge_field.type}`) on page type `{op.on_page_type}`."
    )


def _render_consequences(recommendation: Recommendation) -> list[str]:
    """Render the predicted impact and migration burden, one subsection per proposal."""
    lines = ["## Consequences", ""]
    if not recommendation.proposals:
        lines += ["No spec proposals were gated in, so there is no predicted impact or migration burden.", ""]
        return lines
    for index, proposal in enumerate(recommendation.proposals, start=1):
        lines += [f"### {index}. `{proposal.id}`", "", "Predicted impact:"]
        if proposal.predictions:
            for prediction in proposal.predictions:
                lines.append(
                    f"- {prediction.description} (cases {_join_cases(prediction.target_cases)}, "
                    f"metric {prediction.metric}, direction {prediction.direction})"
                )
        else:
            lines.append("- (no predictions stated)")
        lines.append("")
        count = len(proposal.migration)
        noun = "work order" if count == 1 else "work orders"
        lines.append(f"Migration burden: {count} {noun}.")
        for order in proposal.migration:
            lines.append(f"- {order.page_id} (`{order.note_path}`): {order.action}")
        lines.append("")
    return lines


def _render_findings(recommendation: Recommendation) -> list[str]:
    """Render the findings tail, one subsection per finding, or a graceful placeholder."""
    lines = ["## Findings tail", ""]
    if not recommendation.findings:
        lines += ["No findings outside the typed proposal grammar.", ""]
        return lines
    for finding in recommendation.findings:
        lines += _render_finding(finding)
    return lines


def _render_finding(finding: StructuralFinding) -> list[str]:
    """Render one findings-tail entry: its finding type, title, body, and related cases."""
    return [f"### [{finding.finding_type}] {finding.title}", "", finding.body, "", f"Related cases: {_join_cases(finding.related_cases)}", ""]


def _join_cases(cases: tuple[str, ...]) -> str:
    """Join case names verbatim, or return a placeholder when there are none."""
    return ", ".join(cases) if cases else "(none)"


def _cluster_mined_links(links: tuple[MinedLink, ...]) -> list[tuple[str, int]]:
    """Count mined links per source page type to destination page type arc, `(dangling)` for no destination, sorted by arc."""
    counts: dict[str, int] = {}
    for link in links:
        target = link.dst_type if link.dst_type is not None else "(dangling)"
        arc = f"{link.src_type} -> {target}"
        counts[arc] = counts.get(arc, 0) + 1
    return sorted(counts.items())
