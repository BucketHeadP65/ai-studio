"""The `@k` metric keys every evals reader picks its headline number from, and the closed set of known keys.

`primary_at_k_key` answers `{prefix}{PREFERRED_K}` when a run carries it, otherwise the smallest numeric cutoff present.
`is_known_metric_key` answers whether a key is one the evaluators can emit, legacy or namespaced by layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

RECALL_PREFIX = "recall@"
"""The metric-name prefix for a recall cutoff key."""

PRECISION_PREFIX = "precision@"
"""The metric-name prefix for a precision cutoff key."""

MRR_KEY = "mrr"
"""The metric-name key for mean reciprocal rank."""

NDCG_PREFIX = "ndcg@"
"""The metric-name prefix for an nDCG cutoff key."""

PREFERRED_K = 5
"""The cutoff to prefer, the harness default and the primary number AI Factory Studio shows."""

METRIC_LAYERS: tuple[str, ...] = ("citation", "surfaced", "ranking")
"""The layer names a namespaced metric key may carry as its `{layer}_` prefix."""


def primary_at_k_key(names: Iterable[str], *, prefix: str, preferred_k: int = PREFERRED_K) -> str | None:
    """The headline `{prefix}{k}` key for a run, preferring `preferred_k` then the smallest cutoff.

    Args:
        names: The metric names recorded on a run's scores.
        prefix: The cutoff family to resolve, `RECALL_PREFIX` or `PRECISION_PREFIX`.
        preferred_k: The cutoff to prefer when present, defaulting to `PREFERRED_K`.

    Returns:
        The chosen `{prefix}{k}` key, or `None` when no metric of that family is present.
    """
    matches = [name for name in names if name.startswith(prefix)]
    if not matches:
        return None
    preferred = f"{prefix}{preferred_k}"
    if preferred in matches:
        return preferred
    return min(matches, key=lambda name: at_k_cutoff(name, prefix=prefix))


def at_k_cutoff(name: str, *, prefix: str) -> tuple[int, str]:
    """The sort key of a `{prefix}{k}` name, its integer `k` first. A non-numeric suffix sorts last.

    Args:
        name: A metric name that starts with `prefix`.
        prefix: The cutoff family, `RECALL_PREFIX` or `PRECISION_PREFIX`.

    Returns:
        The pair of the cutoff and the name.
    """
    suffix = name.removeprefix(prefix)
    return (int(suffix), name) if suffix.isdigit() else (2**31, name)


def is_known_metric_key(name: str) -> bool:
    """Whether `name` names a metric the project's evaluators can emit, legacy or namespaced.

    Covers the legacy unprefixed citation-layer keys (`recall@{k}`, `precision@{k}`, `ndcg@{k}`, `mrr`) and
    every layer's namespaced keys (`{layer}_recall@{k}` and the rest, for `layer` in `METRIC_LAYERS`).

    Args:
        name: A candidate metric key.

    Returns:
        `True` when `name` matches a known key shape.
    """
    body = name
    for layer in METRIC_LAYERS:
        prefix = f"{layer}_"
        if body.startswith(prefix):
            body = body.removeprefix(prefix)
            break
    if body == MRR_KEY:
        return True
    for prefix in (RECALL_PREFIX, PRECISION_PREFIX, NDCG_PREFIX):
        if body.startswith(prefix):
            suffix = body.removeprefix(prefix)
            return suffix.isdigit() and int(suffix) > 0
    return False
