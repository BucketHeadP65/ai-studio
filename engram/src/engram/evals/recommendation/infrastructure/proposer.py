"""The pydantic-ai proposer that drafts a recommendation's schema edits from a structural diagnosis.

Given the diagnosis and the current schema, the agent drafts typed edits for a human to approve, plus a
free-form findings tail. Its output is a `DraftRecommendation`, which carries no measured evidence. The gate
adds the evidence from the diagnosis, so the model never grades its own edits.
The agent carries no retrieval tools, only its one output tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai import Agent

from engram.evals.recommendation.domain.recommendation import DraftRecommendation

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic_ai.models import Model

    from engram.evals.rca.contracts import FailingCase
    from engram.evals.recommendation.domain.detectors import MissingEdgeSignal, OverExpansionSignal, SectionSplitSignal, StructuralDiagnosis
    from engram.evals.recommendation.domain.recommendation import CounterfactualResult
    from engram.kb.contracts import KnowledgeBaseSchema

_RECOMMENDATION_INSTRUCTIONS = (
    "You are a retrieval-schema recommendation for a document knowledge_base. You are given a structural diagnosis of "
    "one retrieval evaluation run (where the agent-under-test failed, which gold pages carry over-budget "
    "sections, which gold pages cannot reach each other in the graph) and a summary of the knowledge base's "
    "current schema (its page types, their canonical sections, and its typed relations). Propose the "
    "human-gated schema edits the diagnosis supports, and nothing it does not.\n\n"
    "There are exactly two typed edit ops:\n"
    "- restructure_sections: replace one existing page type's full ordered list of canonical sections "
    "(the chunking-granularity edit). Use it only when a section-split signal shows an existing page "
    "type's gold sections are over the token budget.\n"
    "- add_relation: register one new typed relation and place its link or link-list edge field on an "
    "existing page type (the graph-layering edit). Use it only when a missing-edge signal shows gold "
    "pages that should connect but cannot, taking the mined unlifted links as the relation's shape.\n\n"
    "Rules:\n"
    "- Reference only page-type kinds and section slugs that already appear in the schema summary, and "
    "never reuse an existing relation slug for a new relation.\n"
    "- Copy every case identifier from the diagnosis verbatim into the proposal's case_names and each "
    "prediction's target_cases. The identifiers may be full question strings, reproduce them exactly, "
    "never paraphrase or invent one.\n"
    "- Give every restructure_sections op one migration brief row per affected page, naming the note to "
    "edit and the imperative split action.\n"
    "- Make every prediction falsifiable: a metric (for example recall@5), a direction (increase or "
    "no_regression), and the target cases it should move.\n"
    "- Put anything outside these two typed ops (a new page type, a compound layer design pairing a "
    "page type with its relations, or a machinery idea like embedding granularity or summary grain) in "
    "the findings tail, named and grounded in the cases it addresses, never as a typed op.\n"
    "- Give the whole recommendation an overall rationale tying the proposals back to the diagnosis."
)


def build_recommendation_agent(model: Model | str, *, instructions: str = _RECOMMENDATION_INSTRUCTIONS) -> Agent[None, DraftRecommendation]:
    """Build the recommendation-proposer agent (structured output, no retrieval tools).

    The agent exposes no tools, only the `DraftRecommendation` output tool. An edit with an unknown `op`
    is an output retry.

    Args:
        model: The model that drafts the recommendation, Azure OpenAI in production, a network-free
            `TestModel` or `FunctionModel` in tests.
        instructions: The proposer's system prompt (defaults to `_RECOMMENDATION_INSTRUCTIONS`).

    Returns:
        An `Agent` whose output is a validated `DraftRecommendation`.
    """
    return Agent(model, instructions=instructions, output_type=DraftRecommendation)


def _recommendation_prompt(diagnosis: StructuralDiagnosis, spec: KnowledgeBaseSchema) -> str:
    """The user prompt: the current schema's page types and relations, then the structural diagnosis."""
    return (
        "Propose the human-gated schema edits the diagnosis supports, then explain them.\n\n"
        f"--- current schema ---\n{_render_spec(spec)}\n"
        f"--- structural diagnosis ---\n{_render_diagnosis(diagnosis)}\n"
        "--- end ---"
    )


def _render_spec(spec: KnowledgeBaseSchema) -> str:
    """Render the schema's page types (sections plus edge fields) and its typed relations."""
    page_types = "\n".join(
        f"  - {pt.page_type}: canonical sections [{', '.join(pt.canonical_section_slugs) or '(none)'}], "
        f"edge fields [{', '.join(pt.edge_fields) or '(none)'}]"
        for pt in spec.page_types
    )
    relations = "\n".join(f"  - {rel.slug}: {rel.domain} -> {rel.range}" for rel in spec.relations) if spec.relations else "  (none)"
    return f"page types:\n{page_types}\nrelations:\n{relations}"


