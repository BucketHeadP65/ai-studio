"""The reflection agent: a pydantic-ai model that drafts ranked findings from a diagnosis.

The agent carries no retrieval tools. Its only output is a `DraftReflection`, which
`gate_reflection` must still re-validate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai import Agent

from engram.evals.rca.domain.reflection import DraftReflection

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic_ai.models import Model

    from engram.evals.rca.domain.diagnosis import Diagnosis, ErrorType


# -- the reflection agent (LLM-assisted diagnosis) ---------------------------

_REFLECTOR_INSTRUCTIONS = (
    "You are a retrieval-eval diagnostician. You are given a deterministic diagnosis of one retrieval "
    "evaluation run: its overall metrics, the per-case error-type taxonomy (gold_missed / gold_buried / "
    "over_fetch / abandoned / none), the per-bucket (archetype x difficulty) means and error counts, "
    "and the worst cases. Draft ranked findings that explain the failures, most important first. Do "
    "NOT score, grade, or invent numbers, the detectors already computed every measurement. For each "
    "finding: name the retrieval surface it blames (S1 retrieval instructions, S2 retrieval config, S3 "
    "schema and ingestion), the error type it explains, a short case-specific narrative, and a "
    "change-manifest of falsifiable predictions. Each prediction names a bucket by its archetype and "
    "difficulty, a metric that appears in the diagnosis, and whether that metric should go up or down. "
    "List separately the buckets a change puts at risk. Reference only buckets and metrics present in "
    "the diagnosis."
)


def build_reflector_agent(model: Model | str, *, instructions: str = _REFLECTOR_INSTRUCTIONS) -> Agent[None, DraftReflection]:
    """Build the reflection agent (structured output, no retrieval tools).

    The agent exposes no tools, only the `DraftReflection` output tool. Its output is always a
    structurally valid `DraftReflection`, which `gate_reflection` must still re-validate.

    Args:
        model: The model that drafts the reflection.
        instructions: The reflection system prompt (defaults to `_REFLECTOR_INSTRUCTIONS`).

    Returns:
        An `Agent` whose output is a validated `DraftReflection`.
    """
    return Agent(model, instructions=instructions, output_type=DraftReflection)


def _reflector_prompt(diagnosis: Diagnosis) -> str:
    """Assemble the user prompt: the diagnosis overall metrics, error totals, buckets, and worst cases."""
    return (
        "Draft ranked findings that explain where this retrieval evaluation run failed, then predict how "
        "each proposed change would move the buckets.\n\n"
        f"--- diagnosis ---\n{_render_diagnosis(diagnosis)}\n"
        "--- end ---"
    )


def _render_diagnosis(diagnosis: Diagnosis) -> str:
    """Render the diagnosis: overall metrics, run-wide error totals, per-bucket slices, worst cases."""
    sections = (
        f"overall: {_render_metrics(diagnosis.overall)}",
        f"error totals: {_render_error_counts(diagnosis.error_totals)}",
        "buckets (by archetype/difficulty):\n" + _render_buckets(diagnosis),
        "worst cases (lowest recall):\n" + _render_worst_cases(diagnosis),
    )
    return "\n".join(sections)


def _render_metrics(metrics: Mapping[str, float]) -> str:
    """Render a metric mapping as a compact sorted `name=value` list."""
    return ", ".join(f"{name}={value:.3f}" for name, value in sorted(metrics.items())) or "(none)"


def _render_error_counts(counts: Mapping[ErrorType, int]) -> str:
    """Render an error-type count mapping as a compact sorted `type=count` list."""
    return ", ".join(f"{name}={count}" for name, count in sorted(counts.items())) or "(none)"


def _render_buckets(diagnosis: Diagnosis) -> str:
    """Render each per-bucket slice as its labels, case count, mean metrics, and error counts."""
    if not diagnosis.buckets:
        return "  (none)"
    return "\n".join(
        f"  - {bucket.archetype.value if bucket.archetype is not None else 'unlabelled'}/{bucket.difficulty} "
        f"(n={bucket.n_cases}): {_render_metrics(bucket.metrics)}. Errors {_render_error_counts(bucket.error_counts)}"
        for bucket in diagnosis.buckets
    )


def _render_worst_cases(diagnosis: Diagnosis) -> str:
    """Render each worst case as its labels, error type, surface, gold rank, and gold vs cited ids."""
    if not diagnosis.worst_cases:
        return "  (none)"
    return "\n".join(
        f"  - {case.archetype.value if case.archetype is not None else 'unlabelled'}/{case.difficulty} "
        f"error={case.error_type} surface={case.surface} recall={case.recall:.3f} "
        f"gold_rank={case.gold_rank if case.gold_rank is not None else 'n/a'}\n"
        f"    question: {case.question}\n"
        f"    gold ids: {', '.join(case.gold_ids) or '(none)'}\n"
        f"    cited ids: {', '.join(case.cited_ids) or '(none)'}"
        for case in diagnosis.worst_cases
    )


class PydanticAiReflector:
    """A `Reflector` backed by a pydantic-ai agent, built again on every call."""

    def __init__(self, model: Model | str) -> None:
        """Bind the model the per-call reflection agent is built from.

        Args:
            model: The model that drafts each reflection.
        """
        self._model = model

    async def reflect(self, diagnosis: Diagnosis) -> DraftReflection:
        """Draft a `DraftReflection` from `diagnosis`.

        pydantic-ai validates the output's structure. `gate_reflection` must still re-validate it
        against the diagnosis.

        Args:
            diagnosis: The deterministic diagnosis to draft against.

        Returns:
            The model's pre-gate draft reflection.
        """
        agent = build_reflector_agent(self._model)
        result = await agent.run(_reflector_prompt(diagnosis))
        return result.output
