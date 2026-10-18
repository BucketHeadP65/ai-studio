"""The evaluation base: the immutable model, fingerprints, provenance, resource usage and run identity."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Iterable


type Split = Literal["train", "validation", "test"]
type ReviewAuthority = Literal["owner", "agent", "deterministic", "unreviewed"]
type Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type Nonempty = Annotated[str, Field(min_length=1)]


class EvaluationModel(BaseModel):
    """Immutable evaluation data. An unknown field marks an incompatible artifact and is refused."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


def require_unique(values: Iterable[str], label: str) -> None:
    """Refuse duplicate identifiers instead of silently merging observations.

    Args:
        values: The identifiers to check.
        label: What the identifiers are, named in the refusal.

    Raises:
        ValueError: An identifier appears twice.
    """
    items = tuple(values)
    if len(items) != len(set(items)):
        msg = f"duplicate {label}"
        raise ValueError(msg)


class Provenance(EvaluationModel):
    """Where a label came from and who reviewed it. An agent review is never an owner review."""

    source: Nonempty
    kind: Literal["synthetic", "recorded", "reproduced"]
    review: ReviewAuthority
    reviewer: Nonempty


class EvaluationInput(EvaluationModel):
    """The task content execution receives. Expected labels stay with the evaluator."""

    case_id: Nonempty
    input: Nonempty


class ResourceUsage(EvaluationModel):
    """Recorded usage. An unknown monetary cost stays unknown when totals are combined."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    requests: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


def total_usage(usages: Iterable[ResourceUsage]) -> ResourceUsage:
    """The sum of every attempt, failures included, without treating unpriced usage as free.

    Args:
        usages: The usage rows to add up.

    Returns:
        The combined usage. Its cost is `None` when any row's cost is unknown.
    """
    rows = tuple(usages)
    costs = tuple(row.cost_usd for row in rows)
    known_costs = tuple(cost for cost in costs if cost is not None)
    return ResourceUsage(
        input_tokens=sum(row.input_tokens for row in rows),
        output_tokens=sum(row.output_tokens for row in rows),
        requests=sum(row.requests for row in rows),
        cost_usd=sum(known_costs) if len(known_costs) == len(costs) else None,
    )


class RunIdentity(EvaluationModel):
    """The actual model and version, and the hashes of code, prompt, grader and execution conditions.

    Conditions cover sampling settings, seed policy, resource budgets and environment. Prompt and
    implementation changes have their own hashes. An adapter records the resolved model identity,
    never a mutable routing alias.
    """

    model: Nonempty
    model_version: Nonempty
    prompt_fingerprint: Fingerprint
    implementation_fingerprint: Fingerprint
    conditions_fingerprint: Fingerprint
    grader_fingerprint: Fingerprint