def _render_diagnosis(diagnosis: StructuralDiagnosis) -> str:
    """Render the structural diagnosis: grade, overall metrics, the three signal groups, worst cases."""
    sections = (
        f"evidence grade: {diagnosis.evidence_grade}",
        f"overall: {_render_metrics(diagnosis.base.overall)}",
        "section-split signals:\n" + _render_section_splits(diagnosis.section_splits),
        "missing-edge signals:\n" + _render_missing_edges(diagnosis.missing_edges),
        "over-expansion signal:\n" + _render_over_expansion(diagnosis.over_expansion),
        "worst cases:\n" + _render_worst_cases(diagnosis.base.worst_cases),
    )
    return "\n".join(sections)


def _render_metrics(metrics: Mapping[str, float]) -> str:
    """Render a metric mapping as a compact sorted `name=value` list."""
    return ", ".join(f"{name}={value:.3f}" for name, value in sorted(metrics.items())) or "(none)"


def _render_section_splits(signals: tuple[SectionSplitSignal, ...]) -> str:
    """Render each section-split signal as its page type, cases, and over-budget sections."""
    if not signals:
        return "  (none)"
    return "\n".join(
        f"  - kind {signal.page_type}: cases {_render_case_names(signal.case_names)}, over-budget sections "
        + (", ".join(f"{stat.page_id}#{stat.canon} ({stat.tokens})" for stat in signal.offenders) or "(none)")
        for signal in signals
    )


def _render_missing_edges(signals: tuple[MissingEdgeSignal, ...]) -> str:
    """Render each missing-edge signal as its cases, unreachable pairs, mined candidates, counterfactual."""
    if not signals:
        return "  (none)"
    return "\n".join(
        f"  - cases {_render_case_names(signal.case_names)}, unreachable pairs "
        + (", ".join(f"{src} <-> {dst}" for src, dst in signal.unreachable_pairs) or "(none)")
        + ", mined candidates "
        + (
            ", ".join(
                f"{link.src_page_id} ({link.src_type}) -> {link.dst_page_id or '(dangling)'} "
                f"({link.dst_type or 'unresolved'}) via {link.rel}"
                for link in signal.mined_candidates
            )
            or "(none)"
        )
        + f", {_render_counterfactual(signal.counterfactual)}"
        for signal in signals
    )


def _render_counterfactual(counterfactual: CounterfactualResult | None) -> str:
    """Render the graph counterfactual as a reachability delta, or note it was not measured."""
    if counterfactual is None:
        return "counterfactual: (not measured)"
    return (
        f"counterfactual: {counterfactual.reachable_before} -> {counterfactual.reachable_after} of "
        f"{counterfactual.n_pairs} pairs reachable within {counterfactual.max_hops} hops"
    )


def _render_over_expansion(signal: OverExpansionSignal | None) -> str:
    """Render the over-expansion signal as its cases and the citation and cost means."""
    if signal is None:
        return "  (none)"
    return (
        f"  - cases {_render_case_names(signal.case_names)}, mean cited {signal.mean_cited:.1f}, "
        f"mean gold {signal.mean_gold:.1f}, mean tool calls {signal.mean_tool_calls:.1f}"
    )


def _render_worst_cases(cases: tuple[FailingCase, ...]) -> str:
    """Render each worst case as its recall, archetype, question, and gold vs cited ids."""
    if not cases:
        return "  (none)"
    return "\n".join(
        f"  - recall={case.recall:.3f} archetype={case.archetype.value if case.archetype is not None else 'unlabelled'}\n"
        f"    question: {case.question}\n"
        f"    gold ids: {', '.join(case.gold_ids) or '(none)'}\n"
        f"    cited ids: {', '.join(case.cited_ids) or '(none)'}"
        for case in cases
    )


def _render_case_names(names: tuple[str, ...]) -> str:
    """Render a signal's case identifiers as a compact list, or a placeholder when empty."""
    return "; ".join(names) if names else "(none)"


class PydanticAiRecommendationProposer:
    """A `RecommendationProposer` that drives a pydantic-ai agent over an injected model.

    It builds the agent afresh from the stored model on every call.
    """

    def __init__(self, model: Model | str) -> None:
        """Bind the model the per-call recommendation agent is built from.

        Args:
            model: The model the agent drafts with, a model instance or a model name.
        """
        self._model = model

    async def propose(self, diagnosis: StructuralDiagnosis, spec: KnowledgeBaseSchema) -> DraftRecommendation:
        """Run the recommendation agent on a prompt built from `diagnosis` and `spec`.

        Every returned edit has a known `op`, because pydantic-ai retries the model on an unknown one.

        Args:
            diagnosis: The structural diagnosis to draft against.
            spec: The knowledge base's current schema, whose page types and relations the prompt lists.

        Returns:
            The model's draft recommendation, which the gate must still check.
        """
        agent = build_recommendation_agent(self._model)
        result = await agent.run(_recommendation_prompt(diagnosis, spec))
        return result.output
