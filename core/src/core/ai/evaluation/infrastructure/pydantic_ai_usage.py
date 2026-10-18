"""A pydantic-ai run's measured usage, read into the evaluation's resource usage."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.evaluation.domain.models import ResourceUsage

if TYPE_CHECKING:
    from pydantic_ai.usage import RunUsage


def measured_usage(usage: RunUsage) -> ResourceUsage:
    """One pydantic-ai run's measured usage, with an unresolved price left unknown.

    Args:
        usage: What the run measured.

    Returns:
        The same tokens and requests, `cost_usd` only when the provider actually priced them.
    """
    return ResourceUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        requests=usage.requests,
        cost_usd=None if usage.cost is None else float(usage.cost),
    )
